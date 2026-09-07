from __future__ import annotations

from uuid import UUID

import pytest

from sidera_control_plane.domain.result_operations import ResultError

pytestmark = pytest.mark.integration


def test_reintegration_keeps_old_selection_and_creates_a_new_lineage(
    result_scenario,
) -> None:
    result_scenario.seed_plan()
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
    repaired = result_scenario.rework_source(
        UUID(str(rework.data["request_ids"][0])),
        files={"login.py": b"ALLOW_EXPIRED = True\n"},
    )

    reintegrated = result_scenario.command(
        "ReintegrateTaskResult",
        result_scenario.reintegration_payload(first, repaired),
    )

    new_request_id = UUID(str(reintegrated.data["execution_request_id"]))
    new_selection_id = UUID(str(reintegrated.data["input_selection_id"]))
    assert new_request_id != old_integration_request.id
    assert new_selection_id != old_selection.id
    assert result_scenario.selection(old_selection.id) == old_selection
    assert result_scenario.release_for_request(old_integration_request.id) == old_release

    result_scenario.drive()
    second = result_scenario.latest_candidate()
    new_selection = result_scenario.selection(new_selection_id)
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
    assert result_scenario.request(new_request_id).first_execution_id is not None


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
