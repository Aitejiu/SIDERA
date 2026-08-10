from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings

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


def test_create_task_is_idempotent(
    client: TestClient,
    workspace_id: UUID,
    row_counts: Callable[[], dict[str, int]],
) -> None:
    request = create_task_request(workspace_id)

    first = client.post("/api/commands", json=request)

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
        "command_id": str(uuid4()),
        "correlation_id": str(uuid4()),
    }
    second = client.post("/api/commands", json=replay)

    assert second.status_code == 200
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
    conflict = client.post("/api/commands", json=changed_request)

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert row_counts() == {
        "tasks": 1,
        "commands": 1,
        "events": 1,
        "outbox_messages": 1,
    }


def test_create_task_rejects_invalid_payload(
    client: TestClient,
    workspace_id: UUID,
    row_counts: Callable[[], dict[str, int]],
) -> None:
    request = create_task_request(workspace_id)
    del request["payload"]["acceptance_criteria"]

    response = client.post("/api/commands", json=request)

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
    response = client.post("/api/commands", json=create_task_request(uuid4()))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert row_counts() == {
        "tasks": 0,
        "commands": 0,
        "events": 0,
        "outbox_messages": 0,
    }
