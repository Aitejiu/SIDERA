from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.adapters.git_results import GitResultStore
from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.result_input_queries import (
    ResultInputQueryService,
)
from sidera_control_plane.application.result_queries import ResultQueryService
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.result_input_query_store import (
    SqlAlchemyResultInputQueryStore,
)
from sidera_control_plane.persistence.result_query_store import (
    SqlAlchemyResultQueryStore,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url))) as test_client:
        yield test_client


def _results_path(workspace_id: UUID, root_task_id: UUID) -> str:
    return f"/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/results"


def test_result_overview_keeps_unverified_candidate_in_verifying_stage(
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
    assert body["result_stage"] == "verifying"
    assert body["current_candidate"]["candidate_id"] == str(candidate.id)
    with result_scenario.uow() as uow:
        code_result = uow.results.get_record(
            "code_snapshot", candidate.code_result_id, result_scenario.workspace_id
        )
    assert code_result is not None
    expected_artifact_ids = {str(item) for item in code_result.artifact_ids}
    actual_artifact_ids = {
        artifact["artifact_id"]
        for artifact in body["current_candidate"]["artifacts"]
    }
    assert expected_artifact_ids <= actual_artifact_ids
    assert body["acceptance"] is None
    assert all(
        action["command_type"] != "CompleteTask"
        for action in body["available_actions"]
    )
    assert result_scenario.events("TaskCompleted") == ()


def test_result_overview_exposes_acceptance_after_automatic_checks_finish(
    client: TestClient, result_scenario
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=0)

    response = client.get(
        _results_path(result_scenario.workspace_id, result_scenario.root_id)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result_stage"] == "awaiting_acceptance"
    assert body["current_candidate"]["candidate_id"] == str(candidate.id)
    assert body["criteria"][0]["observed_status"] == "passed"
    assert body["criteria"][0]["verification_status"] == "passed"
    assert any(
        action["command_type"] == "CompleteTask"
        for action in body["available_actions"]
    )


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


def test_acceptance_basis_is_derived_from_the_current_candidate(
    client: TestClient, result_scenario
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=0)
    payload = result_scenario.accept_payload(candidate)

    response = client.post(
        f"{_results_path(result_scenario.workspace_id, result_scenario.root_id)}"
        f"/{candidate.id}/acceptance-basis",
        json={"criteria": payload["criteria"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["candidate_id"] == str(candidate.id)
    assert body["candidate_hash"] == candidate.content_hash
    assert body["expected_result_version"] == result_scenario.head().result_version
    assert body["acceptance_basis_hash"] == payload["acceptance_basis_hash"]
    assert result_scenario.events("TaskAcceptanceRecorded") == ()


def test_worker_result_query_prefers_the_latest_rework_request(
    result_scenario,
) -> None:
    result_scenario.seed_repairable_candidate()
    candidate = result_scenario.current_candidate()
    rework = result_scenario.command(
        "RequestTaskRework",
        result_scenario.rework_payload(candidate),
    )
    rework_request_id = UUID(str(rework.data["request_ids"][0]))
    replacement = result_scenario.rework_source(
        rework_request_id,
        files={"login.py": b"ALLOW_EXPIRED = False\n"},
    )
    result_scenario.command(
        "ReintegrateTaskResult",
        result_scenario.reintegration_payload(candidate, replacement),
    )
    result_scenario.drive()

    view = ResultInputQueryService(
        SqlAlchemyResultInputQueryStore(result_scenario.database),
        governance_enabled=True,
    ).get_worker_result_inputs(
        result_scenario.workspace_id,
        result_scenario.worker_item_ids["a"],
    )

    assert view.request is not None
    assert view.request.execution_request_id == rework_request_id
    assert view.governance.release_state == "unreleased"


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


def test_result_overview_marks_missing_git_capture_as_unavailable(
    result_scenario,
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate()
    snapshot = result_scenario.latest_source("login")
    result_scenario._git(
        result_scenario._repository,
        "update-ref",
        "-d",
        f"refs/sidera/results/{snapshot.capture_operation_id}",
    )

    service = ResultQueryService(
        SqlAlchemyResultQueryStore(result_scenario.database),
        storage_inspector=GitResultStore(
            {"repo://integration-test": result_scenario._repository}
        ),
    )
    detail = service.get_detail(
        result_scenario.workspace_id,
        result_scenario.root_task_id,
        candidate.id,
        verification_cursor=None,
        rework_cursor=None,
        limit=20,
    )

    assert detail.source_results
    assert detail.source_results[0].storage_status == "missing"
