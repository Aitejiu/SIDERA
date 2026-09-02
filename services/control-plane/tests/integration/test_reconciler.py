from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from tests.integration.test_scheduler import register_runtime_modules_and_configure_worker

from sidera_control_plane.application.reconciler import Reconciler
from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    CommandModel,
    EventModel,
    ExecutionModel,
    ExecutionSessionModel,
    TaskModel,
    WorkerModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def seed_running_execution(
    database: Database,
    workspace_id: UUID,
    *,
    max_attempts: int,
    worker_item_id: UUID | None = None,
    adapter_preferences: tuple[str, ...] = (),
    adapter_forbidden: tuple[str, ...] = (),
) -> tuple[Execution, Worker, UUID]:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="Reconciler task",
        description="Lease recovery",
        acceptance_criteria=("lease expiry is recovered",),
        priority=50,
        requested_by="system:test",
    )
    task.mark_ready()
    worker = Worker.register(
        worker_id=uuid4(),
        workspace_id=workspace_id,
        adapter_type="fake",
        display_name="Reconciler worker",
        capabilities=frozenset({"code", "python"}),
        capacity=1,
        cost_per_hour_microunits=1,
        startup_latency_ms=1,
        now=NOW,
    )
    worker_item = (
        WorkerItem.create(
            item_id=worker_item_id,
            workspace_id=workspace_id,
            root_task_id=task.id,
            title="Reconciler WorkerItem",
            responsibility="Keep retry context stable.",
            now=NOW,
        )
        if worker_item_id is not None
        else None
    )
    execution_id = uuid4()
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=task.id,
        worker_item_id=worker_item_id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=max_attempts,
        purpose="lease recovery",
        capability_requirements=frozenset({"code", "python"}),
        pinned_worker_id=worker.id,
        not_before=NOW,
        adapter_preferences=adapter_preferences,
        adapter_forbidden=adapter_forbidden,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(task)
        uow.workers.add(worker)
        register_runtime_modules_and_configure_worker(uow, worker)
        if worker_item is not None:
            uow.worker_items.add(worker_item)
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.executions.add(execution)

    scheduler = Scheduler(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        policy=LowestCostWorkerPolicy(),
        clock=lambda: NOW,
        assignment_lease_seconds=30,
        worker_heartbeat_timeout_seconds=45,
    )
    assert scheduler.run_once(limit=1).assigned == 1

    with SqlAlchemyUnitOfWork(database) as uow:
        assigned = uow.executions.get(execution.id, workspace_id)
        assert assigned is not None
        assert assigned.active_lease_id is not None
        lease = uow.leases.get(assigned.active_lease_id, workspace_id)
        assert lease is not None
        assigned_worker = uow.workers.get(worker.id, workspace_id)
        assert assigned_worker is not None
        assigned_task = uow.tasks.get(task.id, workspace_id)
        assert assigned_task is not None
        previous_execution_version = assigned.version
        previous_worker_version = assigned_worker.version
        previous_task_version = assigned_task.version
        assigned.start(
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            now=NOW + timedelta(seconds=1),
        )
        assigned_worker.mark_running()
        session = ExecutionSession.start(
            session_id=uuid4(),
            workspace_id=workspace_id,
            execution_id=assigned.id,
            worker_id=assigned_worker.id,
            adapter_session_id="reconciler-session",
            now=NOW + timedelta(seconds=1),
        )
        assigned_task.mark_running()
        uow.executions.save(assigned, previous_execution_version)
        uow.workers.save(assigned_worker, previous_worker_version)
        uow.tasks.save(assigned_task, previous_task_version)
        uow.sessions.add(session)
    return assigned, worker, lease.id


def test_reconciler_expires_lease_and_queues_bounded_retry(
    database: Database,
    workspace_id: UUID,
) -> None:
    execution, worker, lease_id = seed_running_execution(
        database,
        workspace_id,
        max_attempts=3,
        worker_item_id=uuid4(),
        adapter_preferences=("opencode", "codex"),
        adapter_forbidden=("unsafe",),
    )
    reconcile_now = NOW + timedelta(seconds=31)
    reconciler = Reconciler(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        clock=lambda: reconcile_now,
        worker_heartbeat_timeout_seconds=30,
    )

    result = reconciler.run_once(limit=10)

    assert result.expired_leases == 1
    assert result.offline_workers == 1
    assert result.retries_queued == 1
    with database.session() as session:
        executions = session.scalars(
            select(ExecutionModel)
            .where(ExecutionModel.lineage_id == execution.lineage_id)
            .order_by(ExecutionModel.attempt.asc())
        ).all()
        lease = session.get(AssignmentLeaseModel, lease_id)
        worker_row = session.get(WorkerModel, worker.id)
        session_row = session.scalar(
            select(ExecutionSessionModel).where(
                ExecutionSessionModel.execution_id == execution.id
            )
        )
        task_row = session.get(TaskModel, execution.task_id)
    assert len(executions) == 2
    assert executions[0].status == "lost"
    assert executions[0].failure_code == "lease_expired"
    assert executions[0].active_lease_id is None
    assert executions[1].status == "queued"
    assert executions[1].attempt == 2
    assert executions[1].worker_item_id == executions[0].worker_item_id
    assert executions[1].adapter_preferences == ["opencode", "codex"]
    assert executions[1].adapter_forbidden == ["unsafe"]
    assert executions[1].not_before == reconcile_now + timedelta(seconds=2)
    assert lease is not None
    assert lease.status == "expired"
    assert worker_row is not None
    assert worker_row.status == "offline"
    assert worker_row.current_load == 0
    assert session_row is not None
    assert session_row.status == "lost"
    assert task_row is not None
    assert task_row.status == "ready"

    with database.session() as session:
        command_count = len(
            session.scalars(
                select(CommandModel).where(
                    CommandModel.command_type == "ReconcileLeaseExpiry"
                )
            ).all()
        )
        event_count = len(session.scalars(select(EventModel)).all())

    repeated = reconciler.run_once(limit=10)

    assert repeated.expired_leases == 0
    assert repeated.offline_workers == 0
    assert repeated.retries_queued == 0
    with database.session() as session:
        assert len(
            session.scalars(
                select(CommandModel).where(
                    CommandModel.command_type == "ReconcileLeaseExpiry"
                )
            ).all()
        ) == command_count
        assert len(session.scalars(select(EventModel)).all()) == event_count


def test_reconciler_waits_when_max_attempts_are_exhausted(
    database: Database,
    workspace_id: UUID,
) -> None:
    execution, _worker, _lease_id = seed_running_execution(
        database,
        workspace_id,
        max_attempts=1,
    )
    reconcile_now = NOW + timedelta(seconds=31)
    reconciler = Reconciler(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        clock=lambda: reconcile_now,
        worker_heartbeat_timeout_seconds=30,
    )

    result = reconciler.run_once(limit=10)

    assert result.expired_leases == 1
    assert result.retries_queued == 0
    with database.session() as session:
        executions = session.scalars(
            select(ExecutionModel).where(
                ExecutionModel.lineage_id == execution.lineage_id
            )
        ).all()
        task = session.get(TaskModel, execution.task_id)
    assert len(executions) == 1
    assert executions[0].status == "lost"
    assert task is not None
    assert task.status == "waiting"
