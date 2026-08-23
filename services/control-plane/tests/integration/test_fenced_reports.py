from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from test_scheduler import register_runtime_modules_and_configure_worker

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    CommandModel,
    EventModel,
    ExecutionSessionModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


class FakeWorkerDriver:
    def __init__(
        self,
        client: TestClient,
        workspace_id: UUID,
        worker_id: UUID,
        lease_id: UUID,
        fencing_token: int,
    ) -> None:
        self._client = client
        self._workspace_id = workspace_id
        self._worker_id = worker_id
        self._lease_id = lease_id
        self._fencing_token = fencing_token

    def start(
        self, execution_id: UUID, *, expected_version: int, adapter_session_id: str
    ) -> Any:
        return self._post(
            command_type="ReportExecutionStarted",
            target_type="execution",
            target_id=execution_id,
            expected_version=expected_version,
            idempotency_key="fake-start-001",
            payload={
                "lease_id": str(self._lease_id),
                "worker_id": str(self._worker_id),
                "fencing_token": self._fencing_token,
                "adapter_session_id": adapter_session_id,
            },
        )

    def renew(self, *, expected_version: int) -> Any:
        return self._post(
            command_type="RenewLease",
            target_type="lease",
            target_id=self._lease_id,
            expected_version=expected_version,
            idempotency_key="fake-renew-001",
            payload={
                "worker_id": str(self._worker_id),
                "fencing_token": self._fencing_token,
            },
        )

    def succeed(self, execution_id: UUID, *, expected_version: int) -> Any:
        return self._post(
            command_type="ReportExecutionSucceeded",
            target_type="execution",
            target_id=execution_id,
            expected_version=expected_version,
            idempotency_key="fake-succeed-001",
            payload={
                "lease_id": str(self._lease_id),
                "worker_id": str(self._worker_id),
                "fencing_token": self._fencing_token,
            },
        )

    def fail(
        self,
        execution_id: UUID,
        *,
        expected_version: int,
        failure_code: str,
    ) -> Any:
        return self._post(
            command_type="ReportExecutionFailed",
            target_type="execution",
            target_id=execution_id,
            expected_version=expected_version,
            idempotency_key="fake-fail-001",
            payload={
                "lease_id": str(self._lease_id),
                "worker_id": str(self._worker_id),
                "fencing_token": self._fencing_token,
                "failure_code": failure_code,
            },
        )

    def cancel(self, execution_id: UUID, *, expected_version: int) -> Any:
        return self._post(
            command_type="CancelExecution",
            target_type="execution",
            target_id=execution_id,
            expected_version=expected_version,
            idempotency_key="fake-cancel-001",
            payload={},
        )

    def _post(
        self,
        *,
        command_type: str,
        target_type: str,
        target_id: UUID,
        expected_version: int,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._client.post(
            "/api/v1/commands",
            json={
                "command_id": str(uuid4()),
                "workspace_id": str(self._workspace_id),
                "command_type": command_type,
                "target_type": target_type,
                "actor": {"type": "worker", "id": str(self._worker_id)},
                "target_id": str(target_id),
                "expected_version": expected_version,
                "idempotency_key": idempotency_key,
                "correlation_id": str(uuid4()),
                "payload": payload,
            },
        )


def seed_scheduled_execution(
    database: Database, workspace_id: UUID
) -> tuple[Execution, Worker, UUID]:
    now = datetime.now(UTC)
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="Fenced report task",
        description="Worker lifecycle",
        acceptance_criteria=("worker reports are accepted",),
        priority=50,
        requested_by="system:test",
    )
    task.mark_ready()
    worker = Worker.register(
        worker_id=uuid4(),
        workspace_id=workspace_id,
        adapter_type="fake",
        display_name="Fake report worker",
        capabilities=frozenset({"code", "python"}),
        capacity=1,
        cost_per_hour_microunits=1,
        startup_latency_ms=1,
        now=now,
    )
    execution_id = uuid4()
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=task.id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=3,
        purpose="worker lifecycle",
        capability_requirements=frozenset({"code", "python"}),
        pinned_worker_id=worker.id,
        not_before=now,
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
        clock=lambda: now,
        assignment_lease_seconds=30,
        worker_heartbeat_timeout_seconds=45,
    )
    result = scheduler.run_once(limit=1)
    assert result.assigned == 1

    with SqlAlchemyUnitOfWork(database) as uow:
        stored_execution = uow.executions.get(execution.id, workspace_id)
    assert stored_execution is not None
    assert stored_execution.active_lease_id is not None
    return stored_execution, worker, stored_execution.active_lease_id


def test_fake_worker_can_start_renew_and_finish_execution(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    execution, worker, lease_id = seed_scheduled_execution(database, workspace_id)
    driver = FakeWorkerDriver(client, workspace_id, worker.id, lease_id, 1)

    started = driver.start(
        execution.id,
        expected_version=2,
        adapter_session_id="fake-session-1",
    )
    assert started.status_code == 200, started.text
    assert started.json()["aggregate_version"] == 3

    replayed_start = driver.start(
        execution.id,
        expected_version=2,
        adapter_session_id="fake-session-1",
    )
    assert replayed_start.status_code == 200, replayed_start.text
    assert replayed_start.json() == started.json()

    with database.session() as session:
        active_session = session.scalar(
            select(ExecutionSessionModel).where(
                ExecutionSessionModel.execution_id == execution.id,
                ExecutionSessionModel.status == "active",
            )
        )
    assert active_session is not None
    assert active_session.adapter_session_id == "fake-session-1"
    assert active_session.input_mode == "unsupported"
    assert active_session.last_sequence == 0

    renewed = driver.renew(expected_version=1)
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["aggregate_version"] == 2

    finished = driver.succeed(execution.id, expected_version=3)
    assert finished.status_code == 200, finished.text
    assert finished.json()["aggregate_version"] == 4

    execution_detail = client.get(
        f"/api/v1/executions/{execution.id}",
        params={"workspace_id": str(workspace_id)},
    )
    assert execution_detail.status_code == 200
    assert execution_detail.json()["status"] == "succeeded"
    assert execution_detail.json()["active_lease_id"] is None

    worker_detail = client.get(
        f"/api/v1/workers/{worker.id}",
        params={"workspace_id": str(workspace_id)},
    )
    assert worker_detail.status_code == 200
    assert worker_detail.json()["status"] == "idle"
    assert worker_detail.json()["current_load"] == 0

    with database.session() as session:
        closed_session = session.scalar(
            select(ExecutionSessionModel).where(
                ExecutionSessionModel.execution_id == execution.id,
            )
        )
        lease = session.get(AssignmentLeaseModel, lease_id)
    assert closed_session is not None
    assert closed_session.status == "closed"
    assert lease is not None
    assert lease.status == "released"


def test_fake_worker_failure_closes_session_and_waits_for_decision(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    execution, worker, lease_id = seed_scheduled_execution(database, workspace_id)
    driver = FakeWorkerDriver(client, workspace_id, worker.id, lease_id, 1)

    started = driver.start(
        execution.id,
        expected_version=2,
        adapter_session_id="fake-session-failure",
    )
    assert started.status_code == 200, started.text

    failed = driver.fail(
        execution.id,
        expected_version=3,
        failure_code="adapter_timeout",
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["aggregate_version"] == 4

    detail = client.get(
        f"/api/v1/executions/{execution.id}",
        params={"workspace_id": str(workspace_id)},
    )
    assert detail.status_code == 200
    assert detail.json()["status"] == "failed"
    assert detail.json()["failure_code"] == "adapter_timeout"

    with SqlAlchemyUnitOfWork(database) as uow:
        task = uow.tasks.get(execution.task_id, workspace_id)
    assert task is not None
    assert task.status.value == "waiting"


@pytest.mark.parametrize("failure_mode", ["wrong_worker", "stale_token"])
def test_fenced_report_rejects_wrong_worker_or_token_without_mutation(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    failure_mode: str,
) -> None:
    execution, worker, lease_id = seed_scheduled_execution(database, workspace_id)
    report_worker_id = uuid4() if failure_mode == "wrong_worker" else worker.id
    report_token = 1 if failure_mode == "wrong_worker" else 2
    driver = FakeWorkerDriver(
        client,
        workspace_id,
        report_worker_id,
        lease_id,
        report_token,
    )

    rejected = driver.start(
        execution.id,
        expected_version=2,
        adapter_session_id=f"rejected-{failure_mode}",
    )

    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "stale_fencing_token"
    with SqlAlchemyUnitOfWork(database) as uow:
        stored_execution = uow.executions.get(execution.id, workspace_id)
    assert stored_execution is not None
    assert stored_execution.status.value == "leased"
    assert stored_execution.version == 2
    with database.session() as session:
        command = session.scalar(
            select(CommandModel).where(
                CommandModel.command_type == "ReportExecutionStarted"
            )
        )
        active_session = session.scalar(
            select(ExecutionSessionModel).where(
                ExecutionSessionModel.execution_id == execution.id,
                ExecutionSessionModel.status == "active",
            )
        )
        events = session.scalars(select(EventModel)).all()
    assert command is not None
    assert command.status == "rejected"
    assert command.rejection_reason == "stale_fencing_token"
    assert active_session is None
    assert {event.event_type for event in events} == {
        "ExecutionRuntimeBound",
        "ExecutionLeased",
        "LeaseGranted",
        "WorkerCapacityReserved",
    }


def test_expired_lease_rejects_worker_report(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    execution, worker, lease_id = seed_scheduled_execution(database, workspace_id)
    with database.session() as session:
        session.execute(
            update(AssignmentLeaseModel)
            .where(AssignmentLeaseModel.id == lease_id)
            .values(expires_at=datetime.now(UTC))
        )
        session.commit()

    driver = FakeWorkerDriver(client, workspace_id, worker.id, lease_id, 1)
    rejected = driver.start(
        execution.id,
        expected_version=2,
        adapter_session_id="expired-session",
    )

    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "lease_expired"


def test_report_after_cancellation_is_rejected_as_stale(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    execution, worker, lease_id = seed_scheduled_execution(database, workspace_id)
    driver = FakeWorkerDriver(client, workspace_id, worker.id, lease_id, 1)

    canceled = driver.cancel(execution.id, expected_version=2)
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["aggregate_version"] == 3

    late_report = driver.start(
        execution.id,
        expected_version=3,
        adapter_session_id="late-session",
    )
    assert late_report.status_code == 409, late_report.text
    assert late_report.json()["error"]["code"] == "stale_fencing_token"
