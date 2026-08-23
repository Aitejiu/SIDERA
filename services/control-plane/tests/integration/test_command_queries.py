from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url))) as test_client:
        yield test_client


def _create_task_request(
    workspace_id: UUID, command_id: UUID, *, idempotency_key: str
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "Command status task",
        "description": "Verify command terminal queries",
        "acceptance_criteria": ["The terminal result is readable"],
        "priority": 50,
    }
    return {
        "command_id": str(command_id),
        "workspace_id": str(workspace_id),
        "command_type": "CreateTask",
        "target_type": "task",
        "actor": {"type": "user", "id": "user:query-test"},
        "target_id": None,
        "expected_version": None,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def _reject_task_request(
    workspace_id: UUID,
    command_id: UUID,
    task_id: UUID,
    expected_version: int,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    return {
        "command_id": str(command_id),
        "workspace_id": str(workspace_id),
        "command_type": "PauseTask",
        "target_type": "task",
        "actor": {"type": "user", "id": "user:query-test"},
        "target_id": str(task_id),
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": {},
    }


def test_command_status_returns_pending_applied_and_rejected(
    client: TestClient,
    workspace_id: UUID,
) -> None:
    applied_id = uuid4()
    applied_request = _create_task_request(
        workspace_id,
        applied_id,
        idempotency_key="command-query-applied",
    )
    applied = client.post("/api/v1/commands", json=applied_request)
    assert applied.status_code == 201, applied.text

    applied_status = client.get(
        f"/api/v1/workspaces/{workspace_id}/commands/{applied_id}"
    )
    assert applied_status.status_code == 200, applied_status.text
    assert applied_status.json() == {
        "command_id": str(applied_id),
        "status": "applied",
        "result": applied.json(),
        "error_code": None,
    }
    assert applied_status.json()["result"]["command_id"] == str(applied_id)

    task_id = UUID(applied.json()["aggregate_id"])
    first_pause = client.post(
        "/api/v1/commands",
        json=_reject_task_request(
            workspace_id,
            uuid4(),
            task_id,
            1,
            idempotency_key="command-query-rejected",
        ),
    )
    assert first_pause.status_code == 200, first_pause.text

    rejected_id = uuid4()
    rejected = client.post(
        "/api/v1/commands",
        json=_reject_task_request(
            workspace_id,
            rejected_id,
            task_id,
            2,
            idempotency_key="command-query-rejected-2",
        ),
    )
    assert rejected.status_code == 409, rejected.text

    rejected_status = client.get(
        f"/api/v1/workspaces/{workspace_id}/commands/{rejected_id}"
    )
    assert rejected_status.status_code == 200, rejected_status.text
    rejected_body = rejected_status.json()
    assert rejected_body["command_id"] == str(rejected_id)
    assert rejected_body["status"] == "rejected"
    assert rejected_body["result"] is None
    assert rejected_body["error_code"] == "invalid_transition"
    assert "message" not in rejected_body
    assert "details" not in rejected_body

    missing_id = uuid4()
    missing = client.get(
        f"/api/v1/workspaces/{workspace_id}/commands/{missing_id}"
    )
    assert missing.status_code == 200, missing.text
    assert missing.json() == {
        "command_id": str(missing_id),
        "status": "pending",
        "result": None,
        "error_code": None,
    }

    other_workspace = UUID("00000000-0000-0000-0000-000000000099")
    cross_workspace = client.get(
        f"/api/v1/workspaces/{other_workspace}/commands/{applied_id}"
    )
    assert cross_workspace.status_code == 200, cross_workspace.text
    assert cross_workspace.json()["status"] == "pending"
