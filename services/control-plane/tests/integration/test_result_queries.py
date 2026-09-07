from __future__ import annotations

from collections.abc import Iterator
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


def _results_path(workspace_id: UUID, root_task_id: UUID) -> str:
    return f"/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/results"


def test_result_overview_distinguishes_candidate_from_root_completion(
    client: TestClient, result_scenario
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate()

    response = client.get(
        _results_path(result_scenario.workspace_id, result_scenario.root_id)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workspace_id"] == str(result_scenario.workspace_id)
    assert body["root_task_id"] == str(result_scenario.root_id)
    assert body["result_stage"] == "awaiting_acceptance"
    assert body["current_candidate"]["candidate_id"] == str(candidate.id)
    assert body["acceptance"] is None
    assert body["available_actions"]
    assert any(
        action["command_type"] == "CompleteTask"
        for action in body["available_actions"]
    )
    assert result_scenario.events("TaskCompleted") == ()


def test_result_detail_is_fixed_to_candidate_and_has_bounded_subresources(
    client: TestClient, result_scenario
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate()

    response = client.get(
        f"{_results_path(result_scenario.workspace_id, result_scenario.root_id)}"
        f"/{candidate.id}"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["candidate"]["candidate_id"] == str(candidate.id)
    assert body["candidate"]["candidate_hash"] == candidate.content_hash
    assert body["source_results"]
    assert body["verifications"] == []
    assert body["rework"] == []
    assert "repository_path" not in response.text
    assert "storage_ref" not in response.text

    verifications = client.get(
        f"{_results_path(result_scenario.workspace_id, result_scenario.root_id)}"
        f"/{candidate.id}/verifications",
        params={"limit": 20},
    )
    rework = client.get(
        f"{_results_path(result_scenario.workspace_id, result_scenario.root_id)}"
        f"/{candidate.id}/rework",
        params={"limit": 20},
    )
    assert verifications.status_code == 200, verifications.text
    assert rework.status_code == 200, rework.text
    assert verifications.json()["items"] == []
    assert rework.json()["items"] == []


def test_result_queries_enforce_workspace_candidate_and_cursor_scope(
    client: TestClient, result_scenario
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate()
    root_path = _results_path(result_scenario.workspace_id, result_scenario.root_id)

    foreign_workspace = client.get(
        _results_path(uuid4(), result_scenario.root_id)
    )
    unknown_candidate = client.get(f"{root_path}/{uuid4()}")
    invalid_cursor = client.get(
        root_path,
        params={"cursor": "not-a-result-cursor"},
    )
    invalid_limit = client.get(
        f"{root_path}/{candidate.id}/verifications",
        params={"limit": 101},
    )

    assert foreign_workspace.status_code == 404
    assert foreign_workspace.json()["error"]["code"] == "not_found"
    assert unknown_candidate.status_code == 404
    assert unknown_candidate.json()["error"]["code"] == "not_found"
    assert invalid_cursor.status_code == 422
    assert invalid_cursor.json()["error"]["code"] in {
        "validation_error",
        "result_cursor_invalid",
    }
    assert invalid_limit.status_code == 422
    assert invalid_limit.json()["error"]["code"] == "validation_error"
