import hashlib
import json
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.task_commands import TaskCommandHandler
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.task import Task, TaskStatus
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import CommandModel, EventModel
from sidera_control_plane.persistence.repositories import TaskRepository
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def create_task_request(workspace_id: UUID) -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "CreateTask",
        "target_type": "task",
        "actor": {"type": "user", "id": "user:test"},
        "target_id": None,
        "expected_version": None,
        "idempotency_key": "create-task-001",
        "correlation_id": str(uuid4()),
        "payload": {
            "title": "Build task API",
            "description": "First vertical slice",
            "acceptance_criteria": ["Duplicate commands create one Task"],
            "priority": 50,
        },
    }


def mutation_request(
    workspace_id: UUID,
    task_id: UUID,
    command_type: str,
    expected_version: int | None,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": "task",
        "actor": {"type": "user", "id": "user:test"},
        "target_id": str(task_id),
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": payload or {},
    }


def create_task(client: TestClient, workspace_id: UUID) -> UUID:
    response = client.post(
        "/api/v1/commands", json=create_task_request(workspace_id)
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["aggregate_id"])


def legacy_request_hash(request: dict[str, Any]) -> str:
    encoded = json.dumps(
        {
            "command_type": request["command_type"],
            "target_id": request["target_id"],
            "expected_version": request["expected_version"],
            "payload": request["payload"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_versioned_command_route_persists_explicit_target_type(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    request = create_task_request(workspace_id)

    versioned = client.post("/api/v1/commands", json=request)
    unversioned = client.post("/api/commands", json=request)

    assert versioned.status_code == 201
    assert unversioned.status_code == 404

    with database.session() as session:
        command = session.scalar(select(CommandModel))
    assert command is not None
    assert command.target_type == "task"


def test_create_task_is_idempotent(
    client: TestClient,
    workspace_id: UUID,
    row_counts: Callable[[], dict[str, int]],
) -> None:
    request = create_task_request(workspace_id)

    first = client.post("/api/v1/commands", json=request)

    assert first.status_code == 201, first.text
    assert first.json()["aggregate_version"] == 1
    assert row_counts() == {
        "tasks": 1,
        "commands": 1,
        "events": 1,
        "outbox_messages": 1,
    }

    replay = {
        **request,
        "correlation_id": str(uuid4()),
    }
    second = client.post("/api/v1/commands", json=replay)

    assert second.status_code == 201
    assert second.json() == first.json()
    assert row_counts() == {
        "tasks": 1,
        "commands": 1,
        "events": 1,
        "outbox_messages": 1,
    }

    changed_request = {
        **replay,
        "command_id": str(uuid4()),
        "payload": {**request["payload"], "title": "Different task"},
    }
    conflict = client.post("/api/v1/commands", json=changed_request)

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert row_counts() == {
        "tasks": 1,
        "commands": 1,
        "events": 1,
        "outbox_messages": 1,
    }


def test_legacy_request_hash_replays_after_target_versioning(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    row_counts: Callable[[], dict[str, int]],
) -> None:
    request = create_task_request(workspace_id)
    first = client.post("/api/v1/commands", json=request)
    assert first.status_code == 201, first.text

    with database.session() as session:
        command = session.scalar(select(CommandModel))
        assert command is not None
        command.request_hash = legacy_request_hash(request)
        session.commit()

    replay = {
        **request,
        "correlation_id": str(uuid4()),
    }
    second = client.post("/api/v1/commands", json=replay)

    assert second.status_code == 201, second.text
    assert second.json() == first.json()
    assert row_counts() == {
        "tasks": 1,
        "commands": 1,
        "events": 1,
        "outbox_messages": 1,
    }


@pytest.mark.parametrize("target_type", ["bogus", "worker"])
def test_task_command_rejects_invalid_target_type(
    client: TestClient,
    workspace_id: UUID,
    row_counts: Callable[[], dict[str, int]],
    target_type: str,
) -> None:
    request = create_task_request(workspace_id)
    request["target_type"] = target_type

    response = client.post("/api/v1/commands", json=request)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert row_counts() == {
        "tasks": 0,
        "commands": 0,
        "events": 0,
        "outbox_messages": 0,
    }


def test_create_task_rejects_invalid_payload(
    client: TestClient,
    workspace_id: UUID,
    row_counts: Callable[[], dict[str, int]],
) -> None:
    request = create_task_request(workspace_id)
    del request["payload"]["acceptance_criteria"]

    response = client.post("/api/v1/commands", json=request)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert row_counts() == {
        "tasks": 0,
        "commands": 0,
        "events": 0,
        "outbox_messages": 0,
    }


def test_create_task_requires_existing_workspace(
    client: TestClient,
    row_counts: Callable[[], dict[str, int]],
) -> None:
    response = client.post("/api/v1/commands", json=create_task_request(uuid4()))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert row_counts() == {
        "tasks": 0,
        "commands": 0,
        "events": 0,
        "outbox_messages": 0,
    }


def test_versioned_task_mutations_and_successful_replay(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    row_counts: Callable[[], dict[str, int]],
) -> None:
    task_id = create_task(client, workspace_id)
    priority_request = mutation_request(
        workspace_id,
        task_id,
        "SetTaskPriority",
        1,
        "set-priority-001",
        {"priority": 90},
    )
    commands = (
        (priority_request, 2),
        (mutation_request(workspace_id, task_id, "PauseTask", 2, "pause-001"), 3),
        (mutation_request(workspace_id, task_id, "ResumeTask", 3, "resume-001"), 4),
        (mutation_request(workspace_id, task_id, "CancelTask", 4, "cancel-001"), 5),
    )

    responses = []
    for request, expected_version in commands:
        response = client.post("/api/v1/commands", json=request)
        assert response.status_code == 200, response.text
        assert response.json()["aggregate_version"] == expected_version
        responses.append(response)

    replay = {
        **priority_request,
        "correlation_id": str(uuid4()),
    }
    replayed = client.post("/api/v1/commands", json=replay)

    assert replayed.status_code == 200
    assert replayed.json() == responses[0].json()
    assert row_counts() == {
        "tasks": 1,
        "commands": 5,
        "events": 5,
        "outbox_messages": 5,
    }
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, workspace_id)
    assert task is not None
    assert task.status is TaskStatus.CANCELED
    assert task.priority == 90
    assert task.version == 5
    with database.session() as session:
        events = session.execute(
            select(
                EventModel.event_type,
                EventModel.aggregate_version,
                EventModel.payload,
            ).order_by(EventModel.aggregate_version)
        ).all()
    assert [event.event_type for event in events] == [
        "TaskCreated",
        "TaskPriorityChanged",
        "TaskPaused",
        "TaskResumed",
        "TaskCanceled",
    ]
    assert events[1].payload == {"priority": 90, "previous_priority": 50}
    assert events[2].payload == {"status": "paused", "previous_status": "pending"}
    assert events[3].payload == {"status": "pending", "previous_status": "paused"}
    assert events[4].payload == {"status": "canceled", "previous_status": "pending"}


def test_complete_task_requires_waiting_and_is_idempotent(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
) -> None:
    task_id = create_task(client, workspace_id)
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, workspace_id)
        assert task is not None
        task.mark_ready()
        uow.tasks.save(task, 1)
        task.mark_running()
        uow.tasks.save(task, 2)
        task.wait_for_decision()
        uow.tasks.save(task, 3)

    request = mutation_request(
        workspace_id,
        task_id,
        "CompleteTask",
        4,
        "complete-task-001",
    )
    completed = client.post("/api/v1/commands", json=request)

    assert completed.status_code == 200, completed.text
    assert completed.json()["aggregate_version"] == 5
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, workspace_id)
    assert task is not None
    assert task.status is TaskStatus.SUCCEEDED
    assert task.version == 5

    replay = {**request, "correlation_id": str(uuid4())}
    replayed = client.post("/api/v1/commands", json=replay)

    assert replayed.status_code == 200
    assert replayed.json() == completed.json()
    with database.session() as session:
        event_types = session.scalars(
            select(EventModel.event_type).order_by(EventModel.created_at, EventModel.id)
        ).all()
    assert event_types == ["TaskCreated", "TaskCompleted"]


def test_complete_task_rejects_non_waiting_status(
    client: TestClient,
    workspace_id: UUID,
) -> None:
    task_id = create_task(client, workspace_id)

    rejected = client.post(
        "/api/v1/commands",
        json=mutation_request(
            workspace_id,
            task_id,
            "CompleteTask",
            1,
            "complete-pending-task-001",
        ),
    )

    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "invalid_transition"


def test_stale_version_records_one_rejection_and_replays_it(
    client: TestClient,
    workspace_id: UUID,
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    row_counts: Callable[[], dict[str, int]],
) -> None:
    task_id = create_task(client, workspace_id)
    updated = client.post(
        "/api/v1/commands",
        json=mutation_request(
            workspace_id,
            task_id,
            "SetTaskPriority",
            1,
            "set-priority-001",
            {"priority": 90},
        ),
    )
    assert updated.status_code == 200, updated.text
    stale_request = mutation_request(
        workspace_id, task_id, "PauseTask", 1, "stale-pause-001"
    )

    stale = client.post("/api/v1/commands", json=stale_request)

    assert stale.status_code == 409
    assert stale.json() == {
        "error": {
            "code": "version_conflict",
            "message": "expected version 1, actual version 2",
            "details": {"expected": 1, "actual": 2},
        }
    }
    assert row_counts() == {
        "tasks": 1,
        "commands": 3,
        "events": 2,
        "outbox_messages": 2,
    }

    replay = {
        **stale_request,
        "correlation_id": str(uuid4()),
    }
    replayed = client.post("/api/v1/commands", json=replay)

    assert replayed.status_code == 409
    assert replayed.json() == stale.json()
    assert row_counts() == {
        "tasks": 1,
        "commands": 3,
        "events": 2,
        "outbox_messages": 2,
    }
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, workspace_id)
    assert task is not None
    assert task.status is TaskStatus.PENDING
    assert task.priority == 90
    assert task.version == 2


def test_terminal_task_mutation_records_rejection_without_event(
    client: TestClient,
    workspace_id: UUID,
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    row_counts: Callable[[], dict[str, int]],
) -> None:
    task_id = create_task(client, workspace_id)
    canceled = client.post(
        "/api/v1/commands",
        json=mutation_request(
            workspace_id, task_id, "CancelTask", 1, "cancel-001"
        ),
    )
    assert canceled.status_code == 200, canceled.text

    rejected = client.post(
        "/api/v1/commands",
        json=mutation_request(
            workspace_id,
            task_id,
            "SetTaskPriority",
            2,
            "priority-after-cancel-001",
            {"priority": 80},
        ),
    )

    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "invalid_transition"
    assert row_counts() == {
        "tasks": 1,
        "commands": 3,
        "events": 2,
        "outbox_messages": 2,
    }
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, workspace_id)
    assert task is not None
    assert task.status is TaskStatus.CANCELED
    assert task.priority == 50
    assert task.version == 2


def test_mutation_requires_target_and_expected_version(
    client: TestClient,
    workspace_id: UUID,
    row_counts: Callable[[], dict[str, int]],
) -> None:
    request = mutation_request(
        workspace_id, uuid4(), "PauseTask", 1, "pause-without-target"
    )
    request["target_id"] = None
    without_target = client.post("/api/v1/commands", json=request)

    request = mutation_request(
        workspace_id, uuid4(), "PauseTask", None, "pause-without-version"
    )
    without_version = client.post("/api/v1/commands", json=request)

    assert without_target.status_code == 422
    assert without_version.status_code == 422
    assert row_counts() == {
        "tasks": 0,
        "commands": 0,
        "events": 0,
        "outbox_messages": 0,
    }


def test_two_session_race_applies_one_mutation_and_audits_one_rejection(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    row_counts: Callable[[], dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = create_task(client, workspace_id)
    barrier = threading.Barrier(2)
    original_get = TaskRepository.get

    def synchronized_get(
        repository: TaskRepository,
        requested_task_id: UUID,
        requested_workspace_id: UUID,
    ) -> Task | None:
        task = original_get(repository, requested_task_id, requested_workspace_id)
        barrier.wait(timeout=5)
        return task

    monkeypatch.setattr(TaskRepository, "get", synchronized_get)
    service = CommandService(uow_factory, (TaskCommandHandler(),))
    envelopes = tuple(
        CommandEnvelope(
            id=uuid4(),
            workspace_id=workspace_id,
            command_type="SetTaskPriority",
            target_type="task",
            actor_type="user",
            actor_id=f"user:race-{priority}",
            target_id=task_id,
            payload={"priority": priority},
            expected_version=1,
            idempotency_key=f"race-priority-{priority}",
            correlation_id=uuid4(),
        )
        for priority in (70, 90)
    )

    applied: list[CommandResult] = []
    rejected: list[VersionConflict] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(service.handle, envelope) for envelope in envelopes]
        for future in futures:
            try:
                applied.append(future.result(timeout=10))
            except VersionConflict as exc:
                rejected.append(exc)
    monkeypatch.setattr(TaskRepository, "get", original_get)

    assert len(applied) == 1
    assert len(rejected) == 1
    assert rejected[0].expected == 1
    assert rejected[0].actual == 2
    assert row_counts() == {
        "tasks": 1,
        "commands": 3,
        "events": 2,
        "outbox_messages": 2,
    }
    with database.session() as session:
        mutation_statuses = session.scalars(
            select(CommandModel.status)
            .where(CommandModel.command_type == "SetTaskPriority")
            .order_by(CommandModel.status)
        ).all()
    assert mutation_statuses == ["applied", "rejected"]

    with uow_factory() as uow:
        task = uow.tasks.get(task_id, workspace_id)
    assert task is not None
    assert task.version == 2
    assert task.priority in {70, 90}
