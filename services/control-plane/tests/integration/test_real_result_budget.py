from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest

from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.persistence.real_result_budget import (
    RealResultExecutionBudgetStore,
)

pytestmark = pytest.mark.integration


def test_real_result_budget_is_durable_and_blocks_replay(database) -> None:
    run_id = uuid4()
    store = RealResultExecutionBudgetStore(database)

    first = store.reserve(run_id=run_id, max_executions=2, step_key="source-a")
    second = store.reserve(run_id=run_id, max_executions=2, step_key="source-b")

    assert first.sequence_no == 1
    assert second.sequence_no == 2
    assert store.reserved_count(run_id) == 2

    with pytest.raises(ResultError, match="real_run_reservation_replayed"):
        store.reserve(run_id=run_id, max_executions=2, step_key="source-a")
    with pytest.raises(ResultError, match="real_run_budget_exhausted"):
        store.reserve(run_id=run_id, max_executions=2, step_key="integration")

    reopened = RealResultExecutionBudgetStore(database)
    assert reopened.reserved_count(run_id) == 2
    reopened.complete(first.id, status="succeeded")
    reopened.complete(first.id, status="succeeded")
    reopened.complete(second.id, status="unknown")


def test_real_run_identity_is_durable_and_deadline_does_not_move(database) -> None:
    run_id = uuid4()
    fingerprint = "a" * 64
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    store = RealResultExecutionBudgetStore(database)

    identity = store.begin_run(
        run_id=run_id,
        profile_fingerprint=fingerprint,
        max_executions=2,
        expires_at=deadline,
    )
    reopened = RealResultExecutionBudgetStore(database)
    same = reopened.begin_run(
        run_id=run_id,
        profile_fingerprint=fingerprint,
        max_executions=2,
        expires_at=deadline,
    )

    assert same == identity
    assert reopened.run_identity(run_id) == identity
    with pytest.raises(ResultError, match="real_run_identity_conflict"):
        reopened.begin_run(
            run_id=run_id,
            profile_fingerprint="b" * 64,
            max_executions=2,
            expires_at=deadline,
        )


def test_legacy_budget_cannot_be_silently_upgraded_to_a_real_run(database) -> None:
    run_id = uuid4()
    store = RealResultExecutionBudgetStore(database)
    store.reserve(run_id=run_id, max_executions=1, step_key="legacy")

    with pytest.raises(ResultError, match="real_run_legacy_identity"):
        store.begin_run(
            run_id=run_id,
            profile_fingerprint="a" * 64,
            max_executions=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def test_last_real_budget_slot_has_one_winner_under_concurrency(database) -> None:
    run_id = uuid4()
    barrier = Barrier(2)

    def reserve() -> str:
        store = RealResultExecutionBudgetStore(database)
        barrier.wait(timeout=5)
        try:
            store.reserve(run_id=run_id, max_executions=1, step_key=uuid4().hex)
        except ResultError as exc:
            return exc.code
        return "won"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _value: reserve(), (1, 2)))

    assert sorted(outcomes) == ["real_run_budget_exhausted", "won"]
    assert RealResultExecutionBudgetStore(database).reserved_count(run_id) == 1


def test_execution_binding_requires_real_persisted_ids_and_is_not_reusable(
    database,
) -> None:
    store = RealResultExecutionBudgetStore(database)
    reservation = store.reserve(
        run_id=uuid4(), max_executions=1, step_key="binding"
    )
    with pytest.raises(ResultError, match="real_execution_binding_missing"):
        store.bind_execution(reservation.id, uuid4(), uuid4())

    store.complete(reservation.id, status="unknown")
    with pytest.raises(ResultError, match="real_reservation_status_conflict"):
        store.bind_execution(reservation.id, uuid4(), uuid4())
