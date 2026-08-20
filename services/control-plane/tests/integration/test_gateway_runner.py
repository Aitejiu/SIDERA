from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.application.input_dispatcher import InputDispatcher
from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
from sidera_control_plane.application.worker_gateway import (
    ASSIGNMENT_TOPIC,
    GatewayRecoveryRequired,
    WorkerGateway,
    _operation_id,
)
from sidera_control_plane.domain.gateway import (
    DeliveryOutcome,
    DeliveryResult,
    GatewayOperationType,
)
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    CapabilityProfile,
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
)
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    CommandModel,
    ExecutionBindingModel,
    ExecutionEventModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionSessionModel,
    GatewayOperationModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    TaskModel,
    WorkerModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _capabilities() -> CapabilityProfile:
    return CapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
    )


class RecordingFakeAdapter(FakeWorkerAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.start_count = 0
        self.input_count = 0

    def start_session(self, *args, **kwargs):
        self.start_count += 1
        return super().start_session(*args, **kwargs)

    def send_input(self, *args, **kwargs):
        self.input_count += 1
        return super().send_input(*args, **kwargs)


class UnknownInputAdapter(RecordingFakeAdapter):
    def send_input(self, _handle, _text, _attachment_refs, *, operation_id):
        self.input_count += 1
        return DeliveryResult(
            operation_id=operation_id,
            outcome=DeliveryOutcome.UNKNOWN,
            accepted_sequence=None,
            failure_code="acp_response_timeout",
        )


class FakeWorkspaceProvider:
    def __init__(self) -> None:
        self.prepare_count = 0

    def prepare(self, spec, *, operation_id):
        self.prepare_count += 1
        return ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=spec.workspace_id,
            task_id=spec.task_id,
            execution_id=spec.execution_id,
            checkouts=(
                WorkspaceCheckout(
                    repository_ref="repo://sidera",
                    base_revision="a" * 40,
                    branch=f"sidera/execution/{spec.execution_id}",
                    current_head="a" * 40,
                ),
            ),
            location_ref=f"fake-workspace://{spec.execution_id}",
            retention_seconds=3600,
            fencing_token=spec.fencing_token,
            created_at=NOW,
        )

    def freeze(self, manifest, *, operation_id):
        return manifest

    def retain(self, manifest, *, operation_id):
        return manifest

    def release(self, manifest, *, operation_id):
        return manifest

    def inspect(self, location_ref):
        raise RuntimeError(f"not implemented for {location_ref}")


def _seed_assignment(database: Database, workspace_id: UUID) -> dict[str, UUID]:
    task_id = uuid4()
    worker_id = uuid4()
    execution_id = uuid4()
    lease_id = uuid4()
    adapter_definition_id = uuid4()
    workspace_definition_id = uuid4()
    adapter_instance_id = uuid4()
    workspace_instance_id = uuid4()
    capabilities = _capabilities()
    with database.session() as session:
        session.add(
            TaskModel(
                id=task_id,
                workspace_id=workspace_id,
                title="Gateway execution",
                description="Run the assigned execution",
                acceptance_criteria=["session starts"],
                status="running",
                priority=50,
                requested_by="integration",
                version=1,
            )
        )
        session.add(
            WorkerModel(
                id=worker_id,
                workspace_id=workspace_id,
                adapter_type="fake",
                display_name="Fake Gateway Worker",
                status="reserved",
                desired_state="active",
                capabilities=["python"],
                capacity=1,
                current_load=1,
                cost_per_hour_microunits=1,
                startup_latency_ms=1,
                last_heartbeat_at=NOW,
                version=1,
            )
        )
        session.add_all(
            [
                ModuleDefinitionModel(
                    id=adapter_definition_id,
                    workspace_id=workspace_id,
                    module_type="worker_adapter",
                    provider="fake",
                    interface_version="sidera.worker-adapter.v1",
                    config_schema_version=1,
                    capabilities={
                        "input": capabilities.input.value,
                        "idempotency": capabilities.idempotency.value,
                        "resume": capabilities.resume.value,
                        "stop": capabilities.stop.value,
                        "sandbox": capabilities.sandbox.value,
                        "stability": capabilities.stability.value,
                    },
                    lifecycle="active",
                ),
                ModuleDefinitionModel(
                    id=workspace_definition_id,
                    workspace_id=workspace_id,
                    module_type="workspace_provider",
                    provider="fake-workspace",
                    interface_version="sidera.workspace-provider.v1",
                    config_schema_version=1,
                    capabilities={
                        "input": capabilities.input.value,
                        "idempotency": capabilities.idempotency.value,
                        "resume": capabilities.resume.value,
                        "stop": capabilities.stop.value,
                        "sandbox": capabilities.sandbox.value,
                        "stability": capabilities.stability.value,
                    },
                    lifecycle="active",
                ),
                ModuleInstanceModel(
                    id=adapter_instance_id,
                    workspace_id=workspace_id,
                    definition_id=adapter_definition_id,
                    instance_name="fake-adapter",
                    config_version=1,
                    config_namespace="fake",
                    config={"fake.name": "integration"},
                    secret_refs=[],
                    lifecycle="active",
                ),
                ModuleInstanceModel(
                    id=workspace_instance_id,
                    workspace_id=workspace_id,
                    definition_id=workspace_definition_id,
                    instance_name="fake-workspace",
                    config_version=1,
                    config_namespace="workspace",
                    config={
                        "workspace.repository_ref": "repo://sidera",
                        "workspace.base_revision": "a" * 40,
                    },
                    secret_refs=[],
                    lifecycle="active",
                ),
            ]
        )
        session.commit()
    with database.session() as session:
        execution = ExecutionModel(
            id=execution_id,
            workspace_id=workspace_id,
            task_id=task_id,
            lineage_id=execution_id,
            attempt=1,
            max_attempts=2,
            purpose="Start Gateway",
            status="leased",
            capability_requirements=["python"],
            pinned_worker_id=None,
            assigned_worker_id=worker_id,
            active_lease_id=None,
            last_fencing_token=1,
            not_before=NOW,
            failure_code=None,
            started_at=None,
            finished_at=None,
            version=2,
        )
        session.add(execution)
        session.flush([execution])
        session.add(
            AssignmentLeaseModel(
                id=lease_id,
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_id,
                status="active",
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                renewed_at=None,
                ended_at=None,
                fencing_token=1,
                assignment_reason={"source": "integration"},
                version=1,
            )
        )
        session.add(
            ExecutionBindingModel(
                id=uuid4(),
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_id,
                adapter_module_id=adapter_instance_id,
                workspace_module_id=workspace_instance_id,
                adapter_config_version=1,
                workspace_config_version=1,
                capability_snapshot={
                    "input": capabilities.input.value,
                    "idempotency": capabilities.idempotency.value,
                    "resume": capabilities.resume.value,
                    "stop": capabilities.stop.value,
                    "sandbox": capabilities.sandbox.value,
                    "stability": capabilities.stability.value,
                },
                lease_id=lease_id,
                fencing_token=1,
                version=1,
            )
        )
        session.commit()
    with database.session() as session:
        session.execute(
            update(ExecutionModel)
            .where(
                ExecutionModel.id == execution_id,
                ExecutionModel.workspace_id == workspace_id,
            )
            .values(active_lease_id=lease_id)
        )
        session.commit()
    return {
        "task_id": task_id,
        "worker_id": worker_id,
        "execution_id": execution_id,
        "lease_id": lease_id,
        "adapter_instance_id": adapter_instance_id,
        "workspace_instance_id": workspace_instance_id,
    }


def _message(workspace_id: UUID, execution_id: UUID, lease_id: UUID) -> OutboxMessage:
    return OutboxMessage(
        id=uuid4(),
        event_id=uuid4(),
        topic=ASSIGNMENT_TOPIC,
        payload={
            "workspace_id": str(workspace_id),
            "aggregate_id": str(execution_id),
            "data": {"lease_id": str(lease_id), "fencing_token": 1},
        },
        attempts=1,
        claim_token=uuid4(),
    )


def test_gateway_starts_once_and_replays_duplicate_assignment(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )
    message = _message(workspace_id, ids["execution_id"], ids["lease_id"])

    first = gateway.handle(message)
    second = gateway.handle(message)

    assert first == second
    assert adapter.start_count == 1
    assert adapter.input_count == 1
    assert provider.prepare_count == 1
    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        assert execution is not None
        assert execution.status == "running"
        stored_session = session.query(ExecutionSessionModel).one()
        assert stored_session.last_sequence == 2
        assert session.query(ExecutionEventModel).count() == 2
        assert session.query(GatewayOperationModel).count() == 3
        assert session.query(GatewayOperationModel).filter_by(status="succeeded").count() == 3


def test_gateway_marks_unresolved_running_operation_unknown_without_restart(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    with database.session() as session:
        session.add(
            GatewayOperationModel(
                id=uuid4(),
                workspace_id=workspace_id,
                operation_id=_operation_id(
                    ids["execution_id"], GatewayOperationType.START_SESSION
                ),
                task_id=ids["task_id"],
                execution_id=ids["execution_id"],
                session_id=None,
                operation_type=GatewayOperationType.START_SESSION.value,
                module_instance_id=ids["adapter_instance_id"],
                config_version=1,
                lease_id=ids["lease_id"],
                fencing_token=1,
                status="running",
                attempt_count=1,
                next_attempt_at=None,
                result_ref=None,
                failure_code=None,
                version=2,
            )
        )
        session.commit()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )

    with pytest.raises(GatewayRecoveryRequired):
        gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    assert adapter.start_count == 0
    with database.session() as session:
        operation = session.query(GatewayOperationModel).filter_by(
            operation_type=GatewayOperationType.START_SESSION.value
        ).one()
        assert operation.status == "unknown"


def test_gateway_preserves_unknown_initial_prompt_result(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = UnknownInputAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )

    with pytest.raises(GatewayRecoveryRequired):
        gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    assert adapter.input_count == 1
    with database.session() as session:
        operation = session.query(GatewayOperationModel).filter_by(
            operation_type=GatewayOperationType.SEND_INPUT.value
        ).one()
        assert operation.status == "unknown"
        assert operation.failure_code == "acp_response_timeout"


def test_input_dispatcher_delivers_to_exact_gateway_session(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )

    result = gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))
    adapter.release_boundary(adapter.active_sessions()[0])
    input_id = uuid4()
    source_command_id = uuid4()
    with database.session() as session:
        session.add(
            CommandModel(
                id=source_command_id,
                workspace_id=workspace_id,
                command_type="SendExecutionInput",
                actor_type="user",
                actor_id="integration",
                target_type="execution_session",
                target_id=result.session_id,
                payload={"source": "integration"},
                expected_version=1,
                idempotency_key=f"source:{source_command_id}",
                request_hash="0" * 64,
                correlation_id=uuid4(),
                status="applied",
                result={},
                rejection_reason=None,
            )
        )
        session.add(
            ExecutionInputModel(
                id=input_id,
                workspace_id=workspace_id,
                execution_id=ids["execution_id"],
                session_id=result.session_id,
                client_idempotency_key="gateway-input-1",
                text="continue the assigned task",
                attachment_refs=[],
                status="queued",
                operation_id=None,
                failure_code=None,
                version=1,
                source_command_id=source_command_id,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    dispatcher = InputDispatcher(
        lambda: SqlAlchemyUnitOfWork(database), gateway, clock=lambda: NOW
    )
    report = dispatcher.run_once()

    assert report.claimed == 1
    assert report.delivered == 1
    with database.session() as session:
        stored_input = session.get(ExecutionInputModel, input_id)
        assert stored_input is not None
        assert stored_input.status == "delivered"
        assert stored_input.operation_id is None
        operations = session.query(GatewayOperationModel).filter_by(
            execution_id=ids["execution_id"],
            operation_type=GatewayOperationType.SEND_INPUT.value,
        )
        assert operations.count() == 2
        assert operations.filter_by(status="succeeded").count() == 2

    gateway.sync_session(
        workspace_id=workspace_id,
        execution_id=ids["execution_id"],
        session_id=result.session_id,
        lease_id=ids["lease_id"],
        fencing_token=1,
    )
    with database.session() as session:
        stored_session = session.get(ExecutionSessionModel, result.session_id)
        assert stored_session is not None
        assert stored_session.last_sequence == 4
        assert session.query(ExecutionEventModel).count() == 4
