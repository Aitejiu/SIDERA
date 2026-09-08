from __future__ import annotations

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
