from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import WorkspaceModel

pytestmark = pytest.mark.integration


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


def create_task(
    client: TestClient,
    workspace_id: UUID,
    title: str,
) -> tuple[UUID, dict[str, Any]]:
    request = {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "CreateTask",
        "target_type": "task",
        "actor": {"type": "user", "id": "user:test"},
        "target_id": None,
        "expected_version": None,
        "idempotency_key": f"create-{title}",
        "correlation_id": str(uuid4()),
        "payload": {
            "title": title,
            "description": f"Description for {title}",
            "acceptance_criteria": [f"{title} is complete"],
            "priority": 50,
        },
    }
    response = client.post("/api/v1/commands", json=request)
    assert response.status_code == 201, response.text
    return UUID(response.json()["aggregate_id"]), request


def test_task_list_detail_pagination_and_workspace_isolation(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    workspace_b_id = uuid4()
    add_workspace(database, workspace_b_id, "Workspace B")
    first_id, _ = create_task(client, workspace_id, "First A task")
    second_id, _ = create_task(client, workspace_id, "Second A task")
    workspace_b_task_id, _ = create_task(client, workspace_b_id, "Workspace B task")

    listed = client.get(
        "/api/v1/tasks",
        params={"workspace_id": str(workspace_id), "limit": 100, "offset": 0},
    )

    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 2
    assert listed.json()["limit"] == 100
    assert listed.json()["offset"] == 0
    assert [item["id"] for item in listed.json()["items"]] == [
        str(second_id),
        str(first_id),
    ]

    paged = client.get(
        "/api/v1/tasks",
        params={"workspace_id": str(workspace_id), "limit": 1, "offset": 1},
    )
    assert paged.status_code == 200
    assert paged.json()["total"] == 2
    assert [item["id"] for item in paged.json()["items"]] == [str(first_id)]

    detail = client.get(
        f"/api/v1/tasks/{second_id}",
        params={"workspace_id": str(workspace_id)},
    )
    assert detail.status_code == 200
    assert detail.json() == {
        "id": str(second_id),
        "workspace_id": str(workspace_id),
        "title": "Second A task",
        "description": "Description for Second A task",
        "acceptance_criteria": ["Second A task is complete"],
        "status": "pending",
        "priority": 50,
        "requested_by": "user:test",
        "version": 1,
        "created_at": detail.json()["created_at"],
        "updated_at": detail.json()["updated_at"],
    }
    assert detail.json()["created_at"]
    assert detail.json()["updated_at"]

    isolated = client.get(
        f"/api/v1/tasks/{workspace_b_task_id}",
        params={"workspace_id": str(workspace_id)},
    )
    assert isolated.status_code == 404
    assert isolated.json()["error"]["code"] == "not_found"

    isolated_timeline = client.get(
        f"/api/v1/tasks/{workspace_b_task_id}/timeline",
        params={"workspace_id": str(workspace_id)},
    )
    assert isolated_timeline.status_code == 404
    assert isolated_timeline.json()["error"]["code"] == "not_found"

    legacy_list = client.get(
        "/api/tasks",
        params={"workspace_id": str(workspace_id)},
    )
    legacy_detail = client.get(
        f"/api/tasks/{second_id}",
        params={"workspace_id": str(workspace_id)},
    )
    legacy_timeline = client.get(
        f"/api/tasks/{second_id}/timeline",
        params={"workspace_id": str(workspace_id)},
    )
    assert legacy_list.status_code == 404
    assert legacy_detail.status_code == 404
    assert legacy_timeline.status_code == 404


def test_task_timeline_is_ordered_and_contains_audit_links(
    client: TestClient,
    workspace_id: UUID,
) -> None:
    task_id, create_request = create_task(client, workspace_id, "Timeline task")
    mutation_request = {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "SetTaskPriority",
        "target_type": "task",
        "actor": {"type": "user", "id": "user:test"},
        "target_id": str(task_id),
        "expected_version": 1,
        "idempotency_key": "timeline-priority-001",
        "correlation_id": str(uuid4()),
        "payload": {"priority": 90},
    }
    changed = client.post("/api/v1/commands", json=mutation_request)
    assert changed.status_code == 200, changed.text

    timeline = client.get(
        f"/api/v1/tasks/{task_id}/timeline",
        params={"workspace_id": str(workspace_id)},
    )

    assert timeline.status_code == 200, timeline.text
    items = timeline.json()
    assert [item["event_type"] for item in items] == [
        "TaskCreated",
        "TaskPriorityChanged",
    ]
    assert [item["aggregate_version"] for item in items] == [1, 2]
    assert items[0]["actor"] == {"type": "user", "id": "user:test"}
    assert items[0]["causation_id"] == create_request["command_id"]
    assert items[0]["correlation_id"] == create_request["correlation_id"]
    assert items[1]["payload"] == {"priority": 90, "previous_priority": 50}
    assert items[1]["causation_id"] == mutation_request["command_id"]
    assert items[1]["correlation_id"] == mutation_request["correlation_id"]
    assert all(item["created_at"] for item in items)


def test_task_list_bounds_are_validated(
    client: TestClient,
    workspace_id: UUID,
) -> None:
    too_many = client.get(
        "/api/v1/tasks",
        params={"workspace_id": str(workspace_id), "limit": 101},
    )
    negative_offset = client.get(
        "/api/v1/tasks",
        params={"workspace_id": str(workspace_id), "offset": -1},
    )

    assert too_many.status_code == 422
    assert too_many.json()["error"]["code"] == "validation_error"
    assert negative_offset.status_code == 422
    assert negative_offset.json()["error"]["code"] == "validation_error"
