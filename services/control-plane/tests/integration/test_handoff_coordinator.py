from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.handoffs import (
    HandoffTransitionStatus,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.integration.test_handoff_repository import _seed

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def test_claim_filters_terminal_recovery_states(database, workspace_id: UUID) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    with SqlAlchemyUnitOfWork(database) as uow:
        transition = uow.handoff_transitions.get(seed.transition.id, workspace_id)
        assert transition is not None
        transition.block(
            failure_code="unknown",
            now=NOW,
            status=HandoffTransitionStatus.BLOCKED_UNKNOWN,
            blocked_operation_id=uuid4(),
        )
        uow.handoff_transitions.save(transition, previous_version=1)

    with SqlAlchemyUnitOfWork(database) as uow:
        claimed = uow.handoff_transitions.claim_next(
            owner_id="handoff-coordinator",
            now=NOW,
            limit=10,
            statuses=(
                HandoffTransitionStatus.PENDING,
                HandoffTransitionStatus.RUNNING,
                HandoffTransitionStatus.BLOCKED,
            ),
            lease_ttl_seconds=30,
        )
        assert claimed == ()
