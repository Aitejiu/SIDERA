from __future__ import annotations

from uuid import uuid4

import pytest

from sidera_control_plane.domain.result_operations import ResultError, TaskResultHead
from sidera_control_plane.domain.task import Task


def test_waiting_alone_does_not_authorize_completion(result_scenario) -> None:
    result_scenario.seed_plan()

    with pytest.raises(ResultError, match="acceptance_contract_required"):
        result_scenario.command("CompleteTask", {})

    assert result_scenario.root().status != "succeeded"


def test_complete_task_requires_a_current_candidate(result_scenario) -> None:
    result_scenario.seed_plan()
    head = result_scenario.head()
    payload = {
        "candidate_id": str(uuid4()),
        "candidate_hash": "a" * 64,
        "acceptance_basis_hash": "b" * 64,
        "expected_result_version": head.result_version,
        "criteria": [
            {
                "criterion_key": "login",
                "decision": "exception_accepted",
                "reason": "candidate was not published",
            }
        ],
    }

    with pytest.raises(ResultError, match="acceptance_candidate_required"):
        result_scenario.command("CompleteTask", payload)


def test_agent_cannot_submit_user_acceptance(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=0)

    with pytest.raises(ResultError, match="acceptance_actor_forbidden"):
        result_scenario.command(
            "CompleteTask",
            result_scenario.accept_payload(candidate),
            actor_type="agent",
            actor_id="worker:summary",
        )


def test_acceptance_rejects_a_candidate_from_another_root(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=0)
    other_root = Task.create(
        task_id=uuid4(),
        workspace_id=result_scenario.workspace_id,
        title="Another result root",
        description="Cross-root acceptance boundary",
        acceptance_criteria=("The root remains isolated",),
        priority=50,
        requested_by="user:test",
    )
    source_head = result_scenario.head()
    other_head = TaskResultHead(
        workspace_id=result_scenario.workspace_id,
        root_task_id=other_root.id,
        current_candidate_id=candidate.id,
        current_acceptance_id=None,
        result_version=source_head.result_version,
        active_operation_id=None,
        auto_rounds_used=source_head.auto_rounds_used,
        auto_rounds_limit=source_head.auto_rounds_limit,
        auto_seconds_limit=source_head.auto_seconds_limit,
        auto_started_at=None,
        auto_deadline=None,
    )
    with result_scenario.uow() as uow:
        uow.tasks.add(other_root)
        uow.results.save_head(other_head, expected_version=0)

    with pytest.raises(ResultError, match="acceptance_root_mismatch"):
        result_scenario.command(
            "CompleteTask",
            result_scenario.accept_payload(candidate),
            target_id=other_root.id,
            expected_version=other_root.version,
        )


def test_acceptance_requires_every_configured_criterion(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=0)
    payload = result_scenario.accept_payload(candidate)
    payload["criteria"] = []

    with pytest.raises(ResultError, match="acceptance_criteria_missing"):
        result_scenario.command("CompleteTask", payload)


def test_acceptance_rejects_an_empty_exception_reason(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    payload = result_scenario.accept_payload(candidate)
    criterion = dict(payload["criteria"][0])
    criterion["decision"] = "exception_accepted"
    criterion["reason"] = ""
    payload["criteria"] = [criterion]

    with pytest.raises(ResultError, match="acceptance_exception_reason_required"):
        result_scenario.command("CompleteTask", payload)


def test_failed_verification_cannot_be_marked_as_passed(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    payload = result_scenario.accept_payload(candidate)
    criterion = dict(payload["criteria"][0])
    criterion["decision"] = "accept_passed"
    payload["criteria"] = [criterion]

    with pytest.raises(ResultError, match="acceptance_verification_failed"):
        result_scenario.command("CompleteTask", payload)


def test_acceptance_is_recorded_without_immediate_task_success(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=0)

    result = result_scenario.command(
        "CompleteTask", result_scenario.accept_payload(candidate)
    )

    assert result.data["status"] == "accepted"
    assert result_scenario.head().current_acceptance_id is not None
    assert result_scenario.root().status != "succeeded"
    assert result_scenario.events("TaskCompleted") == ()
