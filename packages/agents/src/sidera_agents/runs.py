from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from threading import Condition
from uuid import UUID, uuid4

from pydantic import SecretStr

from .admission import request_fingerprint
from .context import ContextAssembler
from .contracts import (
    AssembledInput,
    CapabilitySupport,
    EffectiveConfig,
    NativeCloseState,
    NativeHandle,
    NativeOpenRequest,
    NativeOutcomeStatus,
    NativeProcessState,
    NativeResponseState,
    NativeSkill,
    ProfileRevision,
    ResourceKind,
    RunPolicy,
    RunSnapshot,
    RunStart,
    RunState,
    RuntimeSnapshot,
    SourceContent,
    SourceRef,
    StartLookup,
    StartReceipt,
    StartStatus,
    SupportStatus,
    ToolConnection,
)
from .errors import AgentError
from .ports import (
    ContextSourceReader,
    ExecutionAuthorizer,
    SecretReader,
    ToolConnectionReader,
)

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
        # Store admission stays concurrent; poll waits until each result is published.
        self._pending_lock = Condition()
        self._admissions_in_progress: set[UUID] = set()

    def start(self, request: RunStart) -> StartReceipt:
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
        run_id = uuid4()
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
        with self._pending_lock:
            self._admissions_in_progress.add(run_id)
        created = False
        conflict_error: AgentError | None = None
        stored_operation = None
        try:
            try:
                receipt, created = self._store.admit_start(
                    owner_id=self._owner_id,
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
                self._admissions_in_progress.discard(run_id)
                self._pending_lock.notify_all()
        return receipt

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

    def poll_once(self) -> int:
        self._require_owner()
        processed = 0
        with self._pending_lock:
            while self._admissions_in_progress:
                self._pending_lock.wait()
            pending_ids = set(self._pending)
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
                self._pending.pop(run_id) for run_id in sorted(self._pending, key=str)
            )
        for item in pending:
            self._process_pending(item)
            processed += 1
        for (
            snapshot,
            handle,
            terminal_state,
            terminal_reason_code,
        ) in self._store.list_unresolved_runs():
            self._inspect_unresolved(
                snapshot, handle, terminal_state, terminal_reason_code
            )
        return processed

    def _process_pending(self, item: _PendingStart) -> None:
        snapshot = item.snapshot
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

        self._require_owner()
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
            self._transition_run(
                snapshot.run_id,
                failed,
                receipt_status=StartStatus.REJECTED,
                active_reserved=False,
                session_reserved=False,
            )
            return
        except Exception:  # noqa: BLE001 - an ambiguous native open must not be retried
            self._mark_start_unknown(snapshot, "native_start_unknown")
            return

        if handle.run_id != snapshot.run_id:
            self._mark_start_unknown(snapshot, "native_identity_unverified")
            return
        self._transition_run(
            snapshot.run_id,
            snapshot.model_copy(update={"state": RunState.RUNNING}),
            native_handle=handle,
            replace_native_handle=True,
        )
        try:
            outcome = harness.prompt(handle, item.assembled_input, item.response_id)
        except Exception:  # noqa: BLE001 - a lost native response remains uncertain
            self._mark_run_unknown(snapshot, handle, "native_response_unknown")
            return
        if outcome.response_id != item.response_id:
            self._mark_run_unknown(snapshot, handle, "native_response_unverified")
        elif outcome.status is NativeOutcomeStatus.UNKNOWN:
            self._mark_run_unknown(
                snapshot,
                handle,
                _safe_native_reason(outcome.reason_code, "native_response_unknown"),
            )
        elif outcome.status is NativeOutcomeStatus.COMPLETED:
            if snapshot.policy is RunPolicy.CONVERSATION:
                idle = snapshot.model_copy(
                    update={
                        "state": RunState.IDLE,
                        "input_open": True,
                        "active_response_id": None,
                        "effective_config": snapshot.effective_config.model_copy(
                            update={"observed_model": outcome.observed_model}
                        ),
                    }
                )
                self._transition_run(
                    snapshot.run_id,
                    idle,
                    native_handle=handle,
                    replace_native_handle=True,
                    active_reserved=False,
                )
            else:
                self._close_terminal(item, handle, RunState.COMPLETED, None)
        else:
            self._close_terminal(
                item,
                handle,
                RunState.FAILED,
                _safe_native_reason(outcome.reason_code, "native_response_failed"),
            )

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

    def _mark_run_unknown(
        self, snapshot: RunSnapshot, handle: NativeHandle, reason_code: str
    ) -> None:
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
            native_handle=handle,
            replace_native_handle=True,
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
