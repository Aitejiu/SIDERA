from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import EventModel, WorkspaceModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@dataclass(frozen=True, slots=True)
class RoutingResources:
    task: Task
    execution: Execution
    worker: Worker


def _worker(
    workspace_id: UUID,
    *,
    adapter_type: str = "opencode",
    capabilities: frozenset[str] = frozenset({"python"}),
) -> Worker:
    return Worker.register(
        worker_id=uuid4(),
        workspace_id=workspace_id,
        adapter_type=adapter_type,
        display_name="Routing worker",
        capabilities=capabilities,
        capacity=1,
        cost_per_hour_microunits=100,
        startup_latency_ms=10,
        now=NOW,
    )


def _queued_resources(
    database: Database,
    workspace_id: UUID,
    *,
    adapter_forbidden: tuple[str, ...] = (),
    worker: Worker | None = None,
) -> RoutingResources:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="Routing command task",
        description="Verify queued execution routing commands",
        acceptance_criteria=("routing is explicit",),
        priority=50,
        requested_by="user:routing",
    )
    task.mark_ready()
    selected_worker = worker or _worker(workspace_id)
    execution_id = uuid4()
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=task.id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=2,
        purpose="route queued execution",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=None,
        not_before=NOW,
        adapter_forbidden=adapter_forbidden,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(task)
        uow.workers.add(selected_worker)
        uow.flush()
        uow.executions.add(execution)
    return RoutingResources(task, execution, selected_worker)


def _execution_request(
    workspace_id: UUID,
    execution_id: UUID,
    command_type: str,
    expected_version: int | None,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": "execution",
        "actor": {"type": "user", "id": "user:routing"},
        "target_id": str(execution_id),
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": payload or {},
    }


def _load_execution(
    database: Database, execution_id: UUID, workspace_id: UUID
) -> Execution:
    with SqlAlchemyUnitOfWork(database) as uow:
        execution = uow.executions.get(execution_id, workspace_id)
    assert execution is not None
    return execution


def test_pin_allows_unconfigured_offline_worker_and_records_safe_summary(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    worker = _worker(workspace_id)
    worker.mark_offline()
    resources = _queued_resources(database, workspace_id, worker=worker)
    request = _execution_request(
        workspace_id,
        resources.execution.id,
        "PinExecutionWorker",
        resources.execution.version,
        "pin-unconfigured-001",
        {"worker_id": str(worker.id)},
    )

    response = client.post("/api/v1/commands", json=request)

    assert response.status_code == 200, response.text
    pinned = _load_execution(database, resources.execution.id, workspace_id)
    assert pinned.pinned_worker_id == worker.id
    assert pinned.version == resources.execution.version + 1
    with database.session() as session:
        event = session.scalar(
            select(EventModel).where(EventModel.event_type == "ExecutionWorkerPinned")
        )
    assert event is not None
    assert event.aggregate_id == resources.execution.id
    assert event.aggregate_version == pinned.version
    assert event.payload == {
        "worker_id": str(worker.id),
        "adapter_type": worker.adapter_type,
        "runtime_configuration_present": False,
    }
    assert "config" not in event.payload
    assert "secret" not in str(event.payload)


def test_pin_does_not_require_worker_capacity_to_be_available(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    worker = _worker(workspace_id)
    worker.reserve()
    resources = _queued_resources(database, workspace_id, worker=worker)

    response = client.post(
        "/api/v1/commands",
        json=_execution_request(
            workspace_id,
            resources.execution.id,
            "PinExecutionWorker",
            resources.execution.version,
            "pin-at-capacity-001",
            {"worker_id": worker.id.__str__()},
        ),
    )

    assert response.status_code == 200, response.text
    assert _load_execution(database, resources.execution.id, workspace_id).pinned_worker_id == worker.id


def test_clear_pin_restores_automatic_routing_and_is_versioned(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    resources = _queued_resources(database, workspace_id)
    pin = client.post(
        "/api/v1/commands",
        json=_execution_request(
            workspace_id,
            resources.execution.id,
            "PinExecutionWorker",
            resources.execution.version,
            "pin-clear-001",
            {"worker_id": str(resources.worker.id)},
        ),
    )
    assert pin.status_code == 200
    pinned = _load_execution(database, resources.execution.id, workspace_id)

    clear = client.post(
        "/api/v1/commands",
        json=_execution_request(
            workspace_id,
            resources.execution.id,
            "ClearExecutionWorkerPin",
            pinned.version,
            "clear-pin-001",
        ),
    )

    assert clear.status_code == 200, clear.text
    cleared = _load_execution(database, resources.execution.id, workspace_id)
    assert cleared.pinned_worker_id is None
    assert cleared.version == pinned.version + 1
    with database.session() as session:
        event_types = session.scalars(
            select(EventModel.event_type).order_by(EventModel.created_at, EventModel.id)
        ).all()
    assert event_types == ["ExecutionWorkerPinned", "ExecutionWorkerPinCleared"]


@pytest.mark.parametrize(
    "status",
    (
        ExecutionStatus.LEASED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.CANCELED,
    ),
)
def test_pin_and_clear_are_rejected_after_execution_leaves_queued(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    status: ExecutionStatus,
) -> None:
    resources = _seed_status_execution(database, workspace_id, status)
    pin = client.post(
        "/api/v1/commands",
        json=_execution_request(
            workspace_id,
            resources.execution.id,
            "PinExecutionWorker",
            resources.execution.version,
            f"pin-status-{status.value}",
            {"worker_id": str(resources.worker.id)},
        ),
    )
    assert pin.status_code == 409
    assert pin.json()["error"]["code"] == "invalid_transition"


def test_routing_commands_reject_stale_execution_version_and_replay_idempotently(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    resources = _queued_resources(database, workspace_id)
    request = _execution_request(
        workspace_id,
        resources.execution.id,
        "PinExecutionWorker",
        resources.execution.version,
        "pin-replay-001",
        {"worker_id": str(resources.worker.id)},
    )
    first = client.post("/api/v1/commands", json=request)
    replay = client.post(
        "/api/v1/commands",
        json={**request, "command_id": str(uuid4()), "correlation_id": str(uuid4())},
    )
    stale_clear = client.post(
        "/api/v1/commands",
        json=_execution_request(
            workspace_id,
            resources.execution.id,
            "ClearExecutionWorkerPin",
            resources.execution.version,
            "clear-stale-001",
        ),
    )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert stale_clear.status_code == 409
    assert stale_clear.json()["error"]["code"] == "version_conflict"


def test_pin_rejects_cross_workspace_worker_insufficient_capability_and_forbidden_adapter(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    resources = _queued_resources(
        database,
        workspace_id,
        adapter_forbidden=("unsafe",),
    )
    weak_worker = _worker(workspace_id, capabilities=frozenset({"docs"}))
    forbidden_worker = _worker(workspace_id, adapter_type="unsafe")
    foreign_workspace_id = uuid4()
    foreign_worker = _worker(foreign_workspace_id)
    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=foreign_workspace_id,
                name="Foreign routing workspace",
                status="active",
                version=1,
            )
        )
        session.flush()
        session.commit()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.workers.add(weak_worker)
        uow.workers.add(forbidden_worker)
        uow.workers.add(foreign_worker)

    cases = (
        (weak_worker, "pin-weak-001", "validation_error"),
        (forbidden_worker, "pin-forbidden-001", "validation_error"),
        (foreign_worker, "pin-foreign-001", "not_found"),
    )
    for worker, key, error_code in cases:
        response = client.post(
            "/api/v1/commands",
            json=_execution_request(
                workspace_id,
                resources.execution.id,
                "PinExecutionWorker",
                resources.execution.version,
                key,
                {"worker_id": str(worker.id)},
            ),
        )
        assert response.status_code in {404, 422}
        assert response.json()["error"]["code"] == error_code


def _seed_status_execution(
    database: Database,
    workspace_id: UUID,
    status: ExecutionStatus,
) -> RoutingResources:
    resources = _queued_resources(database, workspace_id)
    lease: AssignmentLease | None = None
    execution = resources.execution
    if status is ExecutionStatus.CANCELED:
        execution.cancel(now=NOW)
    else:
        lease = AssignmentLease.issue(
            lease_id=uuid4(),
            workspace_id=workspace_id,
            execution_id=execution.id,
            worker_id=resources.worker.id,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            fencing_token=1,
            assignment_reason={"policy": "test"},
        )
        execution.assign(
            lease_id=lease.id,
            worker_id=resources.worker.id,
            fencing_token=lease.fencing_token,
        )
        if status in {
            ExecutionStatus.RUNNING,
            ExecutionStatus.SUCCEEDED,
        }:
            execution.start(lease_id=lease.id, fencing_token=1, now=NOW)
        if status is ExecutionStatus.SUCCEEDED:
            execution.succeed(lease_id=lease.id, fencing_token=1, now=NOW)
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.executions.save(execution, 1)
        if lease is not None:
            uow.leases.add(lease)
    return RoutingResources(resources.task, execution, resources.worker)


def test_pin_command_schema_requires_execution_target_and_worker_payload(
    client: TestClient,
    workspace_id: UUID,
) -> None:
    base = {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "PinExecutionWorker",
        "target_type": "worker",
        "actor": {"type": "user", "id": "user:routing"},
        "target_id": str(uuid4()),
        "expected_version": 1,
        "idempotency_key": "routing-schema-001",
        "correlation_id": str(uuid4()),
        "payload": {"worker_id": str(uuid4())},
    }
    invalid_target = client.post("/api/v1/commands", json=base)
    missing_worker = client.post(
        "/api/v1/commands",
        json={
            **base,
            "command_id": str(uuid4()),
            "target_type": "execution",
            "payload": {},
        },
    )
    assert invalid_target.status_code == 422
    assert missing_worker.status_code == 422
