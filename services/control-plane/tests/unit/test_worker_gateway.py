from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
from sidera_control_plane.application.ports import ArtifactContentWriter
from sidera_control_plane.application.worker_gateway import (
    ASSIGNMENT_TOPIC,
    STOP_TOPIC,
    GatewayAssignmentRunner,
    GatewayMessageError,
    WorkerGateway,
    _load_module,
)
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.domain.gateway import (
    RuntimeModuleOperation,
    runtime_module_operation_allowed,
)
from sidera_control_plane.domain.modules import ModuleLifecycle

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _message(topic: str = ASSIGNMENT_TOPIC) -> OutboxMessage:
    return OutboxMessage(
        id=uuid4(),
        event_id=uuid4(),
        topic=topic,
        payload={"workspace_id": str(uuid4())},
        attempts=1,
        claim_token=uuid4(),
    )


class RecordingStore:
    def __init__(self, messages: tuple[OutboxMessage, ...]) -> None:
        self.messages = messages
        self.claim_topics: set[str] | None = None
        self.acknowledged: list[tuple[object, object]] = []
        self.rescheduled: list[tuple[object, object, datetime, str]] = []

    def claim(self, batch_size: int, *, topics=None):
        assert batch_size == 20
        self.claim_topics = set(topics or ())
        return self.messages

    def acknowledge(self, message_id, claim_token) -> bool:
        self.acknowledged.append((message_id, claim_token))
        return True

    def reschedule(self, message_id, claim_token, next_attempt_at, error) -> bool:
        self.rescheduled.append((message_id, claim_token, next_attempt_at, error))
        return True


class RecordingGateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[OutboxMessage] = []
        self.fail = fail

    def handle(self, message: OutboxMessage) -> object:
        self.messages.append(message)
        if self.fail:
            raise RuntimeError("gateway unavailable")


class RecordingArtifactContentWriter(ArtifactContentWriter):
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID, str, bytes]] = []

    def put(
        self,
        workspace_id: UUID,
        execution_id: UUID,
        expected_hash: str,
        content: bytes,
    ) -> str:
        self.calls.append((workspace_id, execution_id, expected_hash, content))
        return f"artifact-local://{workspace_id}/{execution_id}/{expected_hash}"


def test_gateway_runner_claims_assignment_and_stop_topics_and_acknowledges() -> None:
    message = _message()
    store = RecordingStore((message,))
    gateway = RecordingGateway()
    runner = GatewayAssignmentRunner(store, gateway, clock=lambda: NOW)

    assert runner.run_once() == (1, 1, 0)
    assert store.claim_topics == {ASSIGNMENT_TOPIC, STOP_TOPIC}
    assert gateway.messages == [message]
    assert store.acknowledged == [(message.id, message.claim_token)]


def test_gateway_runner_reschedules_failures_without_acknowledging() -> None:
    message = _message()
    store = RecordingStore((message,))
    runner = GatewayAssignmentRunner(
        store,
        RecordingGateway(fail=True),
        clock=lambda: NOW,
    )

    assert runner.run_once() == (1, 0, 1)
    assert store.acknowledged == []
    assert store.rescheduled[0][2] == NOW + timedelta(seconds=2)
    assert "gateway_unavailable" in store.rescheduled[0][3]


def test_worker_gateway_stores_content_before_registry_and_strips_bytes() -> None:
    workspace_id = UUID("20000000-0000-0000-0000-000000000001")
    execution_id = UUID("70000000-0000-0000-0000-000000000001")
    content = b"report"
    content_writer = RecordingArtifactContentWriter()
    gateway = WorkerGateway(
        lambda: None,  # type: ignore[return-value]
        adapter_factories={},
        workspace_factories={},
        artifact_content_store=content_writer,
    )
    candidate = ArtifactCandidate(
        artifact_type=ArtifactType.REPORT,
        storage_ref="artifact://provider/report",
        content_hash=sha256(content).hexdigest(),
        size_bytes=len(content),
        media_type="text/plain",
        provenance={},
        content=content,
    )

    prepared = gateway._prepare_artifact_candidates(
        workspace_id=workspace_id,
        execution_id=execution_id,
        candidates=(candidate,),
    )

    assert prepared[0].storage_ref.startswith("artifact-local://")
    assert prepared[0].content is None
    assert content_writer.calls == [
        (workspace_id, execution_id, candidate.content_hash, content)
    ]


def test_worker_gateway_rejects_content_metadata_mismatch() -> None:
    content_writer = RecordingArtifactContentWriter()
    gateway = WorkerGateway(
        lambda: None,  # type: ignore[return-value]
        adapter_factories={},
        workspace_factories={},
        artifact_content_store=content_writer,
    )
    candidate = ArtifactCandidate(
        artifact_type=ArtifactType.REPORT,
        storage_ref="artifact://provider/report",
        content_hash=sha256(b"report").hexdigest(),
        size_bytes=100,
        media_type="text/plain",
        provenance={},
        content=b"report",
    )

    try:
        gateway._prepare_artifact_candidates(
            workspace_id=UUID("20000000-0000-0000-0000-000000000001"),
            execution_id=UUID("70000000-0000-0000-0000-000000000001"),
            candidates=(candidate,),
        )
    except GatewayMessageError as exc:
        assert "size" in str(exc)
    else:
        raise AssertionError("expected GatewayMessageError")


def test_runtime_module_lifecycle_matrix() -> None:
    for lifecycle in (ModuleLifecycle.ACTIVE, ModuleLifecycle.DRAINING):
        for operation in RuntimeModuleOperation:
            assert runtime_module_operation_allowed(lifecycle, operation)

    allowed_when_disabled = {
        RuntimeModuleOperation.OBSERVE,
        RuntimeModuleOperation.STOP,
        RuntimeModuleOperation.CLEANUP,
    }
    for operation in RuntimeModuleOperation:
        assert runtime_module_operation_allowed(
            ModuleLifecycle.DISABLED, operation
        ) is (operation in allowed_when_disabled)


def test_gateway_message_error_preserves_stable_code() -> None:
    error = GatewayMessageError(
        "the runtime binding is missing", code="runtime_binding_missing"
    )

    assert error.code == "runtime_binding_missing"


def test_gateway_rejects_a_binding_to_a_missing_module_instance() -> None:
    class EmptyModules:
        def get_instance(self, module_id, workspace_id):
            del module_id, workspace_id

    class FakeUnitOfWork:
        modules = EmptyModules()

    with pytest.raises(GatewayMessageError) as caught:
        _load_module(
            FakeUnitOfWork(),  # type: ignore[arg-type]
            uuid4(),
            uuid4(),
            expected_type="worker_adapter",
        )

    assert caught.value.code == "runtime_binding_missing"
