from __future__ import annotations

import pytest

from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.domain.result_verification import VerificationStatus

pytestmark = pytest.mark.integration


def test_real_check_failure_cannot_be_overridden_by_agent_report(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)

    results = result_scenario.verification_results(candidate.id)

    assert len(results) == 1
    assert results[0].exit_code == 7
    assert results[0].status is VerificationStatus.FAILED
    assert results[0].actual_commit == result_scenario.candidate_commit(candidate)
    assert result_scenario.root().status != "succeeded"


def test_real_check_success_is_bound_to_the_published_candidate(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=0)

    results = result_scenario.verification_results(candidate.id)

    assert len(results) == 1
    assert results[0].status is VerificationStatus.PASSED
    assert results[0].candidate_id == candidate.id
    assert results[0].actual_tree == result_scenario.candidate_tree(candidate)


def test_verification_finish_is_idempotent_and_does_not_change_evidence(
    result_scenario,
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    before = candidate.to_mapping()

    replay = result_scenario.finish_verification(candidate.id)

    assert replay == result_scenario.verification_results(candidate.id)
    assert candidate.to_mapping() == before
    assert len(result_scenario.events("VerificationResultFinished")) == 1


def test_missing_required_check_does_not_create_a_successful_root(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(run_checks=False)

    assert result_scenario.verification_results(candidate.id) == ()
    assert result_scenario.root().status != "succeeded"


def test_actual_tree_mismatch_is_rejected_before_result_projection(result_scenario) -> None:
    result_scenario.seed_plan()

    with pytest.raises(ResultError) as error:
        result_scenario.candidate(check_exit_code=0, tamper_actual_tree=True)

    assert error.value.code == "verification_source_changed"


def test_source_modified_during_check_is_blocked(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(
        check_exit_code=0,
        modify_source_during_check=True,
    )

    results = result_scenario.verification_results(candidate.id)

    assert results[0].status is VerificationStatus.BLOCKED
    assert results[0].failure_code == "source_modified_during_check"


def test_finish_replay_preserves_the_original_operation_and_logs(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    operation_before = result_scenario.verification_operation().to_mapping()
    logs_before = result_scenario.verification_log_snapshot()

    result_scenario.finish_verification(candidate.id)

    assert result_scenario.verification_operation().to_mapping() == operation_before
    assert result_scenario.verification_log_snapshot() == logs_before


def test_interrupted_retry_creates_one_new_attempt_and_replays(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    original = result_scenario.verification_results(candidate.id)[0]
    result_scenario.mark_verification_interrupted()

    first = result_scenario.retry_verification(reason="runner stopped")
    second = result_scenario.retry_verification(reason="runner stopped")

    assert first.data == second.data
    assert first.data["attempt"] == 2
    assert result_scenario.verification_results(candidate.id) == (original,)
    assert len(result_scenario.events("VerificationRetryRequested")) == 1
