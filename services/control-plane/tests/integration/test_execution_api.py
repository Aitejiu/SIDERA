from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.ports import WorkerItemPlacement
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.plans import (
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    EventModel,
    TaskModel,
    WorkspaceModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def add_workspace(database: Database, workspace_id: UUID, name: str) -> None:
    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=workspace_id,
                name=name,
                status="active",
                version=1,
            )
        )
        session.commit()


def create_task(client: TestClient, workspace_id: UUID, title: str) -> UUID:
    response = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "CreateTask",
            "target_type": "task",
            "actor": {"type": "user", "id": "user:test"},
            "target_id": None,
            "expected_version": None,
            "idempotency_key": f"create-task-{title}",
            "correlation_id": str(uuid4()),
            "payload": {
                "title": title,
                "description": f"Description for {title}",
                "acceptance_criteria": [f"{title} is complete"],
                "priority": 50,
            },
        },
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["aggregate_id"])


def register_worker(client: TestClient, workspace_id: UUID) -> UUID:
    response = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "RegisterWorker",
            "target_type": "worker",
            "actor": {"type": "admin", "id": "admin:test"},
            "target_id": None,
            "expected_version": None,
            "idempotency_key": f"register-worker-{uuid4()}",
            "correlation_id": str(uuid4()),
            "payload": {
                "adapter_type": "fake",
                "display_name": "Fake Worker",
                "capabilities": ["code", "python"],
                "capacity": 1,
                "cost_per_hour_microunits": 500_000,
                "startup_latency_ms": 25,
            },
        },
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["aggregate_id"])


def create_execution_request(
    workspace_id: UUID,
    task_id: UUID,
    *,
    idempotency_key: str,
    pinned_worker_id: UUID | None = None,
    worker_item_id: UUID | None = None,
    max_attempts: int = 3,
    adapter_preferences: tuple[str, ...] = (),
    adapter_forbidden: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "CreateExecution",
        "target_type": "execution",
        "actor": {"type": "admin", "id": "admin:test"},
        "target_id": None,
        "expected_version": None,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": {
            "task_id": str(task_id),
            "worker_item_id": (
                str(worker_item_id) if worker_item_id is not None else None
            ),
            "purpose": "implement",
            "capability_requirements": ["code", "python"],
            "pinned_worker_id": str(pinned_worker_id)
            if pinned_worker_id is not None
            else None,
            "max_attempts": max_attempts,
            "not_before": NOW.isoformat(),
            "adapter_preferences": list(adapter_preferences),
            "adapter_forbidden": list(adapter_forbidden),
        },
    }


def seed_v2_worker_item_context(
    database: Database, workspace_id: UUID
) -> tuple[UUID, UUID, UUID]:
    root_task_id = uuid4()
    subtask_task_id = uuid4()
    snapshot_id = uuid4()
    worker_item_id = uuid4()
    with database.session() as session:
        session.add_all(
            [
                TaskModel(
                    id=root_task_id,
                    workspace_id=workspace_id,
                    title="Task First root",
                    description="Task First root",
                    acceptance_criteria=["root completes"],
                    status="pending",
                    priority=50,
                    requested_by="task-first-test",
                    version=1,
                ),
                TaskModel(
                    id=subtask_task_id,
                    workspace_id=workspace_id,
                    title="Task First subtask",
                    description="Task First subtask",
                    acceptance_criteria=["subtask completes"],
                    status="ready",
                    priority=50,
                    requested_by="task-first-test",
                    version=1,
                ),
            ]
        )
        session.commit()

    snapshot = TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=2,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        generation=1,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="execution-api:v2",
        title="Task First execution plan",
        goal_summary="Bind execution to a stable WorkerItem.",
        acceptance_refs=("worker item is required",),
        published_by="task-first-test",
        published_at=NOW,
        subtasks=(
            SubtaskPlanItem(
                key="backend",
                task_id=subtask_task_id,
                kind=WorkItemKind.CHILD,
                title="Backend",
                goal_summary="Implement backend.",
                acceptance_refs=("backend works",),
                milestone_key=None,
                expected_outputs=("api",),
                source_refs=("test",),
            ),
        ),
        worker_item_plans=(
            WorkerItemPlanItem(
                key="backend:implementer",
                worker_item_id=worker_item_id,
                subtask_key="backend",
                title="Backend implementer",
                role="implementer",
                responsibility="Implement backend.",
                capability_requirements=("python",),
                worker_policy_ref="policy:test",
                configuration_ref="config:test",
                concurrency_limit=1,
                source_refs=("test",),
            ),
        ),
    )
    item = WorkerItem.create(
        item_id=worker_item_id,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        title="Backend implementer",
        responsibility="Implement backend.",
        now=NOW,
    )
    placement = WorkerItemPlacement(
        plan_snapshot_id=snapshot_id,
        plan_generation=1,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        worker_item_id=worker_item_id,
        worker_item_key="backend:implementer",
        subtask_key="backend",
        subtask_task_id=subtask_task_id,
        role="implementer",
        capability_requirements=("python",),
        worker_policy_ref="policy:test",
        configuration_ref="config:test",
        created_at=NOW,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.plans.add(snapshot)
        uow.worker_items.add(item)
        uow.worker_items.add_placement(placement)
    return subtask_task_id, worker_item_id, root_task_id


def execution_mutation_request(
    workspace_id: UUID,
    execution_id: UUID,
    command_type: str,
    expected_version: int,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": "execution",
        "actor": {"type": "admin", "id": "admin:test"},
        "target_id": str(execution_id),
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": payload or {},
    }


def seed_active_execution(
    database: Database,
    workspace_id: UUID,
    *,
    max_attempts: int = 3,
    worker_item_id: UUID | None = None,
    adapter_preferences: tuple[str, ...] = (),
    adapter_forbidden: tuple[str, ...] = (),
) -> tuple[Task, Worker, Execution, AssignmentLease, ExecutionSession]:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="Active execution task",
        description="Cancel resource release",
        acceptance_criteria=("resources are released",),
        priority=50,
        requested_by="admin:test",
    )
    task.mark_ready()
    worker = Worker.register(
        worker_id=uuid4(),
        workspace_id=workspace_id,
        adapter_type="fake",
        display_name="Active worker",
        capabilities=frozenset({"code", "python"}),
        capacity=1,
        cost_per_hour_microunits=500_000,
        startup_latency_ms=25,
        now=NOW,
    )
    worker_item = (
        WorkerItem.create(
            item_id=worker_item_id,
            workspace_id=workspace_id,
            root_task_id=task.id,
            title="Active WorkerItem",
            responsibility="Keep the active execution bound.",
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
        purpose="active execution",
        capability_requirements=frozenset({"code", "python"}),
        pinned_worker_id=worker.id,
        not_before=NOW,
        adapter_preferences=adapter_preferences,
        adapter_forbidden=adapter_forbidden,
    )
    lease = AssignmentLease.issue(
        lease_id=uuid4(),
        workspace_id=workspace_id,
        execution_id=execution.id,
        worker_id=worker.id,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        fencing_token=1,
        assignment_reason={"policy": "test"},
    )
    execution.assign(
        lease_id=lease.id,
        worker_id=worker.id,
        fencing_token=lease.fencing_token,
    )
    execution_session = ExecutionSession.start(
        session_id=uuid4(),
        workspace_id=workspace_id,
        execution_id=execution.id,
        worker_id=worker.id,
        adapter_session_id="active-session",
        now=NOW,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(task)
        uow.workers.add(worker)
        if worker_item is not None:
            uow.worker_items.add(worker_item)
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.executions.add(
            Execution.create(
                execution_id=execution.id,
                workspace_id=execution.workspace_id,
                task_id=execution.task_id,
                worker_item_id=execution.worker_item_id,
                lineage_id=execution.lineage_id,
                attempt=execution.attempt,
                max_attempts=execution.max_attempts,
                purpose=execution.purpose,
                capability_requirements=execution.capability_requirements,
                pinned_worker_id=execution.pinned_worker_id,
                not_before=execution.not_before,
            )
        )
    worker.reserve()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.leases.add(lease)
        uow.executions.save(execution, 1)
        uow.workers.save(worker, 1)
        uow.sessions.add(execution_session)
    return task, worker, execution, lease, execution_session


def seed_terminal_execution(
    database: Database,
    workspace_id: UUID,
    *,
    status: str,
    max_attempts: int,
    worker_item_id: UUID | None = None,
    adapter_preferences: tuple[str, ...] = (),
    adapter_forbidden: tuple[str, ...] = (),
) -> tuple[Task, Execution]:
    task, worker, execution, lease, _ = seed_active_execution(
        database,
        workspace_id,
        max_attempts=max_attempts,
        worker_item_id=worker_item_id,
        adapter_preferences=adapter_preferences,
        adapter_forbidden=adapter_forbidden,
    )
    execution.start(
        lease_id=lease.id,
        fencing_token=lease.fencing_token,
        now=NOW + timedelta(seconds=1),
    )
    if status == "failed":
        execution.fail(
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            failure_code="worker_failed",
            now=NOW + timedelta(seconds=2),
        )
    else:
        execution.mark_lost(
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            failure_code="worker_lost",
            now=NOW + timedelta(seconds=2),
        )
    worker.release()
    lease.revoke(now=NOW + timedelta(seconds=2))
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.get(task.id, workspace_id)
        uow.workers.save(worker, 2)
        uow.executions.save(execution, 2)
        uow.leases.save(lease, 1)
    return task, execution


def test_create_execution_readiness_parallel_lineages_and_replay(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    row_counts: Callable[[], dict[str, int]],
) -> None:
    task_id = create_task(client, workspace_id, "Parallel execution task")
    first_request = create_execution_request(
        workspace_id,
        task_id,
        idempotency_key="create-execution-001",
        adapter_preferences=("opencode", "codex"),
        adapter_forbidden=("unsafe",),
    )
    first = client.post("/api/v1/commands", json=first_request)

    assert first.status_code == 201, first.text
    first_id = UUID(first.json()["aggregate_id"])
    first_detail = client.get(
        f"/api/v1/executions/{first_id}",
        params={"workspace_id": str(workspace_id)},
    )
    assert first_detail.status_code == 200, first_detail.text
    assert first_detail.json()["status"] == "queued"
    assert first_detail.json()["attempt"] == 1
    assert first_detail.json()["lineage_id"] == str(first_id)
    assert first_detail.json()["capability_requirements"] == ["code", "python"]
    with SqlAlchemyUnitOfWork(database) as uow:
        stored_first = uow.executions.get(first_id, workspace_id)
        assert stored_first is not None
        assert stored_first.adapter_preferences == ("opencode", "codex")
        assert stored_first.adapter_forbidden == frozenset({"unsafe"})

    task_detail = client.get(
        f"/api/v1/tasks/{task_id}",
        params={"workspace_id": str(workspace_id)},
    )
    assert task_detail.status_code == 200
    assert task_detail.json()["status"] == "ready"

    second = client.post(
        "/api/v1/commands",
        json=create_execution_request(
            workspace_id,
            task_id,
            idempotency_key="create-execution-002",
        ),
    )
    assert second.status_code == 201, second.text
    second_id = UUID(second.json()["aggregate_id"])
    assert second_id != first_id

    replay = client.post(
        "/api/v1/commands",
        json={
            **first_request,
            "correlation_id": str(uuid4()),
        },
    )
    assert replay.status_code == 201
    assert replay.json() == first.json()

    listed = client.get(
        "/api/v1/executions",
        params={"workspace_id": str(workspace_id), "task_id": str(task_id)},
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [
        str(first_id),
        str(second_id),
    ]
    assert {item["lineage_id"] for item in listed.json()} == {
        str(first_id),
        str(second_id),
    }
    assert row_counts() == {
        "tasks": 1,
        "commands": 3,
        "events": 4,
        "outbox_messages": 4,
    }


def test_task_first_v2_execution_requires_current_active_worker_item(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    subtask_id, worker_item_id, _root_task_id = seed_v2_worker_item_context(
        database, workspace_id
    )

    missing = client.post(
        "/api/v1/commands",
        json=create_execution_request(
            workspace_id,
            subtask_id,
            idempotency_key="task-first-v2-missing-worker-item",
        ),
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "validation_error"
    assert "worker_item_id" in missing.json()["error"]["message"]

    created = client.post(
        "/api/v1/commands",
        json=create_execution_request(
            workspace_id,
            subtask_id,
            idempotency_key="task-first-v2-bound",
            worker_item_id=worker_item_id,
        ),
    )
    assert created.status_code == 201, created.text
    execution_id = UUID(created.json()["aggregate_id"])
    detail = client.get(
        f"/api/v1/executions/{execution_id}",
        params={"workspace_id": str(workspace_id)},
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["worker_item_id"] == str(worker_item_id)

    with SqlAlchemyUnitOfWork(database) as uow:
        item = uow.worker_items.get(worker_item_id, workspace_id, for_update=True)
        assert item is not None
        previous_version = item.version
        item.retire(now=NOW + timedelta(minutes=1))
        uow.worker_items.save(item, previous_version)

    retired = client.post(
        "/api/v1/commands",
        json=create_execution_request(
            workspace_id,
            subtask_id,
            idempotency_key="task-first-v2-retired-worker-item",
            worker_item_id=worker_item_id,
        ),
    )
    assert retired.status_code == 422
    assert retired.json()["error"]["code"] == "validation_error"
    assert "active" in retired.json()["error"]["message"]


def test_retry_inherits_worker_item_binding(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    worker_item_id = uuid4()
    _task, source = seed_terminal_execution(
        database,
        workspace_id,
        status="failed",
        max_attempts=2,
        worker_item_id=worker_item_id,
    )

    retried = client.post(
        "/api/v1/commands",
        json=execution_mutation_request(
            workspace_id,
            source.id,
            "RetryExecution",
            source.version,
            "retry-worker-item-001",
            {"not_before": (NOW + timedelta(minutes=1)).isoformat()},
        ),
    )
    assert retried.status_code == 201, retried.text
    retry_detail = client.get(
        f"/api/v1/executions/{retried.json()['aggregate_id']}",
        params={"workspace_id": str(workspace_id)},
    )
    assert retry_detail.status_code == 200
    assert retry_detail.json()["worker_item_id"] == str(worker_item_id)


def test_execution_rejects_cross_workspace_references(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    workspace_b_id = uuid4()
    add_workspace(database, workspace_b_id, "Workspace B")
    task_a_id = create_task(client, workspace_id, "Workspace A task")
    task_b_id = create_task(client, workspace_b_id, "Workspace B task")
    worker_b_id = register_worker(client, workspace_b_id)

    wrong_task = client.post(
        "/api/v1/commands",
        json=create_execution_request(
            workspace_id,
            task_b_id,
            idempotency_key="wrong-task-workspace-001",
        ),
    )
    wrong_worker = client.post(
        "/api/v1/commands",
        json=create_execution_request(
            workspace_id,
            task_a_id,
            idempotency_key="wrong-worker-workspace-001",
            pinned_worker_id=worker_b_id,
        ),
    )

    assert wrong_task.status_code == 404
    assert wrong_task.json()["error"]["code"] == "not_found"
    assert wrong_worker.status_code == 404
    assert wrong_worker.json()["error"]["code"] == "not_found"


def test_cancel_execution_releases_lease_worker_session_and_task(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    task, worker, execution, lease, _ = seed_active_execution(database, workspace_id)
    request = execution_mutation_request(
        workspace_id,
        execution.id,
        "CancelExecution",
        execution.version,
        "cancel-execution-001",
    )

    canceled = client.post("/api/v1/commands", json=request)

    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["aggregate_version"] == execution.version + 1
    detail = client.get(
        f"/api/v1/executions/{execution.id}",
        params={"workspace_id": str(workspace_id)},
    )
    worker_detail = client.get(
        f"/api/v1/workers/{worker.id}",
        params={"workspace_id": str(workspace_id)},
    )
    task_detail = client.get(
        f"/api/v1/tasks/{task.id}",
        params={"workspace_id": str(workspace_id)},
    )
    leases = client.get(
        f"/api/v1/executions/{execution.id}/leases",
        params={"workspace_id": str(workspace_id)},
    )
    assert detail.json()["status"] == "canceled"
    assert detail.json()["active_lease_id"] is None
    assert worker_detail.json()["status"] == "idle"
    assert worker_detail.json()["current_load"] == 0
    assert task_detail.json()["status"] == "waiting"
    assert leases.status_code == 200
    assert len(leases.json()) == 1
    assert leases.json()[0]["id"] == str(lease.id)
    assert leases.json()[0]["status"] == "released"

    with database.session() as session:
        event_types = session.scalars(
            select(EventModel.event_type).order_by(EventModel.created_at, EventModel.id)
        ).all()
    assert set(event_types) == {
        "ExecutionCanceled",
        "LeaseReleased",
        "WorkerCapacityReleased",
        "ExecutionSessionClosed",
        "TaskWaitingForDecision",
    }


@pytest.mark.parametrize("terminal_status", ["failed", "lost"])
def test_retry_is_append_only_and_bounded(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    terminal_status: str,
) -> None:
    task, source = seed_terminal_execution(
        database,
        workspace_id,
        status=terminal_status,
        max_attempts=2,
        adapter_preferences=("opencode", "codex"),
        adapter_forbidden=("unsafe",),
    )
    retry_request = execution_mutation_request(
        workspace_id,
        source.id,
        "RetryExecution",
        source.version,
        f"retry-{terminal_status}-001",
        {"not_before": (NOW + timedelta(minutes=1)).isoformat()},
    )

    retried = client.post("/api/v1/commands", json=retry_request)

    assert retried.status_code == 201, retried.text
    retry_id = UUID(retried.json()["aggregate_id"])
    source_detail = client.get(
        f"/api/v1/executions/{source.id}",
        params={"workspace_id": str(workspace_id)},
    )
    retry_detail = client.get(
        f"/api/v1/executions/{retry_id}",
        params={"workspace_id": str(workspace_id)},
    )
    assert source_detail.json()["status"] == terminal_status
    assert source_detail.json()["attempt"] == 1
    assert retry_detail.json()["status"] == "queued"
    assert retry_detail.json()["attempt"] == 2
    assert retry_detail.json()["lineage_id"] == source_detail.json()["lineage_id"]
    assert datetime.fromisoformat(
        retry_detail.json()["not_before"]
    ) == NOW + timedelta(minutes=1)
    with SqlAlchemyUnitOfWork(database) as uow:
        stored_retry = uow.executions.get(retry_id, workspace_id)
        assert stored_retry is not None
        assert stored_retry.adapter_preferences == ("opencode", "codex")
        assert stored_retry.adapter_forbidden == frozenset({"unsafe"})

    replay = client.post(
        "/api/v1/commands",
        json={
            **retry_request,
            "correlation_id": str(uuid4()),
        },
    )
    assert replay.status_code == 201
    assert replay.json() == retried.json()

    duplicate_attempt = client.post(
        "/api/v1/commands",
        json=execution_mutation_request(
            workspace_id,
            source.id,
            "RetryExecution",
            source.version,
            f"retry-{terminal_status}-duplicate-001",
            {"not_before": (NOW + timedelta(minutes=2)).isoformat()},
        ),
    )
    assert duplicate_attempt.status_code == 409

    maxed_task, maxed_source = seed_terminal_execution(
        database,
        workspace_id,
        status=terminal_status,
        max_attempts=1,
    )
    maxed = client.post(
        "/api/v1/commands",
        json=execution_mutation_request(
            workspace_id,
            maxed_source.id,
            "RetryExecution",
            maxed_source.version,
            f"retry-{terminal_status}-maxed-001",
            {"not_before": NOW.isoformat()},
        ),
    )
    assert maxed.status_code == 409
    assert maxed.json()["error"]["code"] == "invalid_transition"
    assert maxed_task.id != task.id


def test_execution_query_workspace_isolation_and_invalid_shapes(
    client: TestClient,
    workspace_id: UUID,
) -> None:
    task_id = create_task(client, workspace_id, "Query execution task")
    created = client.post(
        "/api/v1/commands",
        json=create_execution_request(
            workspace_id,
            task_id,
            idempotency_key="query-execution-001",
        ),
    )
    execution_id = UUID(created.json()["aggregate_id"])
    outside = client.get(
        f"/api/v1/executions/{execution_id}",
        params={"workspace_id": str(uuid4())},
    )
    invalid = create_execution_request(
        workspace_id,
        task_id,
        idempotency_key="invalid-execution-001",
    )
    invalid["payload"]["capability_requirements"] = []
    invalid["target_type"] = "worker"
    rejected = client.post("/api/v1/commands", json=invalid)

    assert outside.status_code == 404
    assert outside.json()["error"]["code"] == "not_found"
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "validation_error"
