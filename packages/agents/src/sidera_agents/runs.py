from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from threading import Condition, RLock
from time import monotonic
from uuid import UUID, uuid4

from pydantic import SecretStr

from .admission import request_fingerprint
from .context import ContextAssembler
from .contracts import (
    AssembledInput,
    CapabilitySupport,
    EffectiveConfig,
    InputReceipt,
    InputStatus,
    InterruptReceipt,
    InterruptStatus,
    NativeCancelState,
    NativeCloseState,
    NativeHandle,
    NativeOpenRequest,
    NativeOutcomeStatus,
    NativeProcessState,
    NativeResponseState,
    NativeSkill,
    ProfileRevision,
    RecoveryReceipt,
    RecoveryStatus,
    ResourceKind,
    RunInput,
    RunInterrupt,
    RunPolicy,
    RunRecover,
    RunSnapshot,
    RunStart,
    RunState,
    RunStop,
    RuntimeSnapshot,
    SourceContent,
    SourceRef,
    StartLookup,
    StartReceipt,
    StartStatus,
    StopReceipt,
    StopStatus,
    SupportStatus,
    ToolConnection,
)
from .errors import AgentError
from .input_delivery import (
    InputDeliverySettings,
    input_fingerprint,
    input_size,
)
from .native_workers import NativeWorkers
from .ports import (
    ContextSourceReader,
    ExecutionAuthorizer,
    SecretReader,
    ToolConnectionReader,
)
from .recovery import LiveRunContext, supports_session_load

# Only these stable identifiers may cross the native boundary into run state.
_SAFE_NATIVE_REASON_CODES = frozenset(
    {
        "native_spawn_failed",
        "native_start_failed",
        "native_start_unknown",
        "native_identity_unverified",
        "native_response_failed",
        "native_response_unknown",
        "native_response_unverified",
        "response_not_current",
        "response_completed",
        "native_close_unknown",
        "native_process_gone",
    }
)


def _safe_native_reason(reason_code: str | None, fallback: str) -> str:
    return reason_code if reason_code in _SAFE_NATIVE_REASON_CODES else fallback


class VerifiedNativeOpenFailure(Exception):
    """A native boundary proof that no process or session was created."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class _PendingStart:
    request: RunStart
    profile: ProfileRevision
    runtime: RuntimeSnapshot
    snapshot: RunSnapshot
    assembled_input: AssembledInput
    response_id: UUID


@dataclass(slots=True)
class _LiveRun:
    run_id: UUID
    workspace_id: UUID
    projection: LiveRunContext
    handle: NativeHandle
    drain_deadline: float | None = None


class RunExecution:
    def __init__(
        self,
        store,
        registry,
        authorizer: ExecutionAuthorizer,
        sources: ContextSourceReader,
        secrets: SecretReader,
        runtimes,
        owner_id: UUID,
        tool_connections: ToolConnectionReader | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._authorizer = authorizer
        self._sources = sources
        self._secrets = secrets
        self._runtimes = runtimes
        self._owner_id = owner_id
        self._tool_connections = tool_connections
        self._context = ContextAssembler()
        self._pending: dict[UUID, _PendingStart] = {}
        self._run_contexts: dict[UUID, _LiveRun] = {}
        self._open_handoffs: set[UUID] = set()
        self._unopened_runs: set[UUID] = set()
        self._opening_runs: set[UUID] = set()
        self._stop_cleanups: set[UUID] = set()
        # Store admission stays concurrent; poll waits until each result is published.
        self._pending_lock = Condition()
        self._admissions_in_progress: set[UUID] = set()
        self._closing = False
        self._native_workers = NativeWorkers()
        self._input_lock = Condition(RLock())
        self._input_admissions: dict[UUID, int] = {}
        self._closing_runs: set[UUID] = set()
        self._interrupt_operations: dict[tuple[UUID, UUID], InterruptReceipt] = {}
        self._in_flight_lock = RLock()
        self._in_flight_responses: set[UUID] = set()
        self._input_settings = InputDeliverySettings()
        self._monotonic = monotonic

    def start(self, request: RunStart) -> StartReceipt:
        run_id = self._begin_admission()
        try:
            return self._start(request, run_id)
        finally:
            self._end_admission(run_id)

    def _start(self, request: RunStart, run_id: UUID) -> StartReceipt:
        self._authorizer.require(request.grant_id, "run.start", request.profile_ref)
        operation = self._store.get_start_operation(
            request.profile_ref.workspace_id, request.operation_id
        )
        if operation is not None:
            if operation.profile_ref != request.profile_ref:
                self._authorizer.require(
                    request.grant_id, "run.start", operation.profile_ref
                )
                raise AgentError(
                    "operation_conflict",
                    "Operation ID was used with a different request.",
                )
            fingerprint = request_fingerprint(request, operation.profile_config_hash)
            if fingerprint != operation.fingerprint:
                raise AgentError(
                    "operation_conflict",
                    "Operation ID was used with a different request.",
                )
            if operation.receipt.status is not StartStatus.BUSY:
                return operation.receipt

        self._require_owner()

        profile = self._store.get_profile(request.profile_ref)
        if profile.ref != request.profile_ref:
            raise AgentError(
                "profile_reference_mismatch", "Profile reference is unavailable."
            )
        profile_status = self._store.get_profile_status(
            request.profile_ref.workspace_id, request.profile_ref.profile_id
        )
        if not profile_status.enabled:
            raise AgentError("configuration_invalid", "This profile is disabled.")

        validation = self._runtimes.validate_profile(
            request.profile_ref, grant_id=request.grant_id
        )
        if not validation.valid or validation.effective_preview is None:
            issue = validation.issues[0] if validation.issues else None
            raise AgentError(
                "configuration_invalid",
                "The selected profile is not executable.",
                None if issue is None else issue.field,
            )
        runtime = self._runtimes.get_runtime(
            profile.draft.runtime_ref, grant_id=request.grant_id
        )
        self._check_required_capabilities(request, runtime.capabilities)
        runtime_entry = self._store.get_runtime_entry(profile.draft.runtime_ref)

        assembled = self._context.assemble(profile, request, self._sources)
        fingerprint = request_fingerprint(request, profile.config_hash)
        response_id = uuid4()
        effective_config = EffectiveConfig.model_validate(
            validation.effective_preview.model_copy(
                update={"context_receipts": assembled.context_receipts}
            ).model_dump(mode="python", warnings=False)
        )
        snapshot = RunSnapshot(
            run_id=run_id,
            workspace_id=request.profile_ref.workspace_id,
            profile_ref=request.profile_ref,
            policy=request.policy,
            state=RunState.STARTING,
            input_open=False,
            active_response_id=response_id,
            effective_config=effective_config,
            last_event_sequence=0,
            capabilities=runtime.capabilities,
            reason_code=None,
        )
        pending = _PendingStart(
            request=request,
            profile=profile,
            runtime=runtime,
            snapshot=snapshot,
            assembled_input=assembled.input,
            response_id=response_id,
        )
        created = False
        conflict_error: AgentError | None = None
        stored_operation = None
        with self._input_lock:
            self._unopened_runs.add(run_id)
        try:
            try:
                receipt, created = self._store.admit_start(
                    owner_id=self._owner_id,
                    authorization_grant_id=request.grant_id,
                    profile_ref=request.profile_ref,
                    runtime_ref=profile.draft.runtime_ref,
                    installation_key=runtime_entry.record.installation_key,
                    operation_id=request.operation_id,
                    profile_config_hash=profile.config_hash,
                    fingerprint=fingerprint,
                    run_id=run_id,
                    snapshot=snapshot,
                )
            except AgentError as error:
                if error.code != "operation_conflict":
                    raise
                stored_operation = self._store.get_start_operation(
                    request.profile_ref.workspace_id, request.operation_id
                )
                conflict_error = error
            if conflict_error is not None:
                if (
                    stored_operation is not None
                    and stored_operation.profile_ref != request.profile_ref
                ):
                    self._authorizer.require(
                        request.grant_id, "run.start", stored_operation.profile_ref
                    )
                raise conflict_error
        finally:
            with self._pending_lock:
                if created:
                    self._pending[run_id] = pending
                else:
                    with self._input_lock:
                        self._unopened_runs.discard(run_id)
        return receipt

    def _begin_admission(self) -> UUID:
        with self._pending_lock:
            if self._closing:
                raise AgentError(
                    "shutdown_pending", "The execution service is closing."
                )
            identity = uuid4()
            self._admissions_in_progress.add(identity)
            return identity

    def _end_admission(self, identity: UUID) -> None:
        with self._pending_lock:
            self._admissions_in_progress.discard(identity)
            self._pending_lock.notify_all()

    def begin_close(self) -> None:
        with self._pending_lock:
            self._closing = True
            self._native_workers.begin_close()
            self._pending_lock.notify_all()

    def wait_until(self, deadline: float) -> bool:
        if not self._native_workers.wait_until(deadline):
            return False
        with self._pending_lock:
            while self._admissions_in_progress:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._pending_lock.wait(timeout=remaining)
        return True

    def get_start(
        self, workspace_id: UUID, operation_id: UUID, *, grant_id: UUID
    ) -> StartLookup:
        self._authorizer.require(grant_id, "run.read", workspace_id)
        receipt = self._store.get_start_receipt(workspace_id, operation_id)
        return StartLookup(found=receipt is not None, receipt=receipt)

    def get_run(
        self, workspace_id: UUID, run_id: UUID, *, grant_id: UUID
    ) -> RunSnapshot:
        self._authorizer.require(grant_id, "run.read", workspace_id)
        return self._store.get_run_snapshot(workspace_id, run_id)

    def send_input(self, request: RunInput) -> InputReceipt:
        identity = self._begin_admission()
        try:
            return self._send_input(request)
        finally:
            self._end_admission(identity)

    def _send_input(self, request: RunInput) -> InputReceipt:
        self._authorize_run_request(
            request.workspace_id, request.run_id, request.grant_id, "run.input"
        )
        self._require_owner()
        with self._input_lock:
            while (
                request.run_id in self._closing_runs
                or request.run_id in self._open_handoffs
            ):
                snapshot = self._store.get_run_snapshot(
                    request.workspace_id, request.run_id
                )
                if snapshot.state is not RunState.RUNNING:
                    receipt = self._admit_input(request)
                    if receipt.status is not InputStatus.ACCEPTED:
                        return receipt
                self._input_lock.wait()
            self._input_admissions[request.run_id] = (
                self._input_admissions.get(request.run_id, 0) + 1
            )
            live = self._run_contexts.get(request.run_id)
        try:
            self._store.get_run_snapshot(request.workspace_id, request.run_id)
            if live is None:
                self._store.fence_orphaned_runs(
                    owner_id=self._owner_id,
                    live_run_ids=self._owned_live_run_ids(),
                )
            else:
                self._reauthorize_input(live, request)
            receipt = self._admit_input(request)
            if receipt.status is InputStatus.ACCEPTED:
                with self._input_lock:
                    live = self._run_contexts.get(request.run_id)
                    if live is not None and live.drain_deadline is None:
                        live.drain_deadline = (
                            self._monotonic()
                            + self._input_settings.drain_deadline_seconds
                        )
            return receipt
        finally:
            with self._input_lock:
                remaining = self._input_admissions.get(request.run_id, 1) - 1
                if remaining <= 0:
                    self._input_admissions.pop(request.run_id, None)
                else:
                    self._input_admissions[request.run_id] = remaining
                self._input_lock.notify_all()

    def _admit_input(self, request: RunInput) -> InputReceipt:
        return self._store.admit_input(
            request,
            owner_id=self._owner_id,
            fingerprint=input_fingerprint(request),
            response_id=uuid4(),
            max_pending=self._input_settings.max_pending,
            max_input_bytes=self._input_settings.max_input_bytes,
            payload_bytes=input_size(request),
        )

    def get_input(
        self,
        workspace_id: UUID,
        run_id: UUID,
        operation_id: UUID,
        *,
        grant_id: UUID,
    ) -> InputReceipt:
        self._authorize_run_request(workspace_id, run_id, grant_id, "run.read")
        return self._store.get_input_receipt(workspace_id, run_id, operation_id)

    def interrupt(self, request: RunInterrupt) -> InterruptReceipt:
        identity = self._begin_admission()
        try:
            return self._interrupt(request)
        finally:
            self._end_admission(identity)

    def _interrupt(self, request: RunInterrupt) -> InterruptReceipt:
        self._authorize_run_request(
            request.workspace_id, request.run_id, request.grant_id, "run.interrupt"
        )
        self._require_owner()
        snapshot = self._store.get_run_snapshot(request.workspace_id, request.run_id)
        key = (request.run_id, request.operation_id)
        prior = self._interrupt_operations.get(key)
        if prior is not None:
            return prior
        if (
            snapshot.state is not RunState.RUNNING
            or snapshot.active_response_id != request.response_id
        ):
            return self._record_interrupt(
                request, InterruptStatus.FAILED, "response_not_current"
            )
        with self._input_lock:
            live = self._run_contexts.get(request.run_id)
        if live is None or "cancel_current_response" not in live.handle.capabilities:
            return self._record_interrupt(
                request, InterruptStatus.UNSUPPORTED, "cancel_not_negotiated"
            )
        harness = self._registry.get(live.projection.runtime.harness_id)
        try:
            outcome = harness.cancel(live.handle, request.response_id)
        except Exception:  # noqa: BLE001 - cancellation without confirmation is uncertain
            outcome = None
        if outcome is None or outcome.response_id != request.response_id:
            self._fence_run(live, "native_response_unknown")
            return self._record_interrupt(
                request, InterruptStatus.UNKNOWN, "native_response_unknown"
            )
        if outcome.status is NativeCancelState.CONFIRMED:
            return self._record_interrupt(request, InterruptStatus.INTERRUPTED, None)
        if outcome.status is NativeCancelState.ACCEPTED:
            return self._record_interrupt(request, InterruptStatus.ACCEPTED, None)
        if outcome.status is NativeCancelState.UNSUPPORTED:
            return self._record_interrupt(
                request, InterruptStatus.UNSUPPORTED, "cancel_not_negotiated"
            )
        if outcome.status is NativeCancelState.UNKNOWN:
            self._fence_run(
                live,
                _safe_native_reason(outcome.reason_code, "native_response_unknown"),
            )
            return self._record_interrupt(
                request, InterruptStatus.UNKNOWN, "native_response_unknown"
            )
        return self._record_interrupt(
            request,
            InterruptStatus.FAILED,
            _safe_native_reason(outcome.reason_code, "native_response_failed"),
        )

    def get_interrupt(
        self,
        workspace_id: UUID,
        run_id: UUID,
        operation_id: UUID,
        *,
        grant_id: UUID,
    ) -> InterruptReceipt:
        self._authorize_run_request(workspace_id, run_id, grant_id, "run.read")
        event = self._store.get_lifecycle_event(
            workspace_id, run_id, operation_id, "interrupt."
        )
        if event is None:
            raise AgentError("not_found", "Interrupt operation not found.")
        status = InterruptStatus(event.kind.removeprefix("interrupt."))
        return InterruptReceipt(
            operation_id=operation_id,
            run_id=run_id,
            response_id=event.response_id,
            status=status,
            reason_code=event.data.get("reason_code"),
            evidence_sequence=event.sequence,
        )

    def stop(self, request: RunStop) -> StopReceipt:
        identity = self._begin_admission()
        try:
            return self._stop(request)
        finally:
            self._end_admission(identity)

    def _stop(self, request: RunStop) -> StopReceipt:
        self._require_owner()
        self._require_original_grant(
            request.workspace_id, request.run_id, request.grant_id
        )
        with self._input_lock:
            event = self._store.request_stop(
                request.run_id, request.operation_id, owner_id=self._owner_id
            )
            if event.kind != "stop.accepted":
                return self._stop_receipt(request.operation_id, request.run_id, event)
            live = self._run_contexts.get(request.run_id)
            snapshot = self._store.get_run_snapshot(
                request.workspace_id, request.run_id
            )
            if request.run_id in self._unopened_runs:
                self._unopened_runs.discard(request.run_id)
                self._complete_pending_stops(snapshot, stopped=True, reason_code=None)
                return self._stop_receipt(request.operation_id, request.run_id, event)
            if live is None:
                handle = self._store.get_native_handle(
                    request.workspace_id, request.run_id
                )
                if handle is None and request.run_id in self._opening_runs:
                    return self._stop_receipt(
                        request.operation_id, request.run_id, event
                    )
            else:
                handle = None
        if live is not None:
            self._process_stop(live)
        else:
            if handle is not None:
                runtime_entry = self._store.get_runtime_entry(
                    snapshot.effective_config.runtime_ref
                )
                self._process_stop_handle(
                    snapshot,
                    handle,
                    self._registry.get(runtime_entry.record.harness_id),
                )
            else:
                self._complete_pending_stops(
                    snapshot,
                    stopped=False,
                    reason_code="native_close_unknown",
                )
        return self._stop_receipt(request.operation_id, request.run_id, event)

    def get_stop(
        self,
        workspace_id: UUID,
        run_id: UUID,
        operation_id: UUID,
        *,
        grant_id: UUID,
    ) -> StopReceipt:
        self._authorize_run_request(workspace_id, run_id, grant_id, "run.read")
        event = self._store.get_lifecycle_event(
            workspace_id, run_id, operation_id, "stop."
        )
        if event is None:
            raise AgentError("not_found", "Stop operation not found.")
        return self._stop_receipt(operation_id, run_id, event)

    def recover(self, request: RunRecover) -> RecoveryReceipt:
        identity = self._begin_admission()
        try:
            return self._recover(request)
        finally:
            self._end_admission(identity)

    def _recover(self, request: RunRecover) -> RecoveryReceipt:
        self._require_original_grant(
            request.workspace_id, request.run_id, request.grant_id
        )
        self._authorizer.require(request.grant_id, "run.recover", request.workspace_id)
        self._require_owner()
        snapshot = self._store.get_run_snapshot(request.workspace_id, request.run_id)
        prior = self._store.get_lifecycle_event(
            request.workspace_id, request.run_id, request.operation_id, "recovery."
        )
        if prior is not None:
            return RecoveryReceipt(
                operation_id=request.operation_id,
                run_id=request.run_id,
                status=RecoveryStatus(prior.kind.removeprefix("recovery.")),
                reason_code=prior.data.get("reason_code"),
            )
        with self._input_lock:
            live = self._run_contexts.get(request.run_id)
        if live is not None:
            self._reauthorize_pinned_scope(live, request.grant_id)
            stored_handle = self._store.get_native_handle(
                request.workspace_id, request.run_id
            )
            if (
                live.run_id != request.run_id
                or live.workspace_id != request.workspace_id
                or live.handle.run_id != request.run_id
                or stored_handle != live.handle
            ):
                self._fence_run(live, "native_identity_unverified")
                return self._record_recovery(
                    request, RecoveryStatus.UNKNOWN, "native_identity_unverified"
                )
            if snapshot.state in (RunState.UNKNOWN, RunState.CLOSING):
                return self._record_recovery(
                    request,
                    RecoveryStatus.UNKNOWN,
                    snapshot.reason_code or "native_response_unknown",
                )
            harness = self._registry.get(live.projection.runtime.harness_id)
            try:
                inspection = harness.inspect(live.handle)
            except Exception:  # noqa: BLE001 - failed identity query cannot resume a Run
                inspection = None
            response_matches = inspection is not None and (
                snapshot.state is RunState.RUNNING
                and snapshot.active_response_id is not None
                and inspection.response_state is NativeResponseState.ACTIVE
                or snapshot.state is RunState.IDLE
                and snapshot.active_response_id is None
                and inspection.response_state is NativeResponseState.IDLE
            )
            if (
                inspection is None
                or inspection.process_state is not NativeProcessState.ALIVE
                or not response_matches
            ):
                reason = (
                    "native_process_gone"
                    if inspection is not None
                    and inspection.process_state is NativeProcessState.GONE
                    else "native_response_unknown"
                )
                self._fence_run(live, reason)
                return self._record_recovery(request, RecoveryStatus.UNKNOWN, reason)
            return self._record_recovery(request, RecoveryStatus.RECOVERED, None)

        handle = self._store.get_native_handle(request.workspace_id, request.run_id)
        if handle is None or handle.run_id != request.run_id:
            return self._record_recovery(
                request, RecoveryStatus.UNKNOWN, "native_identity_unverified"
            )
        try:
            runtime_entry = self._store.get_runtime_entry(
                snapshot.effective_config.runtime_ref
            )
            harness = self._registry.get(runtime_entry.record.harness_id)
            inspection = harness.inspect(handle)
        except Exception:  # noqa: BLE001 - restart inspection cannot authorize a replacement
            inspection = None
        if inspection is None or inspection.process_state is NativeProcessState.UNKNOWN:
            return self._record_recovery(
                request, RecoveryStatus.UNKNOWN, "native_identity_unverified"
            )
        reason_code = (
            "session_load_unverified"
            if supports_session_load(handle.capabilities)
            else "session_load_unsupported"
        )
        return self._record_recovery(request, RecoveryStatus.UNSUPPORTED, reason_code)

    def _record_recovery(
        self,
        request: RunRecover,
        status: RecoveryStatus,
        reason_code: str | None,
    ) -> RecoveryReceipt:
        event = self._store.record_lifecycle_event(
            request.workspace_id,
            request.run_id,
            request.operation_id,
            None,
            "recovery." + status.value,
            reason_code,
            owner_id=self._owner_id,
        )
        return RecoveryReceipt(
            operation_id=request.operation_id,
            run_id=request.run_id,
            status=RecoveryStatus(event.kind.removeprefix("recovery.")),
            reason_code=event.data.get("reason_code"),
        )

    def _authorize_run_request(
        self, workspace_id: UUID, run_id: UUID, grant_id: UUID, action: str
    ) -> None:
        self._require_original_grant(workspace_id, run_id, grant_id)
        self._authorizer.require(grant_id, action, workspace_id)

    def _require_original_grant(
        self, workspace_id: UUID, run_id: UUID, grant_id: UUID
    ) -> None:
        try:
            original_grant = self._store.get_run_authorization_grant(
                workspace_id, run_id
            )
        except AgentError as error:
            if error.code == "not_found":
                raise AgentError("permission_denied", "Access denied.") from None
            raise
        if original_grant != grant_id:
            raise AgentError("permission_denied", "Access denied.")

    def _record_interrupt(
        self, request: RunInterrupt, status: InterruptStatus, reason_code: str | None
    ) -> InterruptReceipt:
        event = self._store.record_lifecycle_event(
            request.workspace_id,
            request.run_id,
            request.operation_id,
            request.response_id,
            "interrupt." + status.value,
            reason_code,
            owner_id=self._owner_id,
        )
        receipt = InterruptReceipt(
            operation_id=request.operation_id,
            run_id=request.run_id,
            response_id=request.response_id,
            status=status,
            reason_code=reason_code,
            evidence_sequence=event.sequence,
        )
        self._interrupt_operations[(request.run_id, request.operation_id)] = receipt
        return receipt

    @staticmethod
    def _stop_receipt(operation_id: UUID, run_id: UUID, event) -> StopReceipt:
        status = StopStatus(event.kind.removeprefix("stop."))
        return StopReceipt(
            operation_id=operation_id,
            run_id=run_id,
            status=status,
            reason_code=event.data.get("reason_code"),
            evidence_sequence=event.sequence,
        )

    def poll_once(self, *, _background: bool = False) -> int:
        self._require_owner()
        processed = 0
        with self._pending_lock:
            while self._admissions_in_progress:
                if _background:
                    return 0
                self._pending_lock.wait()
            pending_ids = set(self._pending) | set(self._native_workers.run_ids())
            for snapshot in self._store.list_starting_runs():
                if snapshot.run_id not in pending_ids:
                    unknown = snapshot.model_copy(
                        update={
                            "state": RunState.UNKNOWN,
                            "input_open": False,
                            "reason_code": "native_start_unknown",
                        }
                    )
                    self._transition_run(
                        snapshot.run_id,
                        unknown,
                        receipt_status=StartStatus.UNKNOWN,
                    )
                    processed += 1
            pending = tuple(
                self._pending[run_id] for run_id in sorted(self._pending, key=str)
            )
        fenced = self._store.fence_orphaned_runs(
            owner_id=self._owner_id,
            live_run_ids=self._owned_live_run_ids(),
        )
        processed += fenced
        for item in pending:
            with self._pending_lock:
                if self._closing or self._native_workers.busy(item.snapshot.run_id):
                    continue
                if _background:
                    try:
                        dispatched = self._dispatch_native(
                            item.snapshot.workspace_id,
                            item.snapshot.run_id,
                            lambda item=item: self._process_pending(item),
                        )
                    except Exception:  # noqa: BLE001 - Thread.start proves no callback ran
                        self._pending.pop(item.snapshot.run_id, None)
                        self._transition_run(
                            item.snapshot.run_id,
                            item.snapshot.model_copy(
                                update={
                                    "state": RunState.FAILED,
                                    "reason_code": "native_start_failed",
                                }
                            ),
                            receipt_status=StartStatus.REJECTED,
                            active_reserved=False,
                            session_reserved=False,
                        )
                        with self._input_lock:
                            self._unopened_runs.discard(item.snapshot.run_id)
                        processed += 1
                        continue
                    if not dispatched:
                        continue
                self._pending.pop(item.snapshot.run_id, None)
            if not _background:
                self._process_pending(item)
            processed += 1
        with self._input_lock:
            live_runs = tuple(self._run_contexts.values())
        for live in live_runs:
            if self._native_workers.busy(live.run_id):
                continue
            snapshot = self._store.get_run_snapshot(live.workspace_id, live.run_id)
            if snapshot.state in (
                RunState.RUNNING,
                RunState.IDLE,
            ) and not self._response_is_in_flight(live.run_id):
                with self._pending_lock:
                    if self._closing:
                        continue
                    if _background:
                        if self._store.list_pending_inputs(live.run_id):
                            try:
                                self._dispatch_native(
                                    live.workspace_id,
                                    live.run_id,
                                    lambda live=live: self._drain_inputs(live),
                                )
                            except Exception:  # noqa: BLE001 - no native input was dispatched
                                self._fail_input_dispatch(live)
                        continue
                self._drain_inputs(live)
        for (
            snapshot,
            handle,
            terminal_state,
            terminal_reason_code,
        ) in self._store.list_unresolved_runs():
            if self._native_workers.busy(snapshot.run_id):
                continue
            self._inspect_unresolved(
                snapshot, handle, terminal_state, terminal_reason_code
            )
        return processed

    def _dispatch_native(
        self, workspace_id: UUID, run_id: UUID, work: Callable[[], None]
    ) -> bool:
        def run() -> None:
            try:
                self._require_owner()
                work()
            except Exception:
                if self._store.execution_owner_is_held(self._owner_id):
                    snapshot = self._store.get_run_snapshot(workspace_id, run_id)
                    if snapshot.state is RunState.STARTING:
                        self._mark_start_unknown(snapshot, "native_start_unknown")
                    elif snapshot.state is RunState.RUNNING:
                        self._store.fence_run(
                            workspace_id,
                            run_id,
                            "native_response_unknown",
                            owner_id=self._owner_id,
                        )
                raise

        return self._native_workers.dispatch(run_id, run)

    def _fail_input_dispatch(self, live: _LiveRun) -> None:
        snapshot = self._store.get_run_snapshot(live.workspace_id, live.run_id)
        for operation in self._store.list_pending_inputs(live.run_id):
            self._store.finish_input(
                live.workspace_id,
                live.run_id,
                operation.operation_id,
                InputStatus.FAILED,
                "native_response_failed",
                owner_id=self._owner_id,
            )
        if snapshot.policy is RunPolicy.CONVERSATION:
            with self._input_lock:
                live.drain_deadline = None
            if snapshot.active_response_id is not None:
                self._store.finish_conversation_response(
                    live.run_id, snapshot.active_response_id, owner_id=self._owner_id
                )
        elif snapshot.active_response_id is not None:
            self._store.close_input_admission(
                live.run_id,
                snapshot.active_response_id,
                owner_id=self._owner_id,
                terminal_state=RunState.FAILED,
                reason_code="native_response_failed",
            )

    def _process_pending(self, item: _PendingStart) -> None:
        snapshot = item.snapshot
        with self._input_lock:
            self._open_handoffs.add(snapshot.run_id)
        published = False
        try:
            try:
                open_request = self._build_open_request(item)
                harness = self._registry.get(item.runtime.harness_id)
            except AgentError as error:
                self._require_owner()
                self._reject_projection(snapshot, error.code)
                return
            except Exception:  # noqa: BLE001 - projection errors reject only this admitted run
                self._require_owner()
                self._reject_projection(snapshot, "run_projection_failed")
                return

            with self._input_lock:
                self._require_owner()
                current = self._store.get_run_snapshot(
                    snapshot.workspace_id, snapshot.run_id
                )
                if (
                    current.state is not RunState.STARTING
                    or current.active_response_id != item.response_id
                ):
                    return
                # Stop and native creation share this short claim, never native I/O.
                self._unopened_runs.discard(snapshot.run_id)
                self._opening_runs.add(snapshot.run_id)
            try:
                handle = harness.open(open_request)
            except VerifiedNativeOpenFailure as error:
                failed = snapshot.model_copy(
                    update={
                        "state": RunState.FAILED,
                        "reason_code": _safe_native_reason(
                            error.reason_code, "native_start_failed"
                        ),
                    }
                )
                with self._input_lock:
                    if not self._complete_pending_stops(
                        snapshot, stopped=True, reason_code=None
                    ):
                        self._transition_run(
                            snapshot.run_id,
                            failed,
                            receipt_status=StartStatus.REJECTED,
                            active_reserved=False,
                            session_reserved=False,
                        )
                return
            except Exception:  # noqa: BLE001 - an ambiguous native open must not be retried
                with self._input_lock:
                    if not self._complete_pending_stops(
                        snapshot, stopped=False, reason_code="native_start_unknown"
                    ):
                        self._mark_start_unknown(snapshot, "native_start_unknown")
                return

            self._require_owner()
            if handle.run_id != snapshot.run_id:
                with self._input_lock:
                    if not self._complete_pending_stops(
                        snapshot,
                        stopped=False,
                        reason_code="native_identity_unverified",
                    ):
                        self._mark_start_unknown(snapshot, "native_identity_unverified")
                return
            self._transition_run(
                snapshot.run_id,
                snapshot.model_copy(
                    update={"state": RunState.RUNNING, "input_open": True}
                ),
                native_handle=handle,
                replace_native_handle=True,
            )
            live = _LiveRun(
                run_id=snapshot.run_id,
                workspace_id=snapshot.workspace_id,
                projection=LiveRunContext.from_start(
                    item.request, item.profile, item.runtime
                ),
                handle=handle,
            )
            with self._input_lock:
                current = self._store.get_run_snapshot(live.workspace_id, live.run_id)
                stopping = current.state is RunState.CLOSING
                if (
                    current.state is not RunState.RUNNING
                    or current.active_response_id != item.response_id
                ):
                    if not stopping:
                        return
                else:
                    self._run_contexts[snapshot.run_id] = live
                    self._open_handoffs.discard(snapshot.run_id)
                    self._input_lock.notify_all()
                    published = True
            if stopping:
                self._process_stop_handle(current, handle, harness)
                return
        finally:
            with self._input_lock:
                self._unopened_runs.discard(snapshot.run_id)
                self._opening_runs.discard(snapshot.run_id)
                if not published:
                    self._open_handoffs.discard(snapshot.run_id)
                    self._input_lock.notify_all()

        if not self._begin_initial_prompt(live, item.response_id):
            return
        try:
            outcome = harness.prompt(handle, item.assembled_input, item.response_id)
        except Exception:  # noqa: BLE001 - a lost native response remains uncertain
            self._require_owner()
            self._finish_unknown_response(
                live, item.response_id, "native_response_unknown"
            )
            return
        finally:
            self._mark_response_in_flight(snapshot.run_id, False)
        self._require_owner()
        if outcome.response_id != item.response_id:
            self._finish_unknown_response(
                live, item.response_id, "native_response_unverified"
            )
        elif outcome.status is NativeOutcomeStatus.UNKNOWN:
            self._finish_unknown_response(
                live,
                item.response_id,
                _safe_native_reason(outcome.reason_code, "native_response_unknown"),
            )
        elif outcome.status in (
            NativeOutcomeStatus.COMPLETED,
            NativeOutcomeStatus.CANCELLED,
        ):
            self._finish_response(
                live,
                item.response_id,
                cancelled=outcome.status is NativeOutcomeStatus.CANCELLED,
                observed_model=outcome.observed_model,
            )
        else:
            self._fail_response(
                live,
                item.response_id,
                _safe_native_reason(outcome.reason_code, "native_response_failed"),
            )

    def _begin_initial_prompt(self, live: _LiveRun, response_id: UUID) -> bool:
        with self._input_lock:
            self._require_owner()
            current = self._store.get_run_snapshot(live.workspace_id, live.run_id)
            if (
                current.state is not RunState.RUNNING
                or current.active_response_id != response_id
            ):
                return False
            self._mark_response_in_flight(live.run_id, True)
            return True

    def _owned_live_run_ids(self) -> tuple[UUID, ...]:
        with self._input_lock:
            return tuple(
                set(self._run_contexts)
                | self._open_handoffs
                | set(self._native_workers.run_ids())
            )

    def _response_is_in_flight(self, run_id: UUID) -> bool:
        with self._in_flight_lock:
            return run_id in self._in_flight_responses

    def _finish_response(
        self,
        live: _LiveRun,
        response_id: UUID,
        *,
        cancelled: bool = False,
        observed_model: str | None = None,
        operation_id: UUID | None = None,
    ) -> None:
        run_id = live.run_id
        snapshot = self._store.get_run_snapshot(live.workspace_id, run_id)
        if snapshot.state in (RunState.CLOSING, RunState.STOPPED, RunState.UNKNOWN):
            return
        if snapshot.active_response_id != response_id:
            return
        if snapshot.policy is RunPolicy.CONVERSATION:
            snapshot = self._store.finish_conversation_response(
                run_id,
                response_id,
                owner_id=self._owner_id,
                cancelled=cancelled,
                observed_model=observed_model,
            )
            self._drain_inputs(live)
            return

        snapshot = self._close_after_response(
            live,
            response_id,
            terminal_state=RunState.COMPLETED,
            reason_code=None,
        )
        if snapshot.state is RunState.RUNNING:
            self._drain_inputs(live)

    def _close_after_response(
        self,
        live: _LiveRun,
        response_id: UUID,
        *,
        terminal_state: RunState,
        reason_code: str | None,
    ) -> RunSnapshot:
        run_id = live.run_id
        with self._input_lock:
            self._closing_runs.add(run_id)
            while self._input_admissions.get(run_id, 0):
                self._input_lock.wait()
            try:
                snapshot = self._store.close_input_admission(
                    run_id,
                    response_id,
                    owner_id=self._owner_id,
                    terminal_state=terminal_state,
                    reason_code=reason_code,
                )
            finally:
                self._closing_runs.discard(run_id)
                self._input_lock.notify_all()
        if snapshot.state is not RunState.CLOSING:
            return snapshot
        self._close_live_session(live, terminal_state, reason_code)
        return self._store.get_run_snapshot(live.workspace_id, run_id)

    def _drain_inputs(self, live: _LiveRun) -> None:
        run_id = live.run_id
        workspace_id = live.workspace_id
        while True:
            self._require_owner()
            current = self._store.get_run_snapshot(workspace_id, run_id)
            if current.state not in (RunState.RUNNING, RunState.IDLE) or (
                current.state is RunState.RUNNING and current.active_response_id is None
            ):
                return
            pending = self._store.list_pending_inputs(run_id)
            now = self._monotonic()
            if (
                pending
                and live.drain_deadline is not None
                and now >= live.drain_deadline
            ):
                if current.policy is RunPolicy.CONVERSATION:
                    live.drain_deadline = None
                for item in pending:
                    self._store.finish_input(
                        item.workspace_id,
                        run_id,
                        item.operation_id,
                        InputStatus.FAILED,
                        "input_drain_deadline",
                        owner_id=self._owner_id,
                    )
                if current.policy is RunPolicy.BOUNDED:
                    self._close_after_response(
                        live,
                        current.active_response_id,
                        terminal_state=RunState.COMPLETED,
                        reason_code=None,
                    )
                elif current.active_response_id is not None:
                    self._store.finish_conversation_response(
                        run_id,
                        current.active_response_id,
                        owner_id=self._owner_id,
                    )
                return
            if current.state is RunState.IDLE:
                if not pending or not self._store.activate_pending_input(
                    run_id, pending[0].operation_id, owner_id=self._owner_id
                ):
                    return
                current = self._store.get_run_snapshot(workspace_id, run_id)
            if (
                current.state is not RunState.RUNNING
                or current.active_response_id is None
            ):
                return
            pending = self._store.list_pending_inputs(run_id)
            if not pending:
                if current.policy is RunPolicy.CONVERSATION:
                    live.drain_deadline = None
                    self._store.finish_conversation_response(
                        run_id,
                        current.active_response_id,
                        owner_id=self._owner_id,
                    )
                else:
                    self._close_after_response(
                        live,
                        current.active_response_id,
                        terminal_state=RunState.COMPLETED,
                        reason_code=None,
                    )
                return
            operation = pending[0]
            claimed = self._store.claim_input(
                run_id, operation.operation_id, owner_id=self._owner_id
            )
            if claimed is None or claimed.payload is None:
                return
            response_id = claimed.response_id
            try:
                self._reauthorize_input(live, claimed.payload)
                projection = live.projection.request_for_input(
                    claimed.operation_id,
                    claimed.payload.text,
                    claimed.payload.context,
                )
                assembled = self._context.assemble(
                    live.projection.profile, projection, self._sources
                ).input
            except AgentError as error:
                self._store.finish_input(
                    workspace_id,
                    run_id,
                    claimed.operation_id,
                    InputStatus.FAILED,
                    error.code
                    if error.code
                    in {
                        "permission_denied",
                        "context_required_missing",
                        "context_version_mismatch",
                        "context_hash_mismatch",
                    }
                    else "input_projection_failed",
                    owner_id=self._owner_id,
                )
                self._after_failed_input(live, response_id)
                continue
            except Exception:  # noqa: BLE001 - context projections never expose internals
                self._store.finish_input(
                    workspace_id,
                    run_id,
                    claimed.operation_id,
                    InputStatus.FAILED,
                    "input_projection_failed",
                    owner_id=self._owner_id,
                )
                self._after_failed_input(live, response_id)
                continue
            self._require_owner()
            if not self._begin_initial_prompt(live, response_id):
                return
            try:
                harness = self._registry.get(live.projection.runtime.harness_id)
                outcome = harness.prompt(live.handle, assembled, response_id)
            except Exception:  # noqa: BLE001 - unknown ACK must never be resent
                self._mark_response_in_flight(run_id, False)
                self._require_owner()
                self._store.finish_input(
                    workspace_id,
                    run_id,
                    claimed.operation_id,
                    InputStatus.UNKNOWN,
                    "native_response_unknown",
                    owner_id=self._owner_id,
                )
                self._fence_run(live, "native_response_unknown")
                return
            finally:
                self._mark_response_in_flight(run_id, False)
            self._require_owner()
            if (
                outcome.response_id != response_id
                or outcome.status is NativeOutcomeStatus.UNKNOWN
            ):
                reason = (
                    "native_response_unverified"
                    if outcome.response_id != response_id
                    else _safe_native_reason(
                        outcome.reason_code, "native_response_unknown"
                    )
                )
                self._store.finish_input(
                    workspace_id,
                    run_id,
                    claimed.operation_id,
                    InputStatus.UNKNOWN,
                    reason,
                    owner_id=self._owner_id,
                )
                self._fence_run(live, reason)
                return
            if outcome.status is NativeOutcomeStatus.FAILED:
                self._store.finish_input(
                    workspace_id,
                    run_id,
                    claimed.operation_id,
                    InputStatus.FAILED,
                    _safe_native_reason(outcome.reason_code, "native_response_failed"),
                    owner_id=self._owner_id,
                )
                self._fail_response(
                    live,
                    response_id,
                    _safe_native_reason(outcome.reason_code, "native_response_failed"),
                    operation_id=claimed.operation_id,
                )
                return
            self._store.finish_input(
                workspace_id,
                run_id,
                claimed.operation_id,
                InputStatus.DELIVERED,
                None,
                owner_id=self._owner_id,
            )
            if live.projection.request.policy is RunPolicy.CONVERSATION:
                snapshot = self._store.finish_conversation_response(
                    run_id,
                    response_id,
                    owner_id=self._owner_id,
                    cancelled=outcome.status is NativeOutcomeStatus.CANCELLED,
                    observed_model=outcome.observed_model,
                )
            else:
                snapshot = self._close_after_response(
                    live,
                    response_id,
                    terminal_state=RunState.COMPLETED,
                    reason_code=None,
                )
            if snapshot.state is RunState.IDLE:
                continue
            if snapshot.state in (
                RunState.COMPLETED,
                RunState.CLOSING,
                RunState.UNKNOWN,
                RunState.STOPPED,
            ):
                return

    def _after_failed_input(self, live: _LiveRun, response_id: UUID) -> None:
        if live.projection.request.policy is RunPolicy.CONVERSATION:
            snapshot = self._store.finish_conversation_response(
                live.run_id, response_id, owner_id=self._owner_id
            )
        else:
            snapshot = self._close_after_response(
                live, response_id, terminal_state=RunState.COMPLETED, reason_code=None
            )
        if snapshot.state in (RunState.IDLE, RunState.CLOSING, RunState.COMPLETED):
            return

    def _reauthorize_input(self, live: _LiveRun, request: RunInput) -> None:
        self._require_original_grant(
            request.workspace_id, request.run_id, request.grant_id
        )
        self._authorizer.require(request.grant_id, "run.input", request.workspace_id)
        self._reauthorize_pinned_scope(live, request.grant_id)

    def _reauthorize_pinned_scope(self, live: _LiveRun, grant_id: UUID) -> None:
        self._check_required_capabilities(
            live.projection.request, live.projection.runtime.capabilities
        )
        for binding in live.projection.request.tools:
            self._authorizer.require(grant_id, "tool.execute", binding)
        for resource_ref in live.projection.profile.draft.resource_refs:
            self._authorizer.require(grant_id, "resource.read", resource_ref)

    def _mark_response_in_flight(self, run_id: UUID, active: bool) -> None:
        with self._in_flight_lock:
            if active:
                self._in_flight_responses.add(run_id)
            else:
                self._in_flight_responses.discard(run_id)

    def _fence_run(self, live: _LiveRun, reason_code: str) -> None:
        self._store.fence_run(
            live.workspace_id,
            live.run_id,
            reason_code,
            owner_id=self._owner_id,
        )

    def _finish_unknown_response(
        self, live: _LiveRun, response_id: UUID, reason: str
    ) -> None:
        snapshot = self._store.get_run_snapshot(live.workspace_id, live.run_id)
        if snapshot.state in (RunState.CLOSING, RunState.STOPPED, RunState.UNKNOWN):
            return
        if snapshot.active_response_id != response_id:
            return
        self._fence_run(live, reason)

    def _fail_response(
        self,
        live: _LiveRun,
        response_id: UUID,
        reason: str,
        *,
        operation_id: UUID | None = None,
    ) -> None:
        run_id = live.run_id
        if operation_id is not None:
            self._store.finish_input(
                live.workspace_id,
                run_id,
                operation_id,
                InputStatus.FAILED,
                reason,
                owner_id=self._owner_id,
            )
        self._close_after_response(
            live, response_id, terminal_state=RunState.FAILED, reason_code=reason
        )

    def _close_live_session(
        self, live: _LiveRun, terminal_state: RunState, reason_code: str | None
    ) -> None:
        run_id = live.run_id
        workspace_id = live.workspace_id
        harness = self._registry.get(live.projection.runtime.harness_id)
        close_reason = None
        session_reserved = True
        try:
            result = harness.close(live.handle)
            if result.status is NativeCloseState.CLOSED:
                session_reserved = False
            else:
                close_reason = _safe_native_reason(
                    result.reason_code, "native_close_unknown"
                )
        except Exception:  # noqa: BLE001 - uncertain close retains native capacity
            close_reason = "native_close_unknown"
        self._transition_run(
            run_id,
            self._store.get_run_snapshot(workspace_id, run_id).model_copy(
                update={
                    "state": RunState.CLOSING if session_reserved else terminal_state,
                    "input_open": False,
                    "active_response_id": None,
                    "reason_code": close_reason if session_reserved else reason_code,
                }
            ),
            native_handle=live.handle,
            replace_native_handle=True,
            active_reserved=False,
            session_reserved=session_reserved,
            terminal_state=terminal_state if session_reserved else None,
            terminal_reason_code=reason_code if session_reserved else None,
        )
        if not session_reserved:
            with self._input_lock:
                self._run_contexts.pop(run_id, None)

    def _pending_stop_ids(self, snapshot: RunSnapshot) -> tuple[UUID, ...]:
        latest = {
            event.operation_id: event
            for event in self._store.get_lifecycle_events(
                snapshot.workspace_id, snapshot.run_id
            )
            if event.kind.startswith("stop.")
        }
        return tuple(
            operation_id
            for operation_id, event in latest.items()
            if operation_id is not None and event.kind == "stop.accepted"
        )

    def _complete_pending_stops(
        self, snapshot: RunSnapshot, *, stopped: bool, reason_code: str | None
    ) -> bool:
        with self._input_lock:
            operation_ids = self._pending_stop_ids(snapshot)
            for operation_id in operation_ids:
                self._store.complete_stop(
                    snapshot.run_id,
                    operation_id,
                    owner_id=self._owner_id,
                    stopped=stopped,
                    reason_code=reason_code,
                )
            return bool(operation_ids)

    def _process_stop(self, live: _LiveRun) -> None:
        snapshot = self._store.get_run_snapshot(live.workspace_id, live.run_id)
        harness = self._registry.get(live.projection.runtime.harness_id)
        self._process_stop_handle(snapshot, live.handle, harness)

    def _process_stop_handle(self, snapshot, handle, harness) -> None:
        with self._input_lock:
            if snapshot.run_id in self._stop_cleanups or not self._pending_stop_ids(
                snapshot
            ):
                return
            self._stop_cleanups.add(snapshot.run_id)
        result = None
        try:
            result = self._close_stop_handle(snapshot, handle, harness)
        finally:
            with self._input_lock:
                try:
                    if result is not None:
                        stopped, reason = result
                        self._complete_pending_stops(
                            snapshot, stopped=stopped, reason_code=reason
                        )
                        if stopped:
                            self._run_contexts.pop(snapshot.run_id, None)
                finally:
                    self._stop_cleanups.discard(snapshot.run_id)

    def _close_stop_handle(self, snapshot, handle, harness) -> tuple[bool, str | None]:
        reason = None
        stopped = False
        if handle.run_id != snapshot.run_id:
            return False, "native_identity_unverified"
        if (
            snapshot.active_response_id is not None
            and "cancel_current_response" in handle.capabilities
        ):
            try:
                harness.cancel(handle, snapshot.active_response_id)
            except Exception:  # noqa: BLE001, S110 - owned termination remains authoritative
                pass
        try:
            close_result = harness.close(handle)
        except Exception:  # noqa: BLE001 - inspect remains the authority after close errors
            close_result = None
        if close_result is not None and close_result.status is NativeCloseState.CLOSED:
            stopped = True
        else:
            try:
                inspection = harness.inspect(handle)
            except Exception:  # noqa: BLE001 - unknown inspection retains reservation
                inspection = None
            if (
                inspection is not None
                and inspection.process_state is NativeProcessState.GONE
            ):
                stopped = True
            else:
                reason = "native_close_unknown"
        return stopped, reason

    def _close_terminal(
        self,
        item: _PendingStart,
        handle: NativeHandle,
        state: RunState,
        reason_code: str | None,
    ) -> None:
        if reason_code is not None:
            reason_code = _safe_native_reason(reason_code, "native_response_failed")
        snapshot = item.snapshot.model_copy(
            update={
                "state": state,
                "input_open": False,
                "active_response_id": None,
                "reason_code": reason_code,
            }
        )
        harness = self._registry.get(item.runtime.harness_id)
        session_reserved = True
        try:
            close_result = harness.close(handle)
        except Exception:  # noqa: BLE001 - uncertain close retains the session reservation
            close_reason = "native_close_unknown"
        else:
            if close_result.status is NativeCloseState.CLOSED:
                session_reserved = False
                close_reason = None
            else:
                close_reason = _safe_native_reason(
                    close_result.reason_code, "native_close_unknown"
                )
        terminal_state = None if not session_reserved else state
        if session_reserved:
            snapshot = snapshot.model_copy(
                update={
                    "state": RunState.CLOSING,
                    "reason_code": close_reason,
                }
            )
        else:
            snapshot = snapshot.model_copy(update={"reason_code": reason_code})
        self._transition_run(
            item.snapshot.run_id,
            snapshot,
            native_handle=handle,
            replace_native_handle=True,
            active_reserved=False,
            session_reserved=session_reserved,
            terminal_state=terminal_state,
            terminal_reason_code=reason_code if terminal_state is not None else None,
        )

    def _inspect_unresolved(
        self,
        snapshot: RunSnapshot,
        handle: NativeHandle,
        terminal_state: RunState | None,
        terminal_reason_code: str | None,
    ) -> None:
        if snapshot.state is RunState.RUNNING:
            snapshot = snapshot.model_copy(
                update={
                    "state": RunState.UNKNOWN,
                    "input_open": False,
                    "reason_code": "native_response_unknown",
                }
            )
            self._transition_run(snapshot.run_id, snapshot)
        try:
            runtime_entry = self._store.get_runtime_entry(
                snapshot.effective_config.runtime_ref
            )
            harness = self._registry.get(runtime_entry.record.harness_id)
        except AgentError:
            return
        try:
            inspection = harness.inspect(handle)
        except Exception:  # noqa: BLE001 - failed inspection cannot prove process exit
            return
        if inspection.process_state is NativeProcessState.GONE:
            if snapshot.state is RunState.CLOSING and terminal_state is not None:
                reconciled = snapshot.model_copy(
                    update={
                        "state": terminal_state,
                        "input_open": False,
                        "active_response_id": None,
                        "reason_code": terminal_reason_code,
                    }
                )
            else:
                inspection_reason = (
                    None
                    if inspection.reason_code is None
                    else _safe_native_reason(
                        inspection.reason_code, "native_process_gone"
                    )
                )
                reconciled = snapshot.model_copy(
                    update={
                        "active_response_id": None,
                        "reason_code": inspection_reason
                        or snapshot.reason_code
                        or "native_process_gone",
                    }
                )
            self._transition_run(
                snapshot.run_id,
                reconciled,
                active_reserved=False,
                session_reserved=False,
            )
        elif inspection.response_state is NativeResponseState.IDLE:
            self._transition_run(
                snapshot.run_id,
                snapshot.model_copy(update={"active_response_id": None}),
                active_reserved=False,
            )

    def _mark_start_unknown(self, snapshot: RunSnapshot, reason_code: str) -> None:
        unknown = snapshot.model_copy(
            update={
                "state": RunState.UNKNOWN,
                "input_open": False,
                "reason_code": reason_code,
            }
        )
        self._transition_run(
            snapshot.run_id,
            unknown,
            receipt_status=StartStatus.UNKNOWN,
        )

    def _build_open_request(self, item: _PendingStart) -> NativeOpenRequest:
        for binding in item.request.tools:
            self._authorizer.require(item.request.grant_id, "tool.execute", binding)

        environment: dict[str, object] = dict(item.profile.draft.environment)
        for binding in item.profile.draft.secret_refs:
            try:
                environment[binding.destination] = self._secrets.read(
                    item.request.grant_id, binding
                )
            except AgentError:
                raise
            except Exception:  # noqa: BLE001 - never expose secret-reader exception details
                raise AgentError(
                    "secret_unavailable", "Authorized secret is unavailable."
                ) from None

        tools = self._resolve_tools(item.request, item.profile)
        projected_config: dict[str, object] = dict(item.profile.draft.native_settings)
        if item.profile.draft.model_id is not None:
            projected_config["model_id"] = item.profile.draft.model_id
        if item.profile.draft.thinking is not None:
            projected_config["thinking"] = item.profile.draft.thinking
        return NativeOpenRequest(
            run_id=item.snapshot.run_id,
            runtime=item.runtime,
            projected_config=projected_config,
            workspace=item.request.workspace,
            tools=tools,
            skills=self._resolve_skills(item.request, item.profile),
            environment=environment,
        )

    def _reject_projection(self, snapshot: RunSnapshot, reason_code: str) -> None:
        safe_codes = {
            "configuration_invalid",
            "permission_denied",
            "secret_unavailable",
            "tool_connection_unavailable",
        }
        reason = reason_code if reason_code in safe_codes else "run_projection_failed"
        failed = snapshot.model_copy(
            update={"state": RunState.FAILED, "reason_code": reason}
        )
        self._transition_run(
            snapshot.run_id,
            failed,
            receipt_status=StartStatus.REJECTED,
            active_reserved=False,
            session_reserved=False,
        )

    def _resolve_tools(self, request: RunStart, profile) -> tuple[ToolConnection, ...]:
        resolved: list[ToolConnection] = []
        for binding in request.tools:
            if binding.connection_id is not None:
                if self._tool_connections is None:
                    raise AgentError(
                        "tool_connection_unavailable",
                        "Tool connection access is unavailable.",
                    )
                connection = self._tool_connections.resolve(
                    request.grant_id, binding.connection_id
                )
                resolved.append(connection)
                continue

            ref = binding.resource_ref
            if ref is None or ref not in profile.draft.resource_refs:
                raise AgentError(
                    "configuration_invalid",
                    "Tool resource is not pinned to this profile.",
                )
            if ref.workspace_id != profile.ref.workspace_id:
                raise AgentError("permission_denied", "Access denied.")
            self._authorizer.require(request.grant_id, "resource.read", ref)
            resource = self._store.get_resource(ref)
            status = self._store.get_resource_status(ref.workspace_id, ref.resource_id)
            if not status.enabled or resource.draft.kind is not ResourceKind.MCP:
                raise AgentError(
                    "configuration_invalid", "Tool resource is unavailable."
                )
            settings = resource.draft.settings
            if "connection_id" in settings:
                if self._tool_connections is None:
                    raise AgentError(
                        "tool_connection_unavailable",
                        "Tool connection access is unavailable.",
                    )
                resolved.append(
                    self._tool_connections.resolve(
                        request.grant_id, UUID(str(settings["connection_id"]))
                    )
                )
            elif settings.get("transport") == "stdio":
                resolved.append(
                    ToolConnection(
                        name=resource.draft.name,
                        transport="stdio",
                        endpoint_or_argv=tuple(settings["command"]),
                        headers={},
                        environment={
                            key: SecretStr(value)
                            for key, value in settings.get("environment", {}).items()
                        },
                    )
                )
            else:
                resolved.append(
                    ToolConnection(
                        name=resource.draft.name,
                        transport="http",
                        endpoint_or_argv=str(settings["url"]),
                        headers={
                            key: SecretStr(value)
                            for key, value in settings.get("headers", {}).items()
                        },
                        environment={},
                    )
                )
        return tuple(resolved)

    def _resolve_skills(
        self, request: RunStart, profile: ProfileRevision
    ) -> tuple[NativeSkill, ...]:
        skills: list[NativeSkill] = []
        for ref in profile.draft.resource_refs:
            if ref.workspace_id != profile.ref.workspace_id:
                raise AgentError("permission_denied", "Access denied.")
            self._authorizer.require(request.grant_id, "resource.read", ref)
            resource = self._store.get_resource(ref)
            status = self._store.get_resource_status(ref.workspace_id, ref.resource_id)
            if resource.draft.kind is not ResourceKind.SKILL:
                continue
            if not status.enabled:
                raise AgentError(
                    "configuration_invalid",
                    "Pinned skill resource is disabled.",
                    "resource_refs",
                )
            if resource.draft.secret_refs:
                raise AgentError(
                    "configuration_invalid",
                    "Skill resources cannot bind native secrets.",
                    "resource_refs",
                )
            settings = resource.draft.settings
            if set(settings) == {"body"} and isinstance(settings.get("body"), str):
                content = settings["body"]
                digest = "sha256:" + sha256(content.encode("utf-8")).hexdigest()
            elif set(settings) == {"source_ref", "content_hash"}:
                try:
                    source_ref = SourceRef.model_validate(settings["source_ref"])
                    source = self._sources.read(request.grant_id, source_ref)
                except Exception:  # noqa: BLE001 - source diagnostics may contain private data
                    raise AgentError(
                        "configuration_invalid",
                        "Pinned skill content is unavailable.",
                        "resource_refs",
                    ) from None
                expected_hash = settings["content_hash"]
                if (
                    not isinstance(source, SourceContent)
                    or source.actual_version != source_ref.version
                    or source.content_hash != expected_hash
                ):
                    raise AgentError(
                        "configuration_invalid",
                        "Pinned skill content could not be verified.",
                        "resource_refs",
                    )
                try:
                    digest = "sha256:" + sha256(source.text.encode("utf-8")).hexdigest()
                except UnicodeEncodeError:
                    digest = ""
                if digest != expected_hash:
                    raise AgentError(
                        "configuration_invalid",
                        "Pinned skill content could not be verified.",
                        "resource_refs",
                    )
                content = source.text
            else:
                raise AgentError(
                    "configuration_invalid",
                    "Pinned skill settings are invalid.",
                    "resource_refs",
                )
            skills.append(
                NativeSkill(
                    resource_ref=ref,
                    name=resource.draft.name,
                    content=content,
                    content_hash=digest,
                )
            )
        return tuple(skills)

    @staticmethod
    def _check_required_capabilities(
        request: RunStart, capabilities: dict[str, CapabilitySupport]
    ) -> None:
        for name in request.required_capabilities:
            support = capabilities.get(name)
            if support is None or support.status is not SupportStatus.SUPPORTED:
                raise AgentError(
                    "configuration_invalid",
                    "A required runtime capability is unavailable.",
                    name,
                )

    def _require_owner(self) -> None:
        if not self._store.execution_owner_is_held(self._owner_id):
            raise AgentError(
                "execution_owner_unavailable",
                "The execution owner lock is not held.",
            )

    def _transition_run(self, run_id: UUID, snapshot: RunSnapshot, **kwargs) -> None:
        self._store.transition_run(run_id, snapshot, owner_id=self._owner_id, **kwargs)
