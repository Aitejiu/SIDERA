from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.plan_transition_coordinator import (
    PlanTransitionCoordinator,
)
from sidera_control_plane.application.worker_gateway import (
    GatewayMessageError,
    GatewayRecoveryRequired,
    WorkerGateway,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.gateway import GatewayOperationType
from sidera_control_plane.domain.plan_transitions import (
    WorkerTransitionStatus,
    WriterState,
)
from sidera_control_plane.persistence.gateway_repository import (
    GatewayOperationRepository,
)
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    ContextPackageAcknowledgementModel,
    ExecutionBindingModel,
    ExecutionModel,
    ExecutionSessionModel,
    GatewayOperationModel,
    WorkerTransitionModel,
)
from sidera_control_plane.persistence.plan_transition_repository import (
    WorkerTransitionRepository,
)
from tests.integration.test_gateway_runner import NOW as GATEWAY_NOW
from tests.integration.test_gateway_runner import (
    FakeWorkspaceProvider,
    RecordingFakeAdapter,
    UnknownInputAdapter,
    UnknownStopAdapter,
    _message,
    _patch_materialization,
    _request_stop,
    _seed_assignment,
)
from tests.integration.test_revision_confirmation import (
    _base_plan,
    _confirm_command,
    _revision_payload,
    _seed_revision_confirmation,
)
from tests.integration.test_worker_transition_continuation import (
    NOW as CONTINUATION_NOW,
)
from tests.integration.test_worker_transition_continuation import (
    _activate_target,
    _seed_terminal_source,
    _seed_work_context,
)

pytestmark = pytest.mark.integration


def _gateway(database, adapter, provider) -> WorkerGateway:
    return WorkerGateway(
        lambda: _uow_factory(database),
        adapter_factories={"fake": lambda _instance: adapter},
        workspace_factories={"fake-workspace": lambda _instance: provider},
        clock=lambda: GATEWAY_NOW,
    )


def _uow_factory(database):
    from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

    return SqlAlchemyUnitOfWork(database)


def test_running_gateway_operation_is_frozen_before_external_call(
    database, workspace_id: UUID
) -> None:
    ids = _seed_assignment(database, workspace_id)
    operation_id = _operation_id(
        ids["execution_id"], GatewayOperationType.PREPARE_WORKSPACE
    )
    with database.session() as session:
        session.add(
            GatewayOperationModel(
                id=uuid4(),
                workspace_id=workspace_id,
                operation_id=operation_id,
                task_id=ids["task_id"],
                execution_id=ids["execution_id"],
                session_id=None,
                operation_type=GatewayOperationType.PREPARE_WORKSPACE.value,
                module_instance_id=ids["workspace_instance_id"],
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

    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    with pytest.raises(GatewayRecoveryRequired):
        _gateway(database, adapter, provider).handle(
            _message(workspace_id, ids["execution_id"], ids["lease_id"])
        )

    assert adapter.start_count == 0
    assert provider.prepare_count == 0
    with database.session() as session:
        operation = session.scalar(
            select(GatewayOperationModel).where(
                GatewayOperationModel.operation_id == operation_id
            )
        )
        execution = session.get(ExecutionModel, ids["execution_id"])
        assert operation is not None and execution is not None
        assert operation.status == "unknown"
        assert operation.attempt_count == 1
        assert execution.status == "leased"
        assert session.scalar(select(ExecutionSessionModel)) is None


def test_session_start_persistence_failure_is_unknown_and_not_replayed(
    database, workspace_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    original_save = GatewayOperationRepository.save
    injected = False

    def fail_after_start(self, operation, previous_version) -> None:
        nonlocal injected
        if (
            operation.operation_type is GatewayOperationType.START_SESSION
            and not injected
        ):
            injected = True
            raise RuntimeError("injected session persistence failure")
        original_save(self, operation, previous_version)

    monkeypatch.setattr(GatewayOperationRepository, "save", fail_after_start)
    gateway = _gateway(database, adapter, provider)
    message = _message(workspace_id, ids["execution_id"], ids["lease_id"])

    with pytest.raises(GatewayRecoveryRequired):
        gateway.handle(message)
    with pytest.raises(GatewayRecoveryRequired):
        gateway.handle(message)

    assert adapter.start_count == 1
    assert provider.prepare_count == 1
    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        operation = session.scalar(
            select(GatewayOperationModel).where(
                GatewayOperationModel.operation_id
                == _operation_id(ids["execution_id"], GatewayOperationType.START_SESSION)
            )
        )
        assert execution is not None and operation is not None
        assert execution.status == "leased"
        assert session.scalar(select(ExecutionSessionModel)) is None
        assert operation.status == "unknown"
        assert operation.attempt_count == 1


def test_unknown_input_is_not_replayed_after_gateway_restart(
    database, workspace_id: UUID
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = UnknownInputAdapter()
    provider = FakeWorkspaceProvider()
    gateway = _gateway(database, adapter, provider)
    message = _message(workspace_id, ids["execution_id"], ids["lease_id"])

    with pytest.raises(GatewayRecoveryRequired):
        gateway.handle(message)
    with pytest.raises(GatewayRecoveryRequired):
        gateway.handle(message)

    assert adapter.start_count == 1
    assert adapter.input_count == 1
    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        operation = session.scalar(
            select(GatewayOperationModel).where(
                GatewayOperationModel.execution_id == ids["execution_id"],
                GatewayOperationModel.operation_type
                == GatewayOperationType.SEND_INPUT.value,
            )
        )
        stored_session = session.scalar(select(ExecutionSessionModel))
        assert execution is not None and operation is not None and stored_session is not None
        assert execution.status == "running"
        assert stored_session.status == "active"
        assert operation.status == "unknown"
        assert operation.attempt_count == 1


def test_lost_stop_response_is_unknown_and_not_replayed(
    database, workspace_id: UUID
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = UnknownStopAdapter()
    provider = FakeWorkspaceProvider()
    gateway = _gateway(database, adapter, provider)
    started = gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))
    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        assert execution is not None
        _, stop_message = _request_stop(
            database, workspace_id, ids["execution_id"], execution.version
        )

    with pytest.raises(GatewayRecoveryRequired):
        gateway.handle(stop_message)
    with pytest.raises(GatewayRecoveryRequired):
        gateway.handle(stop_message)

    assert adapter.stop_count == 1
    with database.session() as session:
        operation = session.scalar(
            select(GatewayOperationModel).where(
                GatewayOperationModel.execution_id == ids["execution_id"],
                GatewayOperationModel.operation_type
                == GatewayOperationType.STOP_SESSION.value,
            )
        )
        execution = session.get(ExecutionModel, ids["execution_id"])
        stored_session = session.get(ExecutionSessionModel, started.session_id)
        assert operation is not None and execution is not None and stored_session is not None
        assert operation.status == "unknown"
        assert operation.attempt_count == 1
        assert execution.status == "running"
        assert stored_session.status == "active"


def test_patch_delivered_before_db_failure_is_unknown_and_not_replayed(
    database, workspace_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    gateway = _gateway(database, adapter, provider)
    started = gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))
    adapter.release_boundary(adapter.active_sessions()[0])
    transition_id = uuid4()
    materialization = _patch_materialization(workspace_id, transition_id)
    original_save = GatewayOperationRepository.save
    injected = False

    def fail_patch_commit(self, operation, previous_version) -> None:
        nonlocal injected
        if (
            operation.operation_type is GatewayOperationType.APPLY_CONTEXT_PATCH
            and not injected
        ):
            injected = True
            raise RuntimeError("injected patch commit failure")
        original_save(self, operation, previous_version)

    monkeypatch.setattr(GatewayOperationRepository, "save", fail_patch_commit)
    kwargs = {
        "workspace_id": workspace_id,
        "execution_id": ids["execution_id"],
        "session_id": started.session_id,
        "lease_id": ids["lease_id"],
        "fencing_token": 1,
        "worker_transition_id": transition_id,
        "action_step": "apply_context_patch",
        "logical_attempt": 1,
        "materialization": materialization,
    }

    first = gateway.apply_context_patch(**kwargs)
    second = gateway.apply_context_patch(**kwargs)

    assert first.outcome == "unknown"
    assert second.outcome == "unknown"
    assert len(adapter.applied_context_patches) == 1
    with database.session() as session:
        operation = session.scalar(
            select(GatewayOperationModel).where(
                GatewayOperationModel.operation_type
                == GatewayOperationType.APPLY_CONTEXT_PATCH.value
            )
        )
        assert operation is not None
        assert operation.status == "unknown"
        assert operation.attempt_count == 1
        assert operation.failure_code == "injected_patch_commit_failure"


def test_old_fencing_token_cannot_start_or_write_runtime_state(
    database, workspace_id: UUID
) -> None:
    ids = _seed_assignment(database, workspace_id)
    with database.session() as session:
        session.execute(
            update(AssignmentLeaseModel)
            .where(AssignmentLeaseModel.id == ids["lease_id"])
            .values(fencing_token=2)
        )
        session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == ids["execution_id"])
            .values(last_fencing_token=2)
        )
        session.execute(
            update(ExecutionBindingModel)
            .where(ExecutionBindingModel.execution_id == ids["execution_id"])
            .values(fencing_token=2)
        )
        session.commit()

    adapter = RecordingFakeAdapter()
    provider = FakeWorkspaceProvider()
    with pytest.raises(GatewayMessageError):
        _gateway(database, adapter, provider).handle(
            _message(workspace_id, ids["execution_id"], ids["lease_id"])
        )

    assert adapter.start_count == 0
    assert provider.prepare_count == 0
    with database.session() as session:
        execution = session.get(ExecutionModel, ids["execution_id"])
        lease = session.get(AssignmentLeaseModel, ids["lease_id"])
        binding = session.scalar(
            select(ExecutionBindingModel).where(
                ExecutionBindingModel.execution_id == ids["execution_id"]
            )
        )
        assert execution is not None and lease is not None and binding is not None
        assert execution.status == "leased"
        assert execution.last_fencing_token == 2
        assert lease.fencing_token == 2
        assert binding.fencing_token == 2
        assert session.scalar(select(ExecutionSessionModel)) is None


def _prepare_active_continuation(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> tuple[dict[str, object], UUID, PlanTransitionCoordinator]:
    snapshot = _base_plan(workspace_id)
    payload = replace(
        _revision_payload(snapshot),
        execution_intents=tuple(
            replace(
                intent,
                expected_artifact_refs=("artifact_type:diff",),
                required_evidence_refs=("acceptance:implementation",),
            )
            for intent in _revision_payload(snapshot).execution_intents
        ),
    )
    seed = _seed_revision_confirmation(
        database,
        workspace_id,
        snapshot=snapshot,
        payload=payload,
    )
    source_task_id = snapshot.subtasks[0].task_id
    assert source_task_id is not None
    _seed_terminal_source(
        uow_factory,
        seed["worker_item"].id,
        source_task_id,
        workspace_id,
    )
    _seed_work_context(uow_factory, seed["request"].id, workspace_id)
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        response = client.post(
            "/api/v1/commands",
            json=_confirm_command(seed, idempotency_key="r6-1-2-failure-continuation"),
        )
    assert response.status_code == 201, response.text

    coordinator = PlanTransitionCoordinator(
        uow_factory=uow_factory,
        clock=lambda: CONTINUATION_NOW,
        owner_id="failure-transition-test",
    )
    assert coordinator.run_once(limit=10).failed == 0
    from sidera_control_plane.application.activation_reconciler import (
        ActivationReconciler,
    )
    from sidera_control_plane.context_package_worker import ContextPackageWorker
    from tests.integration.test_context_package_worker import EmptyCatalog

    activation = ActivationReconciler(
        uow_factory=uow_factory,
        clock=lambda: CONTINUATION_NOW,
        owner_id="failure-activation-test",
    ).run_once(limit=10)
    assert activation.failed == 0 and activation.materialized == 1
    target_execution_id = activation.execution_ids[0]
    package = ContextPackageWorker(
        uow_factory=uow_factory,
        catalog=EmptyCatalog(),
        owner_id="failure-context-test",
        clock=lambda: CONTINUATION_NOW,
    ).run_once(limit=10)
    assert package.claimed == 1 and package.ready == 1
    assert coordinator.run_once(limit=10).failed == 0
    _activate_target(
        uow_factory,
        target_execution_id,
        seed["worker_item"].id,
        workspace_id,
    )
    return seed, target_execution_id, coordinator


def test_lost_target_context_ack_freezes_transition_after_session_start(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    seed, target_execution_id, coordinator = _prepare_active_continuation(
        database, database_url, workspace_id, uow_factory
    )
    with database.session() as session:
        acknowledgement = session.scalar(
            select(ContextPackageAcknowledgementModel).where(
                ContextPackageAcknowledgementModel.consumer_id == target_execution_id
            )
        )
        assert acknowledgement is not None
        session.delete(acknowledgement)
        session.commit()

    result = coordinator.run_once(limit=10)

    assert result.failed == 1
    with database.session() as session:
        transition = session.scalar(
            select(WorkerTransitionModel).where(
                WorkerTransitionModel.worker_item_id == seed["worker_item"].id
            )
        )
        target = session.get(ExecutionModel, target_execution_id)
        target_session = session.scalar(
            select(ExecutionSessionModel).where(
                ExecutionSessionModel.execution_id == target_execution_id
            )
        )
        assert transition is not None and target is not None and target_session is not None
        assert transition.status == WorkerTransitionStatus.BLOCKED_UNKNOWN.value
        assert transition.writer_state == WriterState.UNKNOWN.value
        assert transition.failure_code == "target_context_ack_missing"
        assert target.status == "running"
        assert target_session.status == "active"


def test_new_session_survives_coordinator_crash_and_reconciles_on_restart(
    database, database_url: str, workspace_id: UUID, uow_factory, monkeypatch
) -> None:
    seed, target_execution_id, coordinator = _prepare_active_continuation(
        database, database_url, workspace_id, uow_factory
    )
    original_save = WorkerTransitionRepository.save

    def crash_after_session_start(self, transition, previous_version) -> None:
        if transition.status is WorkerTransitionStatus.CONVERGED:
            raise RuntimeError("injected coordinator crash")
        original_save(self, transition, previous_version)

    monkeypatch.setattr(WorkerTransitionRepository, "save", crash_after_session_start)
    failed = coordinator.run_once(limit=10)
    assert failed.failed == 1
    monkeypatch.setattr(WorkerTransitionRepository, "save", original_save)

    recovered = coordinator.run_once(limit=10)

    assert recovered.converged == 1
    with database.session() as session:
        transition = session.scalar(
            select(WorkerTransitionModel).where(
                WorkerTransitionModel.worker_item_id == seed["worker_item"].id
            )
        )
        target = session.get(ExecutionModel, target_execution_id)
        target_session = session.scalar(
            select(ExecutionSessionModel).where(
                ExecutionSessionModel.execution_id == target_execution_id
            )
        )
        assert transition is not None and target is not None and target_session is not None
        assert transition.status == WorkerTransitionStatus.CONVERGED.value
        assert transition.writer_state == WriterState.CLOSED.value
        assert target.status == "running"
        assert target_session.status == "active"


def _operation_id(execution_id: UUID, operation_type: GatewayOperationType) -> UUID:
    from sidera_control_plane.domain.gateway import gateway_operation_id

    return gateway_operation_id(execution_id, operation_type)
