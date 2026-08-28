from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_plan_transition_commands import _retire_unknown

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.models import ProjectionChangeModel

pytestmark = pytest.mark.integration


def _client(database_url: str) -> TestClient:
    return TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    )


def test_revision_convergence_is_projected_at_root_subtask_and_worker_scopes(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    blocked = _retire_unknown(database, database_url, workspace_id, uow_factory)
    root_task_id = blocked["seed"]["root_task"].id
    worker_item_id = blocked["seed"]["worker_item"].id
    subtask_key = blocked["seed"]["snapshot"].subtasks[0].key

    with _client(database_url) as client:
        root_response = client.get(
            f"/api/v1/tasks/{root_task_id}/overview",
            params={"workspace_id": str(workspace_id)},
        )
        subtask_response = client.get(
            f"/api/v1/tasks/{root_task_id}/subtasks/{subtask_key}",
            params={"workspace_id": str(workspace_id)},
        )
        worker_response = client.get(
            f"/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/workspace"
        )

    assert root_response.status_code == 200, root_response.text
    assert subtask_response.status_code == 200, subtask_response.text
    assert worker_response.status_code == 200, worker_response.text

    root: dict[str, Any] = root_response.json()
    convergence = root["convergence"]
    assert root["plan_head"]["active_generation"] >= 1
    assert root["active_revision"]["planning_request_id"]
    assert root["active_revision"]["target_generation"] == 2
    assert convergence["status"] == "degraded"
    assert convergence["unknown_count"] >= 1
    assert convergence["confirmation_blocked"] is True

    subtask: dict[str, Any] = subtask_response.json()
    assert subtask["convergence"]["status"] == "blocked_unknown"
    assert subtask["subtask"]["convergence"]["unknown_count"] >= 1
    assert subtask["worker_items"][0]["plan_sync"]["status"] == "waiting_activation"

    worker: dict[str, Any] = worker_response.json()
    sync = worker["plan_sync"]
    assert sync["status"] == "blocked_unknown"
    assert sync["source_generation"] == 1
    assert sync["target_generation"] == 2
    assert sync["current_context_version"] >= 1
    assert isinstance(sync["queued_input_ids"], list)
    assert isinstance(sync["superseded_input_ids"], list)
    assert sync["trace_href"] is None
    assert "patch_body" not in str(worker)
    assert "provider_process_ref" not in str(worker)

    with database.session() as session:
        root_changes = session.scalars(
            select(ProjectionChangeModel).where(
                ProjectionChangeModel.workspace_id == workspace_id,
                ProjectionChangeModel.resource_id == root_task_id,
            )
        ).all()
        worker_changes = session.scalars(
            select(ProjectionChangeModel).where(
                ProjectionChangeModel.workspace_id == workspace_id,
                ProjectionChangeModel.resource_id == worker_item_id,
            )
        ).all()

    assert any(
        change.projection_type == "task_overview"
        and change.change_type == "planning_materialized"
        for change in root_changes
    )
    assert any(
        change.projection_type == "worker_item_workspace"
        and change.change_type == "WorkerTransitionBlockedUnknown"
        for change in worker_changes
    )
