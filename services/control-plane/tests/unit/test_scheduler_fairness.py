from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError

from sidera_control_plane.application.scheduler import (
    Scheduler,
    _is_active_lease_conflict,
)
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy


class FakeUniqueViolation(UniqueViolation):
    def __init__(self, constraint_name: str) -> None:
        super().__init__("duplicate key")
        self._constraint_name = constraint_name

    @property
    def diag(self) -> Any:
        return SimpleNamespace(constraint_name=self._constraint_name)


def make_scheduler() -> Scheduler:
    return Scheduler(
        uow_factory=cast(Any, lambda: SimpleNamespace()),
        policy=LowestCostWorkerPolicy(),
        clock=datetime.now,
    )


def test_run_once_continues_after_deferred_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = make_scheduler()
    first_id = uuid4()
    second_id = uuid4()
    excluded_calls: list[frozenset[object]] = []

    def assign_one(*, excluded_ids: frozenset[object]) -> tuple[int, int, int, object]:
        excluded_calls.append(excluded_ids)
        if len(excluded_calls) == 1:
            return 1, 0, 1, first_id
        return 1, 1, 0, second_id

    monkeypatch.setattr(scheduler, "_assign_one", assign_one)

    result = scheduler.run_once(limit=2)

    assert result.examined == 2
    assert result.assigned == 1
    assert result.deferred == 1
    assert excluded_calls == [frozenset(), frozenset({first_id})]


@pytest.mark.parametrize(
    ("constraint_name", "expected"),
    [
        ("uq_assignment_leases_active_execution", True),
        ("uq_assignment_leases_execution_token", False),
    ],
)
def test_only_active_execution_lease_conflict_is_retryable(
    constraint_name: str,
    expected: bool,
) -> None:
    error = IntegrityError(
        "duplicate key",
        None,
        FakeUniqueViolation(constraint_name),
    )

    assert _is_active_lease_conflict(error) is expected


def test_non_unique_integrity_error_is_not_retryable() -> None:
    error = IntegrityError("constraint failed", None, Exception("bad data"))

    assert _is_active_lease_conflict(error) is False
