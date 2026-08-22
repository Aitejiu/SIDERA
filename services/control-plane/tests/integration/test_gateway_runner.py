from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.execution_commands import ExecutionCommandHandler
from sidera_control_plane.application.input_dispatcher import InputDispatcher
from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
from sidera_control_plane.application.worker_gateway import (
    ASSIGNMENT_TOPIC,
    STOP_TOPIC,
    GatewayRecoveryRequired,
    GatewayStopResult,
    WorkerGateway,
    _operation_id,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import InvalidTransition
from sidera_control_plane.domain.gateway import (
    AdapterSessionHandle,
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
    ExecutionSpec,
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
    OutboxMessageModel,
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
        self.stop_count = 0

    def start_session(
        self,
        spec: ExecutionSpec,
        workspace: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
    ) -> AdapterSessionHandle:
        self.start_count += 1
        return super().start_session(spec, workspace, operation_id=operation_id)

    def send_input(
        self,
        handle: AdapterSessionHandle,
        text: str,
        attachment_refs: tuple[str, ...],
        *,
        operation_id: UUID,
    ) -> DeliveryResult:
        self.input_count += 1
        return super().send_input(
            handle, text, attachment_refs, operation_id=operation_id
        )

    def stop_session(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> DeliveryResult:
        self.stop_count += 1
        return super().stop_session(handle, operation_id=operation_id)


class UnknownInputAdapter(RecordingFakeAdapter):
    def send_input(
        self,
        handle: AdapterSessionHandle,
        text: str,
        attachment_refs: tuple[str, ...],
        *,
        operation_id: UUID,
    ) -> DeliveryResult:
        del handle, text, attachment_refs
        self.input_count += 1
        return DeliveryResult(
            operation_id=operation_id,
            outcome=DeliveryOutcome.UNKNOWN,
            accepted_sequence=None,
            failure_code="acp_response_timeout",
        )


class UnknownStopAdapter(RecordingFakeAdapter):
    def stop_session(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> DeliveryResult:
        del handle
        self.stop_count += 1
        return DeliveryResult(
            operation_id=operation_id,
            outcome=DeliveryOutcome.UNKNOWN,
            accepted_sequence=None,
            failure_code="process_stop_unconfirmed",
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


def _seed_assignment(
    database: Database, workspace_id: UUID, *, name_suffix: str = ""
) -> dict[str, UUID]:
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
                    instance_name=f"fake-adapter{name_suffix}",
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
                    instance_name=f"fake-workspace{name_suffix}",
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


def _stop_service(database: Database) -> CommandService:
    return CommandService(
        lambda: SqlAlchemyUnitOfWork(database),
        (ExecutionCommandHandler(lambda: NOW, lease_ttl_seconds=300),),
    )


def _request_stop(
    database: Database, workspace_id: UUID, execution_id: UUID, expected_version: int
) -> tuple[UUID, OutboxMessage]:
    command_id = uuid4()
    _stop_service(database).handle(
        CommandEnvelope(
            id=command_id,
            workspace_id=workspace_id,
            command_type="StopExecution",
            target_type="execution",
            actor_type="user",
            actor_id="user:integration",
            target_id=execution_id,
            payload={},
            expected_version=expected_version,
            idempotency_key=f"stop:{execution_id}:{command_id}",
            correlation_id=uuid4(),
        )
    )
    with database.session() as session:
        row = session.query(OutboxMessageModel).filter_by(topic=STOP_TOPIC).one()
        return command_id, OutboxMessage(
            id=row.id,
            event_id=row.event_id,
            topic=row.topic,
            payload=dict(row.payload),
            attempts=1,
            claim_token=uuid4(),
        )


def _request_continue(
    database: Database,
    workspace_id: UUID,
    session_id: UUID,
    expected_version: int,
) -> UUID:
    command_id = uuid4()
    _stop_service(database).handle(
        CommandEnvelope(
            id=command_id,
            workspace_id=workspace_id,
            command_type="ContinueExecutionSession",
            target_type="execution_session",
            actor_type="user",
            actor_id="user:integration",
            target_id=session_id,
            payload={"not_before": NOW.isoformat()},
            expected_version=expected_version,
            idempotency_key=f"continue:{session_id}:{command_id}",
            correlation_id=uuid4(),
        )
    )
    return command_id


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


def test_gateway_stops_exact_execution_and_continues_without_touching_sibling(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    sibling_ids = _seed_assignment(database, workspace_id, name_suffix="-sibling")
    with database.session() as session:
        session.execute(
            update(ExecutionModel)
            .where(
                ExecutionModel.id == sibling_ids["execution_id"],
                ExecutionModel.workspace_id == workspace_id,
            )
            .values(task_id=ids["task_id"])
        )
        session.commit()
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )

    first_session = gateway.handle(
        _message(workspace_id, ids["execution_id"], ids["lease_id"])
    )
    sibling_session = gateway.handle(
        _message(workspace_id, sibling_ids["execution_id"], sibling_ids["lease_id"])
    )
    with database.session() as session:
        first_execution = session.get(ExecutionModel, ids["execution_id"])
        sibling_execution = session.get(ExecutionModel, sibling_ids["execution_id"])
        assert first_execution is not None
        assert sibling_execution is not None
        first_version = first_execution.version
        assert first_execution.status == "running"
        assert sibling_execution.status == "running"

    _command_id, stop_message = _request_stop(
        database, workspace_id, ids["execution_id"], first_version
    )
    stopped = gateway.handle(stop_message)
    replayed = gateway.handle(stop_message)

    assert isinstance(stopped, GatewayStopResult)
    assert isinstance(replayed, GatewayStopResult)
    assert stopped == replayed
    assert stopped.operation_id == _operation_id(
        ids["execution_id"], GatewayOperationType.STOP_SESSION
    )
    assert adapter.stop_count == 1
    with database.session() as session:
        stopped_execution = session.get(ExecutionModel, ids["execution_id"])
        sibling_execution = session.get(ExecutionModel, sibling_ids["execution_id"])
        stopped_session = session.get(ExecutionSessionModel, first_session.session_id)
        sibling_session_row = session.get(
            ExecutionSessionModel, sibling_session.session_id
        )
        stopped_lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        stopped_worker = session.get(WorkerModel, ids["worker_id"])
        assert stopped_execution is not None
        assert sibling_execution is not None
        assert stopped_session is not None
        assert sibling_session_row is not None
        assert stopped_lease is not None
        assert stopped_worker is not None
        assert stopped_execution.status == "canceled"
        assert stopped_execution.stop_requested is True
        assert stopped_execution.active_lease_id is None
        assert sibling_execution.status == "running"
        assert sibling_execution.active_lease_id == sibling_ids["lease_id"]
        assert stopped_session.status == "closed"
        assert sibling_session_row.status == "active"
        assert stopped_lease.status == "released"
        assert stopped_worker.current_load == 0
        operation = session.query(GatewayOperationModel).filter_by(
            execution_id=ids["execution_id"],
            operation_type=GatewayOperationType.STOP_SESSION.value,
        ).one()
        assert operation.status == "succeeded"
        closed_session_version = stopped_session.version

    _request_continue(
        database,
        workspace_id,
        first_session.session_id,
        closed_session_version,
    )

    with database.session() as session:
        continuation = (
            session.query(ExecutionModel)
            .filter_by(source_execution_id=ids["execution_id"])
            .one()
        )
        assert continuation.status == "queued"
        assert continuation.source_session_id == first_session.session_id
        assert continuation.task_id == ids["task_id"]
        assert continuation.active_lease_id is None
        assert session.query(ExecutionModel).count() == 3
        task = session.get(TaskModel, ids["task_id"])
        assert task is not None
        assert task.status == "running"


def test_unknown_stop_blocks_continuation_and_preserves_running_session(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = UnknownStopAdapter()
    provider = FakeWorkspaceProvider()
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: NOW,
    )
    started = gateway.handle(
        _message(workspace_id, ids["execution_id"], ids["lease_id"])
    )
    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        assert execution is not None
        execution_version = execution.version

    _, stop_message = _request_stop(
        database, workspace_id, ids["execution_id"], execution_version
    )
    with pytest.raises(GatewayRecoveryRequired):
        gateway.handle(stop_message)

    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        stored_session = session.get(ExecutionSessionModel, started.session_id)
        assert execution is not None
        assert stored_session is not None
        assert execution.status == "running"
        assert execution.active_lease_id == ids["lease_id"]
        assert stored_session.status == "active"
        assert session.query(ExecutionModel).count() == 1
        operation = session.query(GatewayOperationModel).filter_by(
            execution_id=ids["execution_id"],
            operation_type=GatewayOperationType.STOP_SESSION.value,
        ).one()
        assert operation.status == "unknown"
        session_version = stored_session.version

    with pytest.raises(InvalidTransition, match="only a terminal Session"):
        _request_continue(
            database, workspace_id, started.session_id, session_version
        )
