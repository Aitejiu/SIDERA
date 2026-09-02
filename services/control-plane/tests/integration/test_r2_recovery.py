from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.integration.test_scheduler import register_runtime_modules_and_configure_worker

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.reconciler import Reconciler
from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import ExecutionModel, TaskModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(database_url: str):
    settings = Settings(database_url=database_url)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def seed_running_execution(
    database: Database,
    workspace_id: UUID,
) -> tuple[Execution, Worker, UUID]:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="Recovery task",
        description="Reconciler concurrency",
        acceptance_criteria=("recovery is restartable",),
        priority=50,
        requested_by="system:test",
    )
    task.mark_ready()
    worker = Worker.register(
        worker_id=uuid4(),
        workspace_id=workspace_id,
        adapter_type="fake",
        display_name="Recovery worker",
        capabilities=frozenset({"code", "python"}),
        capacity=1,
        cost_per_hour_microunits=1,
        startup_latency_ms=1,
        now=NOW,
    )
    execution_id = uuid4()
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=task.id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=3,
        purpose="reconciler concurrency",
        capability_requirements=frozenset({"code", "python"}),
        pinned_worker_id=worker.id,
        not_before=NOW,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(task)
        uow.workers.add(worker)
        register_runtime_modules_and_configure_worker(uow, worker)
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
            adapter_session_id="recovery-session",
            now=NOW + timedelta(seconds=1),
        )
        assigned_task.mark_running()
        uow.executions.save(assigned, previous_execution_version)
        uow.workers.save(assigned_worker, previous_worker_version)
        uow.tasks.save(assigned_task, previous_task_version)
        uow.sessions.add(session)
    return assigned, worker, lease.id


def test_two_reconcilers_expire_once_and_restart_scheduler_assigns_retry(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    execution, worker, lease_id = seed_running_execution(database, workspace_id)
    reconcile_now = NOW + timedelta(seconds=31)

    def run_reconciler() -> Any:
        return Reconciler(
            uow_factory=lambda: SqlAlchemyUnitOfWork(database),
            clock=lambda: reconcile_now,
            worker_heartbeat_timeout_seconds=30,
        ).run_once(limit=10)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _item: run_reconciler(), range(2)))

    assert sum(result.expired_leases for result in results) == 1
    assert sum(result.retries_queued for result in results) == 1
    assert sum(result.offline_workers for result in results) == 1

    with SqlAlchemyUnitOfWork(database) as uow:
        lost = uow.executions.get(execution.id, workspace_id)
    assert lost is not None
    assert lost.version == 4

    late_report = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "ReportExecutionStarted",
            "target_type": "execution",
            "actor": {"type": "worker", "id": str(worker.id)},
            "target_id": str(execution.id),
            "expected_version": lost.version,
            "idempotency_key": "late-recovery-report-001",
            "correlation_id": str(uuid4()),
            "payload": {
                "lease_id": str(lease_id),
                "worker_id": str(worker.id),
                "fencing_token": 1,
                "adapter_session_id": "late-recovery-session",
            },
        },
    )
    assert late_report.status_code == 409, late_report.text
    assert late_report.json()["error"]["code"] == "stale_fencing_token"

    restarted_reconciler = Reconciler(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        clock=lambda: reconcile_now + timedelta(seconds=1),
        worker_heartbeat_timeout_seconds=30,
    )
    repeated = restarted_reconciler.run_once(limit=10)
    assert repeated.expired_leases == 0
    assert repeated.retries_queued == 0
    assert repeated.offline_workers == 0

    with SqlAlchemyUnitOfWork(database) as uow:
        stored_worker = uow.workers.get(worker.id, workspace_id)
        retry_row = uow.session.scalar(
            select(ExecutionModel).where(
                ExecutionModel.lineage_id == execution.lineage_id,
                ExecutionModel.attempt == 2,
            )
        )
        task = uow.tasks.get(execution.task_id, workspace_id)
        assert stored_worker is not None
        assert retry_row is not None
        assert task is not None
        previous_worker_version = stored_worker.version
        stored_worker.heartbeat(
            healthy=True,
            now=reconcile_now + timedelta(seconds=3),
        )
        uow.workers.save(stored_worker, previous_worker_version)

    scheduler = Scheduler(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        policy=LowestCostWorkerPolicy(),
        clock=lambda: reconcile_now + timedelta(seconds=3),
        assignment_lease_seconds=30,
        worker_heartbeat_timeout_seconds=30,
    )
    scheduled = scheduler.run_once(limit=10)

    assert scheduled.assigned == 1
    with database.session() as session:
        retry = session.get(ExecutionModel, retry_row.id)
        task_row = session.get(TaskModel, execution.task_id)
    assert retry is not None
    assert retry.status == "leased"
    assert task_row is not None
    assert task_row.status == "ready"
