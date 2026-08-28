from collections import deque
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from threading import RLock
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.knowledge_candidate_submission import (
    KnowledgeCandidateProposalV1,
)
from sidera_control_plane.domain.artifacts import ArtifactCandidate
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextConsumerType,
    ContextMaterializationV1,
    ContextPurpose,
)
from sidera_control_plane.domain.context_patches import (
    ContextPatchDeliveryResult,
    ContextPatchMaterializationV1,
)
from sidera_control_plane.domain.gateway import (
    AdapterEvent,
    AdapterSessionHandle,
    DeliveryOutcome,
    DeliveryResult,
)
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
)
from sidera_control_plane.domain.workspace import (
    ExecutionSpec,
    ExecutionWorkspaceManifest,
)


class FakeAdapterError(RuntimeError):
    """A deterministic failure injected into the fake worker adapter."""


class FakeWorkerAdapter:
    """In-memory WorkerAdapter used for deterministic control-plane tests."""

    _CAPABILITIES = WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
        context_package_versions=(1,),
        context_patch_versions=(1,),
    )

    def __init__(
        self,
        *,
        artifacts: tuple[ArtifactCandidate, ...] = (),
        knowledge_candidates: tuple[KnowledgeCandidateProposalV1, ...] = (),
        failures: Mapping[str, str] | None = None,
    ) -> None:
        self._artifacts = tuple(artifacts)
        if any(
            not isinstance(item, KnowledgeCandidateProposalV1)
            for item in knowledge_candidates
        ):
            raise TypeError("knowledge_candidates must be typed proposals")
        self._knowledge_candidates = tuple(knowledge_candidates)
        self._failures = dict(failures or {})
        self._patch_outcomes: deque[tuple[DeliveryOutcome, str | None]] = deque()
        self._patch_results: dict[UUID, ContextPatchDeliveryResult] = {}
        self._applied_context_patches: list[ContextPatchMaterializationV1] = []
        self._sessions: dict[str, _FakeSession] = {}
        self._lock = RLock()

    def probe(self, config: Mapping[str, object]) -> WorkerAdapterCapabilityProfile:
        del config
        self._maybe_fail("probe")
        return self._CAPABILITIES

    def accept_context(
        self,
        materialization: ContextMaterializationV1,
        *,
        operation_id: UUID,
    ) -> ContextAcknowledgement:
        _validate_context(materialization, operation_id)
        metadata = materialization.metadata
        return ContextAcknowledgement(
            package_id=metadata.package_id,
            package_hash=metadata.package_hash,
            consumer_type=metadata.consumer_type,
            consumer_id=metadata.consumer_id,
            context_version=metadata.context_version,
            adapter_module_id=uuid5(
                NAMESPACE_URL, "sidera.fake.worker.context-adapter"
            ),
            adapter_config_version=1,
            acknowledged_at=datetime.now(UTC),
            schema_version=materialization.schema_version,
            operation_id=operation_id,
        )

    def start_session(
        self,
        spec: ExecutionSpec,
        workspace: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
    ) -> AdapterSessionHandle:
        del workspace, operation_id
        self._maybe_fail("start_session")
        now = datetime.now(UTC)
        execution_session_id = uuid5(
            NAMESPACE_URL, f"sidera.fake.execution-session:{spec.execution_id}"
        )
        adapter_session_id = f"fake-session-{spec.execution_id}"
        handle = AdapterSessionHandle(
            execution_session_id=execution_session_id,
            adapter_session_id=adapter_session_id,
            process_ref=f"fake://{spec.execution_id}",
            capability_snapshot=self._CAPABILITIES,
            last_accepted_sequence=0,
            started_at=now,
        )
        with self._lock:
            self._sessions[adapter_session_id] = _FakeSession(
                handle=handle,
                events=deque(
                    [
                        AdapterEvent(
                            event_type="session.started",
                            sequence=1,
                            payload={"execution_id": str(spec.execution_id)},
                            occurred_at=now,
                        )
                    ]
                ),
                next_sequence=2,
            )
        return handle

    def read_events(self, handle: AdapterSessionHandle) -> Iterable[AdapterEvent]:
        self._maybe_fail("read_events")
        session = self._session(handle)
        with self._lock:
            events = tuple(session.events)
            session.events.clear()
        return events

    def send_input(
        self,
        handle: AdapterSessionHandle,
        text: str,
        attachment_refs: tuple[str, ...],
        *,
        operation_id: UUID,
    ) -> DeliveryResult:
        del attachment_refs
        self._maybe_fail("send_input")
        if not text.strip():
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.FAILED,
                accepted_sequence=None,
                failure_code="empty_input",
            )
        session = self._session(handle)
        with self._lock:
            if session.stopped:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.FAILED,
                    accepted_sequence=None,
                    failure_code="session_stopped",
                )
            if session.prompt_busy:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.BLOCKED,
                    accepted_sequence=None,
                    failure_code="prompt_busy",
                )
            sequence = session.next_sequence
            session.next_sequence += 1
            session.prompt_busy = True
            session.events.append(
                AdapterEvent(
                    event_type="input.accepted",
                    sequence=sequence,
                    payload={"text_length": len(text)},
                    occurred_at=datetime.now(UTC),
                )
            )
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.DELIVERED,
                accepted_sequence=sequence,
                failure_code=None,
            )

    def apply_context_patch(
        self,
        handle: AdapterSessionHandle,
        materialization: ContextPatchMaterializationV1,
        *,
        operation_id: UUID,
    ) -> ContextPatchDeliveryResult:
        if not isinstance(materialization, ContextPatchMaterializationV1):
            raise TypeError("materialization must be a ContextPatchMaterializationV1")
        if not isinstance(operation_id, UUID):
            raise TypeError("operation_id must be a UUID")
        patch_hash = materialization.patch.patch_hash
        previous = self._patch_results.get(operation_id)
        if previous is not None:
            if previous.patch_hash != patch_hash:
                raise FakeAdapterError("patch_operation_id_reused")
            return previous
        self._maybe_fail("apply_context_patch")
        session = self._session(handle)
        with self._lock:
            if session.handle.execution_session_id != handle.execution_session_id:
                return self._remember_patch_result(
                    ContextPatchDeliveryResult(
                        outcome="failed",
                        operation_id=operation_id,
                        patch_hash=patch_hash,
                        accepted_sequence=None,
                        error_code="session_identity_mismatch",
                    )
                )
            if session.stopped:
                return self._remember_patch_result(
                    ContextPatchDeliveryResult(
                        outcome="failed",
                        operation_id=operation_id,
                        patch_hash=patch_hash,
                        accepted_sequence=None,
                        error_code="session_stopped",
                    )
                )
            if materialization.patch.expires_at <= datetime.now(UTC):
                return self._remember_patch_result(
                    ContextPatchDeliveryResult(
                        outcome="failed",
                        operation_id=operation_id,
                        patch_hash=patch_hash,
                        accepted_sequence=None,
                        error_code="context_patch_expired",
                    )
                )
            if self._patch_outcomes:
                outcome, error_code = self._patch_outcomes.popleft()
                if outcome is not DeliveryOutcome.DELIVERED:
                    return self._remember_patch_result(
                        ContextPatchDeliveryResult(
                            outcome=outcome.value,
                            operation_id=operation_id,
                            patch_hash=patch_hash,
                            accepted_sequence=None,
                            error_code=error_code or outcome.value,
                        )
                    )
            if session.prompt_busy:
                return self._remember_patch_result(
                    ContextPatchDeliveryResult(
                        outcome="blocked",
                        operation_id=operation_id,
                        patch_hash=patch_hash,
                        accepted_sequence=None,
                        error_code="prompt_busy",
                    )
                )
            sequence = session.next_sequence
            session.next_sequence += 1
            session.prompt_busy = True
            session.events.append(
                AdapterEvent(
                    event_type="context.patch.accepted",
                    sequence=sequence,
                    payload={"patch_hash": patch_hash},
                    occurred_at=datetime.now(UTC),
                )
            )
            self._applied_context_patches.append(materialization)
            return self._remember_patch_result(
                ContextPatchDeliveryResult(
                    outcome="delivered",
                    operation_id=operation_id,
                    patch_hash=patch_hash,
                    accepted_sequence=sequence,
                    error_code=None,
                )
            )

    def stop_session(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> DeliveryResult:
        self._maybe_fail("stop_session")
        session = self._session(handle)
        with self._lock:
            if session.stopped:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.DELIVERED,
                    accepted_sequence=session.next_sequence - 1,
                    failure_code=None,
                )
            session.stopped = True
            session.prompt_busy = False
            sequence = session.next_sequence
            session.next_sequence += 1
            session.events.append(
                AdapterEvent(
                    event_type="session.canceled",
                    sequence=sequence,
                    payload={},
                    occurred_at=datetime.now(UTC),
                )
            )
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.DELIVERED,
                accepted_sequence=sequence,
                failure_code=None,
            )

    def collect_artifacts(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> tuple[ArtifactCandidate, ...]:
        del operation_id
        self._maybe_fail("collect_artifacts")
        self._session(handle)
        return self._artifacts

    def collect_knowledge_candidates(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> tuple[KnowledgeCandidateProposalV1, ...]:
        if not isinstance(operation_id, UUID):
            raise TypeError("operation_id must be a UUID")
        self._maybe_fail("collect_knowledge_candidates")
        self._session(handle)
        return self._knowledge_candidates

    def release_boundary(self, handle: AdapterSessionHandle) -> None:
        """Make the current prompt safe for the next queued input."""

        session = self._session(handle)
        with self._lock:
            if session.stopped:
                raise FakeAdapterError("session_stopped")
            session.prompt_busy = False
            sequence = session.next_sequence
            session.next_sequence += 1
            session.events.append(
                AdapterEvent(
                    event_type="checkpoint.created",
                    sequence=sequence,
                    payload={"boundary": "prompt_complete"},
                    occurred_at=datetime.now(UTC),
                )
            )

    def active_sessions(self) -> tuple[AdapterSessionHandle, ...]:
        with self._lock:
            return tuple(
                session.handle
                for session in self._sessions.values()
                if not session.stopped
            )

    @property
    def applied_context_patches(self) -> tuple[ContextPatchMaterializationV1, ...]:
        with self._lock:
            return tuple(self._applied_context_patches)

    def inject_patch_outcome(
        self,
        outcome: DeliveryOutcome | str,
        *,
        error_code: str | None = None,
    ) -> None:
        try:
            normalized = (
                outcome
                if isinstance(outcome, DeliveryOutcome)
                else DeliveryOutcome(outcome)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported patch outcome: {outcome!r}") from exc
        if normalized is DeliveryOutcome.DELIVERED and error_code is not None:
            raise ValueError("delivered patch outcome cannot have an error_code")
        if error_code is not None and not error_code.strip():
            raise ValueError("error_code is required when provided")
        self._patch_outcomes.append((normalized, error_code))

    def inject_failure(self, operation: str, failure_code: str) -> None:
        clean_code = failure_code.strip()
        if not clean_code:
            raise ValueError("failure_code is required")
        self._failures[operation] = clean_code

    def _session(self, handle: AdapterSessionHandle) -> "_FakeSession":
        try:
            return self._sessions[handle.adapter_session_id]
        except KeyError as exc:
            raise FakeAdapterError("unknown_session") from exc

    def _maybe_fail(self, operation: str) -> None:
        failure_code = self._failures.get(operation)
        if failure_code is not None:
            raise FakeAdapterError(failure_code)

    def _remember_patch_result(
        self, result: ContextPatchDeliveryResult
    ) -> ContextPatchDeliveryResult:
        self._patch_results[result.operation_id] = result
        return result


class _FakeSession:
    def __init__(
        self,
        *,
        handle: AdapterSessionHandle,
        events: deque[AdapterEvent],
        next_sequence: int,
    ) -> None:
        self.handle = handle
        self.events = events
        self.next_sequence = next_sequence
        self.prompt_busy = False
        self.stopped = False


def _validate_context(
    materialization: ContextMaterializationV1,
    operation_id: UUID,
) -> None:
    if not isinstance(materialization, ContextMaterializationV1):
        raise TypeError("materialization must be a ContextMaterializationV1")
    if not isinstance(operation_id, UUID):
        raise TypeError("operation_id must be a UUID")
    if materialization.schema_version != 1:
        raise ValueError("unsupported context materialization schema")
    if (
        materialization.metadata.purpose is not ContextPurpose.EXECUTION
        or materialization.metadata.consumer_type is not ContextConsumerType.WORKER
    ):
        raise ValueError("worker adapter requires an execution worker context")
    if materialization.total_byte_count > 256 * 1024:
        raise ValueError("context materialization exceeds adapter size limit")
