from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from sidera_control_plane.domain.result_operations import ResultError

pytestmark = pytest.mark.integration


def test_one_automatic_round_is_reserved_once(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    payload = result_scenario.rework_payload(candidate, mode="automatic")

    first = result_scenario.command(
        "RequestTaskRework",
        payload,
        actor_type="system",
        actor_id="collaboration",
        idempotency_key="rework-1",
    )
    replay = result_scenario.command(
        "RequestTaskRework",
        payload,
        actor_type="system",
        actor_id="collaboration",
        idempotency_key="rework-1",
    )

    assert first.command_id == replay.command_id
    assert result_scenario.head().auto_rounds_used == 1
    assert result_scenario.head().auto_deadline is not None
    records = result_scenario.rework_records()
    assert len(records) == 1
    assert records[0].round_number == 1
    assert records[0].status.value == "requested"


def test_manual_budget_authorization_only_appends_and_survives_restart(
    result_scenario,
) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    result_scenario.command(
        "RequestTaskRework",
        result_scenario.rework_payload(candidate, mode="automatic"),
        actor_type="system",
        actor_id="collaboration",
        idempotency_key="rework-round-1",
    )
    started = result_scenario.head()
    assert started.auto_deadline is not None

    result_scenario.command(
        "AuthorizeAdditionalRework",
        {
            "additional_rounds": 1,
            "additional_seconds": 600,
            "reason": "operator approved one more bounded attempt",
        },
        idempotency_key="rework-budget-1",
    )
    result_scenario.reopen_services()
    expanded = result_scenario.head()

    assert expanded.auto_rounds_used == 1
    assert expanded.auto_rounds_limit == 3
    assert expanded.auto_seconds_limit == 2400
    assert expanded.auto_started_at == started.auto_started_at
    assert expanded.auto_deadline == started.auto_deadline + timedelta(seconds=600)


def test_fake_system_actor_cannot_request_human_rework(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)

    with pytest.raises(ResultError, match="rework_user_actor_required"):
        result_scenario.command(
            "RequestTaskRework",
            result_scenario.rework_payload(candidate, mode="user"),
            actor_type="system",
            actor_id="forged-human",
            idempotency_key="rework-forged-human",
        )

    assert result_scenario.head().auto_rounds_used == 0
    assert result_scenario.rework_records() == ()


def test_unknown_owner_is_saved_as_attention_without_guessing(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    payload = result_scenario.rework_payload(
        candidate,
        mode="user",
        target_worker_item_keys=(),
        reason="the failed responsibility is not identified yet",
    )

    result = result_scenario.command(
        "RequestTaskRework",
        payload,
        idempotency_key="rework-owner-required",
    )

    assert result.data["attention_required"] is True
    assert result.data["status"] == "blocked"
    assert result_scenario.head().auto_rounds_used == 0
    records = result_scenario.rework_records()
    assert len(records) == 1
    assert records[0].status.value == "blocked"
    assert records[0].target_worker_item_ids == ()
    assert records[0].reason == "rework_owner_required"


def test_worker_outside_confirmed_plan_is_rejected(result_scenario) -> None:
    result_scenario.seed_plan()
    candidate = result_scenario.candidate(check_exit_code=7)
    payload = result_scenario.rework_payload(candidate)
    payload["target_worker_item_ids"] = [str(uuid4())]

    with pytest.raises(ResultError, match="rework_worker_not_found"):
        result_scenario.command(
            "RequestTaskRework",
            payload,
            idempotency_key="rework-unknown-worker",
        )

    assert result_scenario.rework_records() == ()
