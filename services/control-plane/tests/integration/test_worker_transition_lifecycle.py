from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.plan_transition_coordinator import (
    PlanTransitionCoordinator,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.gateway import (
    GatewayOperation,
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    GatewayOperationModel,
    OutboxMessageModel,
    WorkerItemModel,
    WorkerTransitionModel,
)
from tests.integration.test_revision_confirmation import (
    _base_plan,
    _confirm_command,
    _retire_payload,
    _seed_revision_confirmation,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _seed_active_runtime(database, worker_item_id: UUID, workspace_id: UUID) -> dict[str, UUID]:
    execution_id = uuid4()
    worker_id = uuid4()
    lease_id = uuid4()
    session_id = uuid4()
    worker = Worker.register(
        worker_id=worker_id,
        workspace_id=workspace_id,
        adapter_type="fake",
        display_name="retire-test-worker",
        capabilities=frozenset({"python"}),
        capacity=1,
        cost_per_hour_microunits=0,
        startup_latency_ms=0,
        now=NOW,
    )
    worker.reserve()
    worker.mark_running()
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=UUID("50000000-0000-0000-0000-000000000001"),
        worker_item_id=worker_item_id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=1,
        purpose="Run the current implementation",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=worker_id,
        not_before=NOW,
    )
    lease = AssignmentLease.issue(
        lease_id=lease_id,
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        issued_at=NOW,
        expires_at=NOW.replace(hour=13),
        fencing_token=1,
        assignment_reason={"test": "plan-transition-retire"},
    )
    session = ExecutionSession.start(
        session_id=session_id,
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        adapter_session_id="retire-test-session",
        now=NOW,
    )
    from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.workers.add(worker)
        uow.session.flush()
        uow.executions.add(execution)
        uow.session.flush()
        previous_version = execution.version
        execution.assign(
            lease_id=lease.id,
            worker_id=worker.id,
            fencing_token=lease.fencing_token,
        )
        execution.start(
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            now=NOW,
        )
        uow.leases.add(lease)
        uow.executions.save(execution, previous_version)
        uow.sessions.add(session)
        uow.session.flush()
    return {
        "execution_id": execution_id,
        "worker_id": worker_id,
        "lease_id": lease_id,
        "session_id": session_id,
    }


def test_retire_requires_exact_stop_confirmation_before_worker_item_retired(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    seed = _seed_revision_confirmation(
        database,
        workspace_id,
        snapshot=_base_plan(workspace_id),
        payload=replace(
            _retire_payload(_base_plan(workspace_id)),
            execution_intents=tuple(
                replace(
                    intent,
                    expected_artifact_refs=("artifact_type:diff",),
                    required_evidence_refs=("acceptance:implementation",),
                )
                for intent in _retire_payload(_base_plan(workspace_id)).execution_intents
            ),
        ),
    )
    runtime = _seed_active_runtime(database, seed["worker_item"].id, workspace_id)
    from fastapi.testclient import TestClient

    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        response = client.post(
            "/api/v1/commands",
            json=_confirm_command(seed, idempotency_key="r6-1-2-retire-stop"),
        )
    assert response.status_code == 201, response.text

    coordinator = PlanTransitionCoordinator(
        uow_factory=uow_factory,
        clock=lambda: NOW,
        owner_id="retire-test",
    )
    first = coordinator.run_once(limit=10)
    assert first.failed == 0

    with database.session() as session:
        retire = session.scalar(
            select(WorkerTransitionModel).where(
                WorkerTransitionModel.action == "retire"
            )
        )
        assert retire is not None
        assert retire.status == "waiting_safe_boundary"
        worker_item = session.get(WorkerItemModel, seed["worker_item"].id)
        assert worker_item is not None
        assert worker_item.status == "active"
        execution = session.get(ExecutionModel, runtime["execution_id"])
        assert execution is not None
        assert execution.stop_requested is True
        stop_message = session.scalar(
            select(OutboxMessageModel).where(
                OutboxMessageModel.topic == "execution.ExecutionStopRequested"
            )
        )
        assert stop_message is not None
        assert stop_message.payload["data"]["session_id"] == str(runtime["session_id"])
        assert stop_message.payload["data"]["lease_id"] == str(runtime["lease_id"])

    repeated = coordinator.run_once(limit=10)
    assert repeated.failed == 0
    assert repeated.converged == 0
    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(OutboxMessageModel).where(
                OutboxMessageModel.topic == "execution.ExecutionStopRequested"
            )
        ) == 1

    with uow_factory() as uow:
        session = uow.sessions.get_any(runtime["session_id"], workspace_id, for_update=True)
        lease = uow.leases.get(runtime["lease_id"], workspace_id, for_update=True)
        execution = uow.executions.get(runtime["execution_id"], workspace_id, for_update=True)
        worker = uow.workers.get(runtime["worker_id"], workspace_id, for_update=True)
        assert session is not None and lease is not None and execution is not None and worker is not None
        session_previous = session.version
        session.close(lost=False, now=NOW)
        uow.sessions.save(session, session_previous)
        lease_previous = lease.version
        lease.revoke(now=NOW)
        uow.leases.save(lease, lease_previous)
        worker_previous = worker.version
        worker.release()
        uow.workers.save(worker, worker_previous)
        execution_previous = execution.version
        execution.cancel(now=NOW)
        uow.executions.save(execution, execution_previous)

    second = coordinator.run_once(limit=10)
    assert second.failed == 0
    with database.session() as session:
        worker_item = session.get(WorkerItemModel, seed["worker_item"].id)
        assert worker_item is not None
        assert worker_item.status == "retired"
        retire = session.scalar(
            select(WorkerTransitionModel).where(
                WorkerTransitionModel.action == "retire"
            )
        )
        assert retire is not None
        assert retire.status == "converged"


def test_unknown_stop_freezes_transition_without_replaying_stop(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    seed = _seed_revision_confirmation(
        database,
        workspace_id,
        snapshot=_base_plan(workspace_id),
        payload=replace(
            _retire_payload(_base_plan(workspace_id)),
            execution_intents=tuple(
                replace(
                    intent,
                    expected_artifact_refs=("artifact_type:diff",),
                    required_evidence_refs=("acceptance:implementation",),
                )
                for intent in _retire_payload(_base_plan(workspace_id)).execution_intents
            ),
        ),
    )
    runtime = _seed_active_runtime(database, seed["worker_item"].id, workspace_id)
    from fastapi.testclient import TestClient

    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        response = client.post(
            "/api/v1/commands",
            json=_confirm_command(seed, idempotency_key="r6-1-2-retire-unknown"),
        )
    assert response.status_code == 201, response.text

    coordinator = PlanTransitionCoordinator(
        uow_factory=uow_factory,
        clock=lambda: NOW,
        owner_id="retire-unknown-test",
    )
    first = coordinator.run_once(limit=10)
    assert first.failed == 0

    with uow_factory() as uow:
        execution = uow.executions.get(runtime["execution_id"], workspace_id)
        lease = uow.leases.get(runtime["lease_id"], workspace_id)
        assert execution is not None and lease is not None
        operation = GatewayOperation.create(
            id=uuid4(),
            operation_id=gateway_operation_id(
                execution.id, GatewayOperationType.STOP_SESSION
            ),
            workspace_id=workspace_id,
            task_id=execution.task_id,
            execution_id=execution.id,
            session_id=runtime["session_id"],
            operation_type=GatewayOperationType.STOP_SESSION,
            module_instance_id=None,
            config_version=None,
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            now=NOW,
        )
        uow.gateway_operations.add(operation)
        uow.session.flush()
        previous_operation_version = operation.version
        operation.start(now=NOW)
        operation.mark_unknown(failure_code="gateway_outcome_unknown", now=NOW)
        uow.gateway_operations.save(operation, previous_operation_version)

    blocked = coordinator.run_once(limit=10)
    assert blocked.failed == 1
    assert blocked.converged == 0

    with database.session() as session:
        transition = session.scalar(
            select(WorkerTransitionModel).where(
                WorkerTransitionModel.worker_item_id == seed["worker_item"].id
            )
        )
        assert transition is not None
        assert transition.status == "blocked_unknown"
        assert transition.writer_state == "unknown"
        assert transition.failure_code == "stop_result_unknown"
        worker_item = session.get(WorkerItemModel, seed["worker_item"].id)
        execution = session.get(ExecutionModel, runtime["execution_id"])
        operation = session.scalar(
            select(GatewayOperationModel).where(
                GatewayOperationModel.operation_id
                == gateway_operation_id(
                    runtime["execution_id"], GatewayOperationType.STOP_SESSION
                )
            )
        )
        assert worker_item is not None and execution is not None and operation is not None
        assert worker_item.status == "active"
        assert execution.status == "running"
        assert execution.stop_requested is True
        assert operation.status == "unknown"
        assert session.scalar(
            select(func.count()).select_from(OutboxMessageModel).where(
                OutboxMessageModel.topic == "execution.ExecutionStopRequested"
            )
        ) == 1
