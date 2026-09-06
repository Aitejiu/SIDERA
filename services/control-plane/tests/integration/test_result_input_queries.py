from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from .result_input_scenario import ResultInputScenario

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(
    database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)


def test_worker_result_query_does_not_leak_body(scenario) -> None:
    scenario.run_to_released_summary()

    response = scenario.get_result_inputs("summary")

    assert response.status_code == 200, response.text
    value = response.json()
    assert value["schema_version"] == 1
    assert value["is_historical"] is False
    assert value["request"]["execution_request_id"] == str(
        scenario.request_ids["summary"]
    )
    assert value["groups"][0]["ready_count"] == 2
    assert len(value["groups"][0]["selected_sources"]) == 2
    assert value["groups"][0]["selection_id"] is not None
    assert "body_text" not in response.text
    assert "storage_ref" not in response.text


def test_selection_query_returns_only_fixed_metadata_and_bounded_consumers(
    scenario,
) -> None:
    scenario.run_to_released_summary()
    selection = scenario.selection("summary")

    response = scenario.get_input_selection(selection.id)

    assert response.status_code == 200, response.text
    value = response.json()
    assert value["selection_id"] == str(selection.id)
    assert value["selection_hash"] == selection.selection_hash
    assert value["target_worker_item_id"] == str(scenario.worker_item_ids["summary"])
    assert len(value["groups"][0]["sources"]) == 2
    assert value["consumers"] == []
    assert "body_text" not in response.text
    assert "storage_ref" not in response.text


def test_worker_result_query_can_read_a_same_worker_historical_request(
    scenario,
) -> None:
    scenario.run_to_released_summary()
    historical_request_id = scenario.request_ids["summary"]

    scenario.revise_group(minimum_count=3)

    response = scenario.get_result_inputs(
        "summary", execution_request_id=historical_request_id
    )

    assert response.status_code == 200, response.text
    value = response.json()
    assert value["is_historical"] is True
    assert value["request"]["execution_request_id"] == str(historical_request_id)
    assert value["plan_generation"] == 1


def test_worker_result_query_rejects_a_request_from_another_worker(scenario) -> None:
    scenario.run_to_released_summary()

    response = scenario.get_result_inputs(
        "a", execution_request_id=scenario.request_ids["summary"]
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert "Summary worker" not in response.text


def test_selection_query_rejects_a_different_workspace(scenario) -> None:
    scenario.run_to_released_summary()
    selection = scenario.selection("summary")

    response = scenario.get_input_selection(
        selection.id,
        workspace_id=UUID("20000000-0000-0000-0000-000000000099"),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_worker_result_query_keeps_legacy_worker_item_groups_empty(scenario) -> None:
    scenario.confirm_plan(groups=())

    response = scenario.get_result_inputs("summary")

    assert response.status_code == 200, response.text
    value = response.json()
    assert value["groups"] == []
    assert value["hard_dependencies"] == []


def test_worker_result_query_exposes_execution_delivery_metadata(scenario) -> None:
    scenario.run_to_started_summary()

    response = scenario.get_result_inputs("summary")

    assert response.status_code == 200, response.text
    value = response.json()
    assert value["execution"]["execution_id"] is not None
    assert value["execution"]["status"] == "running"
    assert value["delivery"]["assembly_request_id"] is not None
    assert value["delivery"]["package_id"] is not None
    assert value["delivery"]["acknowledgement_status"] == "acknowledged"
    assert "body_text" not in response.text
    assert "storage_ref" not in response.text


def test_worker_result_query_keeps_waiting_input_state_separate(scenario) -> None:
    scenario.confirm_plan(max_attempts=2)
    scenario.complete_source("b", body=b"report-b\n")
    scenario.reconcile()

    response = scenario.get_result_inputs("summary")

    assert response.status_code == 200, response.text
    value = response.json()
    assert value["groups"][0]["ready_count"] == 1
    assert value["groups"][0]["selected_sources"] == []
    assert value["execution"]["release_status"] == "not_released"
    assert value["delivery"]["assembly_request_id"] is None


def test_worker_result_query_keeps_failed_execution_visible(scenario) -> None:
    scenario.run_to_started_summary()
    scenario.fail_worker("summary")

    response = scenario.get_result_inputs("summary")

    assert response.status_code == 200, response.text
    value = response.json()
    assert value["execution"]["status"] == "failed"
    assert value["execution"]["failure_code"] == "scenario_failure:summary"
    assert value["delivery"]["acknowledgement_status"] == "acknowledged"


def test_selection_query_indexes_acknowledged_consumers(scenario) -> None:
    scenario.run_to_started_summary()
    selection = scenario.selection("summary")

    response = scenario.get_input_selection(selection.id)

    assert response.status_code == 200, response.text
    value = response.json()
    assert len(value["consumers"]) == 1
    assert value["consumers"][0]["consumer_id"] is not None
    assert value["consumers"][0]["execution_request_id"] == str(
        scenario.request_ids["summary"]
    )
    assert value["consumers"][0]["package_id"] is not None
    assert value["consumers"][0]["acknowledgement_status"] == "acknowledged"


def test_worker_result_query_keeps_ack_before_start_distinct(scenario) -> None:
    scenario.run_to_released_summary()
    scenario.activate()
    scenario.assemble_context("summary")
    scenario.complete_source("a", body=b"report-a\n")
    with pytest.raises(RuntimeError, match="after context acknowledgement"):
        scenario.start_worker("summary", crash_after_context_ack=True)

    response = scenario.get_result_inputs("summary")

    assert response.status_code == 200, response.text
    value = response.json()
    assert value["execution"]["status"] == "leased"
    assert value["delivery"]["acknowledgement_status"] == "acknowledged"
    assert value["delivery"]["materialization_outcome"] == "succeeded"
