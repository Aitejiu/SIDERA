from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from sidera_control_plane.application.reconciler import Reconciler
from sidera_control_plane.application.worker_gateway import (
    GatewayMessageError,
    GatewayObsoleteAssignmentError,
    GatewayTerminalAssignmentError,
    WorkerGateway,
    _operation_id,
)
from sidera_control_plane.domain.gateway import GatewayOperationType
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    CommandModel,
    EventModel,
    ExecutionBindingModel,
    ExecutionModel,
    ExecutionSessionModel,
    GatewayOperationModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    ProjectionChangeModel,
    TaskModel,
    WorkerItemModel,
    WorkerModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.integration.test_gateway_runner import (
    NOW,
    FakeWorkspaceProvider,
    RecordingFakeAdapter,
    _capabilities,
    _message,
    _seed_assignment,
)

pytestmark = pytest.mark.integration


def _gateway(
    database: Database,
    adapter: RecordingFakeAdapter | None = None,
    provider: FakeWorkspaceProvider | None = None,
) -> WorkerGateway:
    selected_adapter = adapter or RecordingFakeAdapter()
    selected_provider = provider or FakeWorkspaceProvider()
    return WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={"fake": lambda _instance: selected_adapter},
        workspace_factories={"fake-workspace": lambda _instance: selected_provider},
        clock=lambda: NOW,
    )


def _load_context_error(
    database: Database, workspace_id: UUID, ids: dict[str, UUID]
) -> GatewayMessageError:
    gateway = _gateway(database)
    with pytest.raises(GatewayMessageError) as caught:
        gateway._load_context(
            _message(workspace_id, ids["execution_id"], ids["lease_id"])
        )
    return caught.value


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("binding_missing", "runtime_binding_missing"),
        ("binding_fence_mismatch", "runtime_binding_missing"),
        ("config_version_mismatch", "runtime_binding_version_mismatch"),
        ("definition_type_mismatch", "runtime_module_type_mismatch"),
        ("adapter_provider_mismatch", "runtime_adapter_provider_mismatch"),
        ("capability_mismatch", "runtime_capability_mismatch"),
    ),
)
def test_gateway_loads_only_the_exact_runtime_binding(
    database: Database,
    workspace_id: UUID,
    mutation: str,
    expected_code: str,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    capabilities = _capabilities()
    with database.session() as session:
        binding = session.query(ExecutionBindingModel).one()
        if mutation == "binding_missing":
            session.delete(binding)
        elif mutation == "binding_fence_mismatch":
            binding.fencing_token += 1
        elif mutation == "config_version_mismatch":
            session.execute(
                update(ModuleInstanceModel)
                .where(ModuleInstanceModel.id == ids["adapter_instance_id"])
                .values(config_version=2)
            )
        elif mutation == "definition_type_mismatch":
            adapter_instance = session.get(
                ModuleInstanceModel, ids["adapter_instance_id"]
            )
            assert adapter_instance is not None
            session.execute(
                update(ModuleDefinitionModel)
                .where(ModuleDefinitionModel.id == adapter_instance.definition_id)
                .values(module_type="workspace_provider")
            )
        elif mutation == "adapter_provider_mismatch":
            session.execute(
                update(WorkerModel)
                .where(WorkerModel.id == ids["worker_id"])
                .values(adapter_type="another-adapter")
            )
        elif mutation == "capability_mismatch":
            binding.capability_snapshot = {
                "input": "unsupported",
                "idempotency": capabilities.idempotency.value,
                "resume": capabilities.resume.value,
                "stop": capabilities.stop.value,
                "sandbox": capabilities.sandbox.value,
                "stability": capabilities.stability.value,
            }
        else:
            raise AssertionError(f"unsupported mutation: {mutation}")
        session.commit()

    error = _load_context_error(database, workspace_id, ids)

    assert error.code == expected_code
    if mutation == "binding_fence_mismatch":
        assert type(error) is GatewayMessageError
        with database.session() as session:
            execution = session.get(ExecutionModel, ids["execution_id"])
            lease = session.get(AssignmentLeaseModel, ids["lease_id"])
            assert execution is not None and execution.status == "leased"
            assert lease is not None and lease.status == "active"


def test_gateway_does_not_fallback_to_same_name_module_instance(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    with database.session() as session:
        original = session.get(ModuleInstanceModel, ids["adapter_instance_id"])
        assert original is not None
        session.add(
            ModuleInstanceModel(
                id=uuid4(),
                workspace_id=workspace_id,
                definition_id=original.definition_id,
                instance_name=original.instance_name,
                config_version=original.config_version + 1,
                config_namespace=original.config_namespace,
                config=dict(original.config),
                secret_refs=list(original.secret_refs),
                lifecycle=original.lifecycle,
            )
        )
        session.commit()

    context = _gateway(database)._load_context(
        _message(workspace_id, ids["execution_id"], ids["lease_id"])
    )

    assert context.adapter_instance.id == ids["adapter_instance_id"]


def test_missing_binding_start_is_audited_without_a_fallback(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    command_id = uuid4()
    with database.session() as session:
        session.add(
            CommandModel(
                id=command_id,
                workspace_id=workspace_id,
                command_type="ExecutionLeased",
                actor_type="scheduler",
                actor_id="scheduler",
                target_type="execution",
                target_id=ids["execution_id"],
                payload={},
                expected_version=1,
                idempotency_key=f"gateway-missing-binding:{command_id}",
                request_hash="0" * 64,
                correlation_id=command_id,
                status="applied",
                result={},
                rejection_reason=None,
            )
        )
        session.delete(session.query(ExecutionBindingModel).one())
        session.commit()

    gateway = _gateway(database)
    message = _message(workspace_id, ids["execution_id"], ids["lease_id"])
    message = replace(
        message,
        payload={**message.payload, "causation_id": str(command_id)},
    )
    with pytest.raises(GatewayMessageError) as caught:
        gateway.handle(message)
    assert caught.value.code == "runtime_binding_missing"
    with pytest.raises(GatewayMessageError):
        gateway.handle(message)

    operation_id = _operation_id(
        ids["execution_id"], GatewayOperationType.PREPARE_WORKSPACE
    )
    with database.session() as session:
        operation = (
            session.query(GatewayOperationModel)
            .filter_by(operation_id=operation_id)
            .one()
        )
        assert operation.status == "failed"
        assert operation.failure_code == "runtime_binding_missing"
        events = (
            session.query(EventModel)
            .filter_by(event_type="ExecutionRuntimeStartBlocked")
            .all()
        )
        assert len(events) == 1
        assert events[0].payload == {
            "execution_id": str(ids["execution_id"]),
            "binding_id": None,
            "error_code": "runtime_binding_missing",
        }


def test_existing_binding_can_start_with_draining_modules(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    with database.session() as session:
        adapter_instance = session.get(
            ModuleInstanceModel, ids["adapter_instance_id"]
        )
        workspace_instance = session.get(
            ModuleInstanceModel, ids["workspace_instance_id"]
        )
        assert adapter_instance is not None
        assert workspace_instance is not None
        session.execute(
            update(ModuleDefinitionModel)
            .where(
                ModuleDefinitionModel.id.in_(
                    [adapter_instance.definition_id, workspace_instance.definition_id]
                )
            )
            .values(lifecycle="draining")
        )
        session.execute(
            update(ModuleInstanceModel)
            .where(
                ModuleInstanceModel.id.in_(
                    [adapter_instance.id, workspace_instance.id]
                )
            )
            .values(lifecycle="draining")
        )
        session.commit()

    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    result = _gateway(database, adapter, provider).handle(
        _message(workspace_id, ids["execution_id"], ids["lease_id"])
    )

    assert result.session_id
    assert adapter.start_count == 1
    assert provider.prepare_count == 1


def test_gateway_session_start_invalidates_its_worker_item_workspace(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    worker_item_id = uuid4()
    causation_id = uuid4()
    with database.session() as session:
        session.add(
            WorkerItemModel(
                id=worker_item_id,
                workspace_id=workspace_id,
                root_task_id=ids["task_id"],
                stable_key="session-start",
                title="Session startup",
                responsibility="Start a writable worker session",
                status="active",
                context_version=1,
                acknowledged_context_version=1,
                derived_from_worker_item_ids=[],
                version=1,
            )
        )
        session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == ids["execution_id"])
            .values(worker_item_id=worker_item_id)
        )
        session.add(
            CommandModel(
                id=causation_id,
                workspace_id=workspace_id,
                command_type="ExecutionLeased",
                actor_type="scheduler",
                actor_id="scheduler",
                target_type="execution",
                target_id=ids["execution_id"],
                payload={},
                expected_version=1,
                idempotency_key=f"gateway-session-start:{causation_id}",
                request_hash="0" * 64,
                correlation_id=causation_id,
                status="applied",
                result={},
                rejection_reason=None,
            )
        )
        session.commit()

    message = _message(workspace_id, ids["execution_id"], ids["lease_id"])
    message = replace(
        message,
        payload={**message.payload, "causation_id": str(causation_id)},
    )
    result = _gateway(database).handle(message)

    with database.session() as session:
        execution_started_event = (
            session.query(EventModel)
            .filter_by(
                aggregate_type="execution",
                aggregate_id=ids["execution_id"],
                event_type="ExecutionStarted",
            )
            .one()
        )
        assert execution_started_event.payload["status"] == "running"
        started_event = (
            session.query(EventModel)
            .filter_by(
                aggregate_type="execution_session",
                aggregate_id=result.session_id,
                event_type="ExecutionSessionStarted",
            )
            .one()
        )
        assert started_event.payload["execution_id"] == str(ids["execution_id"])
        invalidations = (
            session.query(ProjectionChangeModel)
            .filter_by(
                workspace_id=workspace_id,
                projection_type="worker_item_workspace",
                resource_id=worker_item_id,
                change_type="ExecutionSessionStarted",
            )
            .all()
        )
        assert len(invalidations) == 1


def test_session_budget_exhaustion_fails_leased_execution_before_adapter_start(
    database: Database,
    workspace_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = RecordingFakeAdapter()
    gateway = _gateway(database, adapter)
    gateway._acceptance_guard = object()  # type: ignore[assignment]

    def reject_session_budget(**values: object) -> None:
        assert values["boundary"] == "session"
        raise GatewayMessageError(
            "test-only deterministic budget rejection", code="budget_exhausted"
        )

    monkeypatch.setattr(gateway, "_begin_acceptance_worker_call", reject_session_budget)
    message = _message(workspace_id, ids["execution_id"], ids["lease_id"])

    with pytest.raises(GatewayTerminalAssignmentError) as caught:
        gateway.handle(message)

    assert caught.value.code == "acceptance_budget_exhausted"
    assert adapter.start_count == 0
    assert adapter.input_count == 0
    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        worker = session.get(WorkerModel, ids["worker_id"])
        task = session.get(TaskModel, ids["task_id"])
        assert execution is not None
        assert execution.status == "failed"
        assert execution.failure_code == "acceptance_budget_exhausted"
        assert execution.started_at is None
        assert execution.active_lease_id is None
        assert lease is not None
        assert lease.status == "released"
        assert worker is not None
        assert worker.current_load == 0
        assert worker.status == "idle"
        assert task is not None
        assert task.status == "waiting"
        assert session.query(ExecutionSessionModel).count() == 0
        operation = session.query(GatewayOperationModel).filter_by(
            operation_id=_operation_id(
                ids["execution_id"], GatewayOperationType.START_SESSION
            )
        ).one()
        assert operation.status == "failed"
        assert operation.failure_code == "budget_exhausted"
        assert session.query(EventModel).filter_by(
            event_type="ExecutionRuntimeStartBlocked"
        ).count() == 1

    reconciled = Reconciler(
        lambda: SqlAlchemyUnitOfWork(database),
        clock=lambda: NOW + timedelta(seconds=31),
    ).run_once(limit=10)
    assert reconciled.expired_leases == 0
    assert reconciled.retries_queued == 0


def test_old_assignment_is_obsolete_after_reconciler_queues_a_new_attempt(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    reconciled = Reconciler(
        lambda: SqlAlchemyUnitOfWork(database),
        clock=lambda: NOW + timedelta(minutes=6),
    ).run_once(limit=10)
    assert reconciled.retries_queued == 1

    adapter = RecordingFakeAdapter()
    gateway = _gateway(database, adapter)
    with pytest.raises(GatewayObsoleteAssignmentError):
        gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    assert adapter.start_count == 0
    with database.session() as session:
        attempts = session.query(ExecutionModel).filter_by(
            lineage_id=ids["execution_id"]
        ).order_by(ExecutionModel.attempt.asc()).all()
        assert [attempt.status for attempt in attempts] == ["lost", "queued"]
        assert attempts[1].attempt == 2
        old_lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        assert old_lease is not None
        assert old_lease.status == "expired"


def test_released_assignment_is_obsolete_without_mutating_execution(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    with database.session() as session:
        session.execute(
            update(AssignmentLeaseModel)
            .where(AssignmentLeaseModel.id == ids["lease_id"])
            .values(status="released", ended_at=NOW, version=2)
        )
        session.commit()

    adapter = RecordingFakeAdapter()
    gateway = _gateway(database, adapter)
    with pytest.raises(GatewayObsoleteAssignmentError):
        gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))

    assert adapter.start_count == 0
    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        worker = session.get(WorkerModel, ids["worker_id"])
        assert execution is not None
        assert execution.status == "leased"
        assert execution.active_lease_id == ids["lease_id"]
        assert lease is not None and lease.status == "released"
        assert worker is not None and worker.current_load == 1


def test_disabled_start_is_failed_and_audited_once(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    command_id = uuid4()
    with database.session() as session:
        session.add(
            CommandModel(
                id=command_id,
                workspace_id=workspace_id,
                command_type="ExecutionLeased",
                actor_type="scheduler",
                actor_id="scheduler",
                target_type="execution",
                target_id=ids["execution_id"],
                payload={},
                expected_version=1,
                idempotency_key=f"gateway-binding:{command_id}",
                request_hash="0" * 64,
                correlation_id=command_id,
                status="applied",
                result={},
                rejection_reason=None,
            )
        )
        workspace_instance = session.get(
            ModuleInstanceModel, ids["workspace_instance_id"]
        )
        assert workspace_instance is not None
        session.execute(
            update(ModuleDefinitionModel)
            .where(ModuleDefinitionModel.id == workspace_instance.definition_id)
            .values(lifecycle="disabled")
        )
        session.commit()

    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = _gateway(database, adapter, provider)
    message = _message(workspace_id, ids["execution_id"], ids["lease_id"])
    message = replace(
        message,
        payload={**message.payload, "causation_id": str(command_id)},
    )

    with pytest.raises(GatewayMessageError) as caught:
        gateway.handle(message)
    assert caught.value.code == "runtime_module_disabled"
    with pytest.raises(GatewayMessageError) as duplicate:
        gateway.handle(message)
    assert duplicate.value.code == "runtime_module_disabled"
    assert adapter.start_count == 0
    assert provider.prepare_count == 0

    operation_id = _operation_id(
        ids["execution_id"], GatewayOperationType.PREPARE_WORKSPACE
    )
    with database.session() as session:
        operation = (
            session.query(GatewayOperationModel)
            .filter_by(operation_id=operation_id)
            .one()
        )
        assert operation.status == "failed"
        assert operation.failure_code == "runtime_module_disabled"
        events = (
            session.query(EventModel)
            .filter_by(event_type="ExecutionRuntimeStartBlocked")
            .all()
        )
        assert len(events) == 1
        event = events[0]
        assert event.aggregate_type == "gateway_operation"
        assert event.aggregate_id == operation_id
        assert event.aggregate_version == operation.version
        assert event.causation_id == command_id
        assert event.payload == {
            "execution_id": str(ids["execution_id"]),
            "binding_id": str(session.query(ExecutionBindingModel).one().id),
            "error_code": "runtime_module_disabled",
        }


def test_disabled_adapter_blocks_session_start_after_workspace_prepare(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_assignment(database, workspace_id)
    command_id = uuid4()
    with database.session() as session:
        session.add(
            CommandModel(
                id=command_id,
                workspace_id=workspace_id,
                command_type="ExecutionLeased",
                actor_type="scheduler",
                actor_id="scheduler",
                target_type="execution",
                target_id=ids["execution_id"],
                payload={},
                expected_version=1,
                idempotency_key=f"gateway-disabled-adapter:{command_id}",
                request_hash="0" * 64,
                correlation_id=command_id,
                status="applied",
                result={},
                rejection_reason=None,
            )
        )
        adapter_instance = session.get(
            ModuleInstanceModel, ids["adapter_instance_id"]
        )
        assert adapter_instance is not None
        session.execute(
            update(ModuleDefinitionModel)
            .where(ModuleDefinitionModel.id == adapter_instance.definition_id)
            .values(lifecycle="disabled")
        )
        session.commit()

    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = _gateway(database, adapter, provider)
    message = _message(workspace_id, ids["execution_id"], ids["lease_id"])
    message = replace(
        message,
        payload={**message.payload, "causation_id": str(command_id)},
    )

    with pytest.raises(GatewayMessageError) as caught:
        gateway.handle(message)
    assert caught.value.code == "runtime_module_disabled"
    with pytest.raises(GatewayMessageError):
        gateway.handle(message)
    assert provider.prepare_count == 1
    assert adapter.start_count == 0

    operation_id = _operation_id(
        ids["execution_id"], GatewayOperationType.START_SESSION
    )
    with database.session() as session:
        operation = (
            session.query(GatewayOperationModel)
            .filter_by(operation_id=operation_id)
            .one()
        )
        assert operation.status == "failed"
        assert operation.failure_code == "runtime_module_disabled"
        events = (
            session.query(EventModel)
            .filter_by(event_type="ExecutionRuntimeStartBlocked")
            .all()
        )
        assert len(events) == 1
        assert events[0].aggregate_id == operation_id
