from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from sidera_control_plane.application.handoff_coordinator import (
    HandoffCoordinator,
    HandoffRunResult,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffTransitionStatus,
)
from sidera_control_plane.handoff_worker import summary_json
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.integration.test_handoff_repository import _seed

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def test_handoff_worker_runs_coordinator_and_persists_blocked_result(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    coordinator = HandoffCoordinator(
        uow_factory=lambda: SqlAlchemyUnitOfWork(database),
        clock=lambda: NOW,
        owner_id="handoff-worker-integration",
        lease_ttl_seconds=30,
        backoff_max_seconds=60,
    )

    result = coordinator.run_once(limit=1)

    assert result == HandoffRunResult(
        claimed=1,
        progressed=0,
        waiting=0,
        converged=0,
        blocked=1,
        failed=0,
        unknown=0,
    )
    assert json.loads(summary_json(result)) == {
        "blocked": 1,
        "claimed": 1,
        "component": "handoff-worker",
        "converged": 0,
        "failed": 0,
        "progressed": 0,
        "status": "ok",
        "unknown": 0,
        "waiting": 0,
    }

    with SqlAlchemyUnitOfWork(database) as uow:
        transition = uow.handoff_transitions.get(seed.transition.id, workspace_id)
        assert transition is not None
        assert transition.status is HandoffTransitionStatus.BLOCKED
        assert transition.phase is HandoffPhase.CHECKPOINT_SOURCE
        assert transition.lease_owner is None
        assert transition.lease_expires_at is None
        assert transition.next_attempt_at == NOW + timedelta(seconds=1)
