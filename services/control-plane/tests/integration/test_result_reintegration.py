from __future__ import annotations

from uuid import UUID

import pytest

from sidera_control_plane.domain.result_operations import ResultError

pytestmark = pytest.mark.integration


def test_reworked_hard_dependency_reintegration_keeps_old_selection_and_creates_a_new_lineage(
    result_scenario,
) -> None:
    result_scenario.seed_plan(
        integration_base_commit=result_scenario.base_commit,
        hard_edges=(("a", "summary"),),
        hard_edge_artifact_refs=("artifact_type:report",),
    )
    original_a_request_id = result_scenario.request_ids["a"]
    first = result_scenario.candidate()
    old_selection = result_scenario.selection(first.input_selection_id)
    old_integration_request = result_scenario.request(
        result_scenario.request_ids["summary"]
    )
    old_release = result_scenario.release_for_request(old_integration_request.id)

    rework = result_scenario.command(
        "RequestTaskRework",
        result_scenario.rework_payload(first),
    )
    rework_request_id = UUID(str(rework.data["request_ids"][0]))
    repaired = result_scenario.rework_source(
        rework_request_id,
        files={"login.py": b"ALLOW_EXPIRED = True\n"},
    )
    assert repaired.consumed_result_ids == first.source_result_ids

    reintegrated = result_scenario.command(
        "ReintegrateTaskResult",
        result_scenario.reintegration_payload(first, repaired),
    )

    new_request_id = UUID(str(reintegrated.data["execution_request_id"]))
    new_selection_id = UUID(str(reintegrated.data["input_selection_id"]))
    new_selection = result_scenario.selection(new_selection_id)
    hard_input = next(
        item
        for item in new_selection.hard_inputs
        if item.dependency_key == f"request:{original_a_request_id}"
    )
    assert new_request_id != old_integration_request.id
    assert new_selection_id != old_selection.id
    assert hard_input.source.effective_request_id == rework_request_id
    assert rework_request_id != original_a_request_id
    assert result_scenario.selection(old_selection.id) == old_selection
    assert result_scenario.release_for_request(old_integration_request.id) == old_release

    result_scenario.drive()
    request = result_scenario.request(new_request_id)
    assert request.first_execution_id is not None, (
        request.gate_reason,
        dict(request.gate_details),
    )
    second = result_scenario.latest_candidate()
    assert second.id != first.id
    assert second.previous_candidate_id == first.id
    assert second.input_selection_id == new_selection_id
    assert second.plan_snapshot_id == first.plan_snapshot_id
    assert second.plan_content_hash == first.plan_content_hash
    assert repaired.id in second.source_result_ids
    old_sources = {
        source.worker_item_key: source
        for group in old_selection.groups
        for source in group.sources
    }
    new_sources = {
        source.worker_item_key: source
        for group in new_selection.groups
        for source in group.sources
    }
    assert new_sources["a"].effective_execution_id == repaired.execution_id
    assert new_sources["b"].effective_execution_id == old_sources["b"].effective_execution_id
    assert new_sources["a"].effective_execution_id != old_sources["a"].effective_execution_id


def test_reintegration_rejects_an_unrelated_result_lineage(result_scenario) -> None:
    result_scenario.seed_plan()
    first = result_scenario.candidate()
    result_scenario.source(
        "c",
        files={"unrelated.py": b"UNRELATED = True\n"},
        commit=True,
    )

    with pytest.raises(ResultError, match="reintegrate_result_not_authorized"):
        result_scenario.command(
            "ReintegrateTaskResult",
            result_scenario.reintegration_payload(
                first,
                result_scenario.latest_source("c"),
            ),
        )
