from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

from sidera_control_plane.domain.result_operations import (
    ResultError,
    ResultOperationStatus,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("clean_database"),
]


def test_capture_after_database_failure_replays_the_same_git_result(
    result_scenario, monkeypatch
) -> None:
    result_scenario.seed_plan()
    result_scenario.source(
        "login",
        files={"login.py": b"ALLOW_EXPIRED = False\n"},
        commit=True,
        capture=False,
    )
    service = result_scenario.capture_service

    def fail_persistence(*args, **kwargs):
        raise RuntimeError("injected capture database failure")

    monkeypatch.setattr(service, "_persist_snapshot", fail_persistence)
    with pytest.raises(RuntimeError, match="injected capture database failure"):
        service.capture_execution(
            workspace_id=result_scenario.workspace_id,
            execution_id=result_scenario.execution_id,
            operation_id=result_scenario.capture_operation_id,
        )

    result_scenario.reopen_services()
    replay = result_scenario.capture_service.capture_execution(
        workspace_id=result_scenario.workspace_id,
        execution_id=result_scenario.execution_id,
        operation_id=result_scenario.capture_operation_id,
    )

    assert replay.capture_operation_id == result_scenario.capture_operation_id
    assert len(result_scenario.retained_code_results()) == 1
    assert result_scenario.latest_source("login") == replay


def test_check_terminal_observation_replays_after_restart_without_duplicate_evidence(
    result_scenario,
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    before = result_scenario.verification_results(candidate.id)
    event_count = len(result_scenario.events("VerificationResultFinished"))

    result_scenario.reopen_services()
    replay = result_scenario.finish_verification(candidate.id)

    assert replay == before
    assert len(result_scenario.events("VerificationResultFinished")) == event_count
    assert result_scenario.verification_operation().status is ResultOperationStatus.FAILED


def test_rework_reservation_survives_worker_start_failure(
    result_scenario, monkeypatch
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    rework = result_scenario.command(
        "RequestTaskRework",
        result_scenario.rework_payload(candidate),
    )
    request_id = UUID(str(rework.data["request_ids"][0]))

    def fail_start(*args, **kwargs):
        raise RuntimeError("injected rework start failure")

    monkeypatch.setattr(result_scenario._planning, "_start_source", fail_start)
    with pytest.raises(RuntimeError, match="injected rework start failure"):
        result_scenario.rework_source(
            request_id,
            files={"login.py": b"ALLOW_EXPIRED = False\n"},
        )
    assert result_scenario.request(request_id).first_execution_id is not None

    monkeypatch.undo()
    repaired = result_scenario.rework_source(
        request_id,
        files={"login.py": b"ALLOW_EXPIRED = False\n"},
    )

    assert repaired.worker_item_id == result_scenario.worker_item_ids["a"]
    assert len(result_scenario.rework_records()) == 1
    assert len(result_scenario.retained_code_results()) == 4


def test_unknown_acceptance_never_replays_git_side_effect(
    result_scenario,
) -> None:
    result_scenario.seed_plan(apply=True)
    candidate = result_scenario.candidate()
    result_scenario.command("CompleteTask", result_scenario.accept_payload(candidate))
    result_scenario.prepare_unknown_application()
    before_commit = result_scenario.target_commit()
    before_apply_events = len(result_scenario.events("ResultApplyRequested"))

    first = result_scenario.drive()
    result_scenario.reopen_services()
    second = result_scenario.drive()

    assert first.state == "blocked"
    assert second.state == "blocked"
    assert first.failure_code == "result_operation_inspect_required"
    assert second.failure_code == "result_operation_inspect_required"
    assert result_scenario.target_commit() == before_commit
    assert len(result_scenario.events("ResultApplyRequested")) == before_apply_events
    assert result_scenario.head().active_operation_id is not None


def test_git_success_database_failure_is_reconciled_after_service_rebuild(
    result_scenario,
) -> None:
    result_scenario.seed_plan(apply=True)
    candidate = result_scenario.candidate()
    result_scenario.command("CompleteTask", result_scenario.accept_payload(candidate))
    result_scenario.fail_once("after_git_apply_before_db_commit")

    first = result_scenario.drive()

    assert first.state == "pending"
    assert result_scenario.target_commit() == result_scenario.candidate_commit(
        candidate
    )
    assert result_scenario.events("TaskCompleted") == ()

    result_scenario.reopen_services()
    second = result_scenario.drive()

    assert second.state == "completed"
    assert result_scenario.root().status.value == "succeeded"
    assert result_scenario.target_commit() == result_scenario.candidate_commit(candidate)
    assert len(result_scenario.events("TaskCompleted")) == 1


def test_two_real_database_connections_reserve_one_rework_round(
    result_scenario,
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    payload = result_scenario.rework_payload(candidate, mode="automatic")

    def request_rework() -> object:
        try:
            return result_scenario.command(
                "RequestTaskRework",
                payload,
                actor_type="system",
                actor_id="collaboration",
                idempotency_key="e2e-concurrent-rework",
            )
        except ResultError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: request_rework(), range(2)))

    assert len(results) == 2
    assert sum(isinstance(item, ResultError) for item in results) <= 1
    assert result_scenario.head().auto_rounds_used == 1
    assert len(result_scenario.rework_records()) == 1
