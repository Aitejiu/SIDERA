from __future__ import annotations

import pytest

from sidera_control_plane.application.result_apply import ResultApplyCrash
from sidera_control_plane.domain.result_operations import (
    ResultApplyOperationV1,
    ResultOperationStatus,
)

pytestmark = pytest.mark.integration


def _accept(result_scenario):
    result_scenario.seed_plan(apply=True)
    candidate = result_scenario.candidate()
    result = result_scenario.command(
        "CompleteTask", result_scenario.accept_payload(candidate)
    )
    return candidate, result


def _apply_operation(result_scenario, operation_id=None) -> ResultApplyOperationV1:
    head = result_scenario.head()
    operation_id = operation_id or head.active_operation_id
    if operation_id is None:
        requested = result_scenario.events("ResultApplyRequested")
        assert requested
        operation_id = requested[-1].aggregate_id
    with result_scenario.uow() as uow:
        operation = uow.results.get_operation(
            operation_id, result_scenario.workspace_id
        )
    assert isinstance(operation, ResultApplyOperationV1)
    return operation


def test_apply_intent_rolls_back_before_commit(result_scenario) -> None:
    result_scenario.seed_plan(apply=True)
    candidate = result_scenario.candidate()
    result_scenario.fail_once("before_intent_commit")

    with pytest.raises(ResultApplyCrash, match="before_intent_commit"):
        result_scenario.command(
            "CompleteTask", result_scenario.accept_payload(candidate)
        )

    assert result_scenario.head().current_acceptance_id is None
    assert result_scenario.head().active_operation_id is None
    assert result_scenario.events("TaskAcceptanceRecorded") == ()
    assert result_scenario.events("ResultApplyRequested") == ()


def test_acceptance_persists_apply_intent_before_git(result_scenario) -> None:
    candidate, result = _accept(result_scenario)

    assert result.data["status"] == "accepted"
    assert result.data["apply_operation_id"] == str(
        _apply_operation(result_scenario).id
    )
    operation = _apply_operation(result_scenario)
    assert operation.candidate_id == candidate.id
    assert operation.status is ResultOperationStatus.PENDING
    assert operation.expected_old_commit == result_scenario.base_commit
    assert result_scenario.target_commit() == result_scenario.base_commit
    assert result_scenario.events("TaskCompleted") == ()


def test_result_worker_applies_and_finalizes_once(result_scenario) -> None:
    candidate, _result = _accept(result_scenario)

    outcome = result_scenario.drive()

    assert outcome.state == "completed"
    assert result_scenario.target_commit() == result_scenario.candidate_commit(
        candidate
    )
    assert result_scenario.root().status.value == "succeeded"
    assert len(result_scenario.events("TaskCompleted")) == 1
    assert _apply_operation(result_scenario).status is ResultOperationStatus.SUCCEEDED
    assert result_scenario.head().active_operation_id is None


def test_git_apply_crash_before_external_write_is_recovered(result_scenario) -> None:
    candidate, _result = _accept(result_scenario)
    result_scenario.fail_once("before_git_apply")

    first = result_scenario.drive()

    assert first.state == "pending"
    assert result_scenario.target_commit() == result_scenario.base_commit
    assert _apply_operation(result_scenario).status is ResultOperationStatus.RUNNING

    second = result_scenario.drive()

    assert second.state == "completed"
    assert result_scenario.target_commit() == result_scenario.candidate_commit(
        candidate
    )
    assert len(result_scenario.events("TaskCompleted")) == 1


def test_git_success_database_failure_is_reconciled_once(result_scenario) -> None:
    candidate, _result = _accept(result_scenario)
    result_scenario.fail_once("after_git_apply_before_db_commit")

    first = result_scenario.drive()
    assert first.state == "pending"
    assert result_scenario.target_commit() == result_scenario.candidate_commit(
        candidate
    )
    assert len(result_scenario.events("TaskCompleted")) == 0

    result_scenario.reopen_services()
    second = result_scenario.drive()

    assert second.state == "completed"
    assert result_scenario.target_commit() == result_scenario.candidate_commit(
        candidate
    )
    assert len(result_scenario.events("TaskCompleted")) == 1
    assert _apply_operation(result_scenario).status is ResultOperationStatus.SUCCEEDED


def test_old_worker_cannot_continue_after_new_lease(result_scenario) -> None:
    candidate, _result = _accept(result_scenario)
    old_claim = result_scenario.claim_apply_operation("result-worker:old")
    assert old_claim is not None

    result_scenario.advance_time(301)
    new_claim = result_scenario.claim_apply_operation("result-worker:new")
    assert new_claim is not None
    assert new_claim.version > old_claim.version

    stale = result_scenario.reconcile_apply_operation(old_claim.id, "result-worker:old")

    assert stale.state == "pending"
    assert stale.failure_code == "result_operation_in_progress"
    assert result_scenario.target_commit() == result_scenario.base_commit

    completed = result_scenario.reconcile_apply_operation(
        new_claim.id, "result-worker:new"
    )
    assert completed.state == "completed"
    assert result_scenario.target_commit() == result_scenario.candidate_commit(
        candidate
    )
    assert len(result_scenario.events("TaskCompleted")) == 1


def test_duplicate_workers_claim_an_apply_operation_once(result_scenario) -> None:
    candidate, _result = _accept(result_scenario)
    first = result_scenario.claim_apply_operation("result-worker:first")
    assert first is not None

    second = result_scenario.claim_apply_operation("result-worker:second")

    assert second is None
    completed = result_scenario.reconcile_apply_operation(
        first.id, "result-worker:first"
    )
    assert completed.state == "completed"
    assert result_scenario.target_commit() == result_scenario.candidate_commit(
        candidate
    )
    assert len(result_scenario.events("TaskCompleted")) == 1


def test_unknown_apply_never_replays_external_operation(result_scenario) -> None:
    _candidate, _result = _accept(result_scenario)
    result_scenario.prepare_unknown_application()

    before = result_scenario.target_commit()
    outcome = result_scenario.drive()

    assert outcome.state == "blocked"
    assert outcome.failure_code == "result_operation_inspect_required"
    assert result_scenario.target_commit() == before
    assert _apply_operation(result_scenario).status is ResultOperationStatus.UNKNOWN
    assert result_scenario.events("TaskCompleted") == ()
