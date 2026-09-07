from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sidera_control_plane.application.result_ports import ResultError
from sidera_control_plane.domain.result_operations import (
    ResultApplyOperationV1,
    ResultOperationStatus,
    extend_rework_budget,
    reserve_rework_round,
    transition_operation,
)

from .result_integration_fixtures import apply_operation_mapping, head_fixture


def test_rework_budget_survives_a_new_execution() -> None:
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    head = head_fixture(auto_rounds_used=2, auto_rounds_limit=2)

    with pytest.raises(ResultError, match="rework_budget_exhausted"):
        reserve_rework_round(head, now)


def test_first_round_sets_one_deadline_and_later_round_keeps_it() -> None:
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    first = reserve_rework_round(head_fixture(), now)
    second = reserve_rework_round(first, now + timedelta(seconds=30))

    assert first.auto_started_at == now
    assert first.auto_deadline == now + timedelta(seconds=1800)
    assert second.auto_started_at == now
    assert second.auto_deadline == first.auto_deadline
    assert second.auto_rounds_used == 2


def test_additional_budget_is_incremental_and_does_not_reset_started_time() -> None:
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    started = reserve_rework_round(head_fixture(), now)

    expanded = extend_rework_budget(
        started, additional_rounds=1, additional_seconds=600
    )

    assert expanded.auto_rounds_limit == 3
    assert expanded.auto_seconds_limit == 2400
    assert expanded.auto_started_at == now
    assert expanded.auto_deadline == now + timedelta(seconds=2400)
    assert expanded.auto_rounds_used == 1


def test_unknown_and_succeeded_operations_cannot_be_retried_by_transition() -> None:
    operation = ResultApplyOperationV1.from_mapping(apply_operation_mapping())

    running = transition_operation(operation, ResultOperationStatus.RUNNING)
    succeeded = transition_operation(running, ResultOperationStatus.SUCCEEDED)
    with pytest.raises(ResultError, match="terminal"):
        transition_operation(succeeded, ResultOperationStatus.RUNNING)

    unknown = transition_operation(running, ResultOperationStatus.UNKNOWN)
    with pytest.raises(ResultError, match="inspect"):
        transition_operation(unknown, ResultOperationStatus.RUNNING)


def test_apply_operation_round_trip_and_lease_identity() -> None:
    operation = ResultApplyOperationV1.from_mapping(
        apply_operation_mapping(
            lease_owner="result-worker-1",
            lease_token="lease-token-1",
            lease_expiry="2026-09-07T12:10:00+00:00",
        )
    )

    assert operation.to_mapping() == apply_operation_mapping(
        lease_owner="result-worker-1",
        lease_token="lease-token-1",
        lease_expiry="2026-09-07T12:10:00+00:00",
    )
