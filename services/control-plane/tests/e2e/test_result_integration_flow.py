from __future__ import annotations

from uuid import UUID

import pytest

from sidera_control_plane.domain.result_operations import ResultOperationStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("clean_database"),
]


def _repair(result_scenario, first):
    rework = result_scenario.command(
        "RequestTaskRework",
        result_scenario.rework_payload(first),
    )
    request_id = UUID(str(rework.data["request_ids"][0]))
    repaired = result_scenario.rework_source(
        request_id,
        files={"login.py": b"ALLOW_EXPIRED = False\n"},
    )
    result_scenario.command(
        "ReintegrateTaskResult",
        result_scenario.reintegration_payload(first, repaired),
    )
    second = result_scenario.drive()
    assert second.id != first.id
    assert second.previous_candidate_id == first.id
    assert repaired.id in second.source_result_ids
    return second


def test_complete_result_flow_is_provider_free_and_does_not_apply_by_default(
    result_scenario,
) -> None:
    result_scenario.seed_plan(apply=False)
    first = result_scenario.seed_repairable_candidate()
    assert result_scenario.verification_results(first.id)[0].status == "failed"

    second = _repair(result_scenario, first)

    assert result_scenario.verification_results(second.id)[0].status == "passed"
    before = result_scenario.target_commit()
    result_scenario.command("CompleteTask", result_scenario.accept_payload(second))
    outcome = result_scenario.drive()

    assert result_scenario.root().status.value == "succeeded", outcome
    assert result_scenario.target_commit() == before
    assert result_scenario.candidate_tree(second) == result_scenario.restore_candidate(
        second
    )
    assert len(result_scenario.events("TaskCompleted")) == 1
    assert all(
        operation.status is not ResultOperationStatus.UNKNOWN
        for operation in result_scenario.result_operations()
    )


def test_complete_result_flow_can_opt_in_to_local_fast_forward(
    result_scenario,
) -> None:
    result_scenario.seed_plan(apply=True)
    first = result_scenario.seed_repairable_candidate()
    second = _repair(result_scenario, first)

    result_scenario.command("CompleteTask", result_scenario.accept_payload(second))
    outcome = result_scenario.drive()

    assert outcome.state == "completed", outcome
    assert result_scenario.root().status.value == "succeeded"
    assert result_scenario.target_commit() == result_scenario.candidate_commit(second)
    assert result_scenario.candidate_tree(second) == result_scenario.restore_candidate(
        second
    )
    assert len(result_scenario.events("TaskCompleted")) == 1
