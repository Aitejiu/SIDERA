from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings

pytestmark = pytest.mark.integration


def test_application_restart_preserves_task_timeline_and_idempotency(
    database_url: str,
    workspace_id: UUID,
    row_counts: Callable[[], dict[str, int]],
) -> None:
    settings = Settings(database_url=database_url)
    create_request = {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "CreateTask",
        "target_type": "task",
        "actor": {"type": "user", "id": "user:test"},
        "target_id": None,
        "expected_version": None,
        "idempotency_key": "restart-create-task-001",
        "correlation_id": str(uuid4()),
        "payload": {
            "title": "Survive API restart",
            "description": "Reconstruct application state from PostgreSQL",
            "acceptance_criteria": ["Replay returns the original result"],
            "priority": 50,
        },
    }

    with TestClient(create_app(settings)) as first_client:
        created = first_client.post("/api/v1/commands", json=create_request)
        assert created.status_code == 201, created.text

    replay_request = {
        **create_request,
        "command_id": str(uuid4()),
        "correlation_id": str(uuid4()),
    }
    task_id = created.json()["aggregate_id"]
    with TestClient(create_app(settings)) as second_client:
        detail = second_client.get(
            f"/api/v1/tasks/{task_id}",
            params={"workspace_id": str(workspace_id)},
        )
        timeline = second_client.get(
            f"/api/v1/tasks/{task_id}/timeline",
            params={"workspace_id": str(workspace_id)},
        )
        replayed = second_client.post("/api/v1/commands", json=replay_request)

    assert detail.status_code == 200
    assert detail.json()["title"] == "Survive API restart"
    assert detail.json()["version"] == 1
    assert timeline.status_code == 200
    assert [item["event_type"] for item in timeline.json()] == ["TaskCreated"]
    assert replayed.status_code == 200
    assert replayed.json() == created.json()
    assert row_counts() == {
        "tasks": 1,
        "commands": 1,
        "events": 1,
        "outbox_messages": 1,
    }
