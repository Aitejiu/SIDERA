from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_accepted_result_survives_workspace_release_and_restores_in_a_new_directory(
    result_scenario,
) -> None:
    result_scenario.seed_plan(apply=True)
    candidate = result_scenario.candidate()
    expected_tree = result_scenario.candidate_tree(candidate)

    result_scenario.command("CompleteTask", result_scenario.accept_payload(candidate))
    outcome = result_scenario.drive()
    assert outcome.state == "completed"

    result_scenario.release_terminal_workspaces()

    assert result_scenario.restore_candidate(candidate) == expected_tree


def test_result_refs_remain_retained_after_terminal_workspace_cleanup(
    result_scenario,
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate()
    retained = result_scenario.retained_code_results()

    result_scenario.release_terminal_workspaces()

    assert any(item.id == candidate.code_result_id for item in retained)
    assert result_scenario.restore_candidate(candidate) == result_scenario.candidate_tree(
        candidate
    )
