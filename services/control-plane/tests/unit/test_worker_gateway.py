from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.event_ingestor import (
    EventIngestionReport,
    TerminalEventObservation,
)
from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
from sidera_control_plane.application.ports import (
    ArtifactContentWriter,
    ControlPlaneUnitOfWork,
)
from sidera_control_plane.application.worker_gateway import (
    ASSIGNMENT_TOPIC,
    STOP_TOPIC,
    AssignmentLeaseKeeper,
    GatewayAssignmentRunner,
    GatewayMessageError,
    WorkerGateway,
    _load_module,
)
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.domain.context_packages import ContextAcknowledgement
from sidera_control_plane.domain.gateway import (
    RuntimeModuleOperation,
    runtime_module_operation_allowed,
)
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import ModuleLifecycle
from tests.unit.test_context_package_v2 import _materialization_v1

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


class RecordingLeaseRepository:
    def __init__(self, lease: AssignmentLease) -> None:
        self.lease = lease
        self.saved: list[tuple[int, int]] = []

    def get(self, lease_id, workspace_id, *, for_update=False):
        assert for_update is True
        assert lease_id == self.lease.id
        assert workspace_id == self.lease.workspace_id
        return self.lease

    def save(self, lease, previous_version):
        self.saved.append((previous_version, lease.version))


class RecordingEventStore:
    def __init__(self) -> None:
        self.events = []

    def append_with_outbox(self, event) -> None:
        self.events.append(event)


class RecordingCommandRepository:
    def __init__(self) -> None:
        self.commands = []

    def record_applied(self, command, request_hash, result) -> None:
        self.commands.append((command, request_hash, result))


class RecordingLeaseUow:
    def __init__(self, leases, events, commands) -> None:
        self.leases = leases
        self.events = events
        self.commands = commands

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback


class RecordingResultCapture:
    def __init__(self) -> None:
        self.observations: list[dict[str, object]] = []
        self.captures: list[dict[str, object]] = []
        self.snapshot = object()

    def observe_terminal(self, **kwargs):
        self.observations.append(kwargs)
        return object()

    def capture_execution(self, **kwargs):
        self.captures.append(kwargs)
        return self.snapshot


def test_assignment_lease_keeper_renews_and_audits_with_fencing() -> None:
    workspace_id = UUID("20000000-0000-0000-0000-000000000001")
    execution_id = UUID("70000000-0000-0000-0000-000000000001")
    worker_id = UUID("80000000-0000-0000-0000-000000000001")
    lease = AssignmentLease.issue(
        lease_id=UUID("90000000-0000-0000-0000-000000000001"),
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        fencing_token=4,
        assignment_reason={"source": "unit-test"},
    )
    leases = RecordingLeaseRepository(lease)
    events = RecordingEventStore()
    commands = RecordingCommandRepository()
    uow = RecordingLeaseUow(leases, events, commands)
    keeper = AssignmentLeaseKeeper(
        uow_factory=lambda: cast(ControlPlaneUnitOfWork, uow),
        lease_id=lease.id,
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        fencing_token=4,
        lease_ttl_seconds=30,
        renew_interval_seconds=10,
        clock=lambda: NOW + timedelta(seconds=5),
    )

    assert keeper.renew_once() is True
    assert lease.expires_at == NOW + timedelta(seconds=35)
    assert lease.renewed_at == NOW + timedelta(seconds=5)
    assert leases.saved == [(1, 2)]
    assert len(events.events) == 1
    assert events.events[0].event_type == "LeaseRenewed"
    assert events.events[0].aggregate_version == 2
    assert events.events[0].payload["fencing_token"] == 4
    assert commands.commands[0][0].command_type == "RenewLease"
    assert commands.commands[0][2].event_ids == (events.events[0].id,)


def test_assignment_lease_keeper_requires_renew_interval_below_half_ttl() -> None:
    with pytest.raises(ValueError, match="less than half"):
        AssignmentLeaseKeeper(
            uow_factory=lambda: None,  # type: ignore[return-value]
            lease_id=uuid4(),
            workspace_id=uuid4(),
            execution_id=uuid4(),
            worker_id=uuid4(),
            fencing_token=1,
            lease_ttl_seconds=30,
            renew_interval_seconds=15,
        )


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


def test_gateway_observes_all_terminals_but_captures_only_success() -> None:
    capture = RecordingResultCapture()
    gateway = WorkerGateway(
        lambda: None,  # type: ignore[return-value]
        adapter_factories={},
        workspace_factories={},
        code_result_capture_service=capture,  # type: ignore[arg-type]
    )
    workspace_id = UUID("20000000-0000-0000-0000-000000000001")
    execution_id = UUID("70000000-0000-0000-0000-000000000001")
    report = EventIngestionReport(
        events=(),
        duplicate_sequences=(),
        terminal_observations=(
            TerminalEventObservation(
                event_type="session.failed",
                sequence=1,
                outcome="failed",
                payload={"failure_code": "worker_failed"},
            ),
            TerminalEventObservation(
                event_type="execution.succeeded",
                sequence=2,
                outcome="succeeded",
                payload={"summary": "ready"},
            ),
        ),
    )

    result = gateway._capture_observed_terminal(
        workspace_id=workspace_id,
        execution_id=execution_id,
        fencing_token=1,
        report=report,
    )

    assert result is capture.snapshot
    assert [item["outcome"] for item in capture.observations] == [
        "failed",
        "succeeded",
    ]
    assert len(capture.captures) == 1


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


def test_gateway_binds_context_ack_to_exact_adapter_runtime_snapshot() -> None:
    materialization = _materialization_v1()
    operation_id = uuid4()
    metadata = materialization.metadata
    acknowledgement = ContextAcknowledgement(
        package_id=metadata.package_id,
        package_hash=metadata.package_hash,
        consumer_type=metadata.consumer_type,
        consumer_id=metadata.consumer_id,
        context_version=metadata.context_version,
        adapter_module_id=uuid4(),
        adapter_config_version=1,
        acknowledged_at=NOW,
        schema_version=materialization.schema_version,
        operation_id=operation_id,
    )
    adapter_module_id = uuid4()

    bound = WorkerGateway._validate_context_acknowledgement(
        acknowledgement,
        materialization=materialization,
        operation_id=operation_id,
        adapter_module_id=adapter_module_id,
        adapter_config_version=1,
    )

    assert bound.adapter_module_id == adapter_module_id
    assert bound.adapter_config_version == 1


@pytest.mark.parametrize(
    "mutation",
    ("operation", "consumer", "package", "config"),
)
def test_gateway_rejects_context_ack_identity_or_stale_config(
    mutation: str,
) -> None:
    materialization = _materialization_v1()
    operation_id = uuid4()
    metadata = materialization.metadata
    acknowledgement = ContextAcknowledgement(
        package_id=metadata.package_id,
        package_hash=metadata.package_hash,
        consumer_type=metadata.consumer_type,
        consumer_id=metadata.consumer_id,
        context_version=metadata.context_version,
        adapter_module_id=uuid4(),
        adapter_config_version=1,
        acknowledged_at=NOW,
        schema_version=materialization.schema_version,
        operation_id=operation_id,
    )
    if mutation == "operation":
        acknowledgement = replace(acknowledgement, operation_id=None)
    elif mutation == "consumer":
        acknowledgement = replace(acknowledgement, consumer_id=uuid4())
    elif mutation == "package":
        acknowledgement = replace(acknowledgement, package_id=uuid4())
    elif mutation == "config":
        acknowledgement = replace(acknowledgement, adapter_config_version=2)
    else:
        raise AssertionError(f"unsupported mutation: {mutation}")

    with pytest.raises(GatewayMessageError) as caught:
        WorkerGateway._validate_context_acknowledgement(
            acknowledgement,
            materialization=materialization,
            operation_id=operation_id,
            adapter_module_id=uuid4(),
            adapter_config_version=1,
        )

    assert caught.value.code == "context_acknowledgement_mismatch"


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
