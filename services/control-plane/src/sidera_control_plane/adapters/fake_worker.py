from collections import deque
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from threading import RLock
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.domain.artifacts import ArtifactCandidate
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextConsumerType,
    ContextMaterializationV1,
    ContextPurpose,
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
    )

    def __init__(
        self,
        *,
        artifacts: tuple[ArtifactCandidate, ...] = (),
        failures: Mapping[str, str] | None = None,
    ) -> None:
        self._artifacts = tuple(artifacts)
        self._failures = dict(failures or {})
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
