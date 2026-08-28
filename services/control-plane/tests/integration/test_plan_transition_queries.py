from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_plan_transition_commands import _retire_unknown

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.models import WorkerTransitionModel

pytestmark = pytest.mark.integration


def _client(database_url: str) -> TestClient:
    return TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    )


def test_transition_queries_return_paths_state_and_refs_without_patch_body(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    blocked = _retire_unknown(database, database_url, workspace_id, uow_factory)
    transition_id = blocked["transition_id"]
    plan_transition_id = blocked["plan_transition_id"]
    root_task_id = blocked["seed"]["root_task"].id

    with _client(database_url) as client:
        detail = client.get(
            f"/api/v1/workspaces/{workspace_id}/plan-transitions/{plan_transition_id}"
        )
        worker_detail = client.get(
            f"/api/v1/workspaces/{workspace_id}/worker-transitions/{transition_id}"
        )
        listing = client.get(
            f"/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/plan-transitions"
        )

    assert detail.status_code == 200, detail.text
    assert worker_detail.status_code == 200, worker_detail.text
    assert listing.status_code == 200, listing.text
    body: dict[str, Any] = detail.json()
    worker: dict[str, Any] = worker_detail.json()
    assert body["status"] == "degraded"
    assert body["root_task"]["path"].startswith("Task/")
    assert body["workers"]
    item = next(
        item
        for item in body["workers"]
        if item["worker_transition_id"] == str(transition_id)
    )
    assert item["subtask"]["path"].startswith("Task/")
    assert item["worker_item"]["path"].startswith("Task/")
    assert item["status"] == "blocked_unknown"
    assert item["writer_state"] == "unknown"
    assert item["action"] == "retire"
    assert item["operations"]
    assert item["operations"][0]["operation_id"]
    assert item["context"]["target_context_version"] >= 1
    assert "changed_requirements" not in str(body)
    assert "unfinished_work" not in str(body)
    assert "checkpoint_ref" not in str(body)
    assert worker["worker_transition_id"] == str(transition_id)
    assert worker["status"] == "blocked_unknown"
    assert worker["queued_input_disposition"]["policy"] == "explicit_resend_only"
    assert isinstance(body["blocked_dependencies"], list)
    assert len(listing.json()) == 1


def test_transition_queries_are_read_only_and_workspace_scoped(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    blocked = _retire_unknown(database, database_url, workspace_id, uow_factory)
    transition_id = blocked["transition_id"]
    with database.session() as session:
        before = session.get(WorkerTransitionModel, transition_id)
        assert before is not None
        before_version = before.version
        before_status = before.status

    with _client(database_url) as client:
        missing = client.get(
            f"/api/v1/workspaces/{uuid4()}/worker-transitions/{transition_id}"
        )
        detail = client.get(
            f"/api/v1/workspaces/{workspace_id}/worker-transitions/{transition_id}"
        )
    assert missing.status_code == 404, missing.text
    assert detail.status_code == 200, detail.text

    with database.session() as session:
        after = session.get(before.__class__, transition_id)
        assert after is not None
        assert after.version == before_version
        assert after.status == before_status
