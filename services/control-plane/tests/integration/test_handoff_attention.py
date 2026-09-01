from uuid import UUID

import pytest
from sqlalchemy import update
from test_handoff_repository import _seed

from sidera_control_plane.persistence.handoff_query_store import (
    SqlAlchemyHandoffQueryStore,
)
from sidera_control_plane.persistence.models import HandoffTransitionModel

pytestmark = pytest.mark.integration


def test_unknown_handoff_state_requires_attention_on_worker_item(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    with database.session() as session:
        session.execute(
            update(HandoffTransitionModel)
            .where(
                HandoffTransitionModel.id == seed.transition.id,
                HandoffTransitionModel.workspace_id == workspace_id,
            )
            .values(status="blocked_unknown", failure_code="operation_unknown")
        )
        session.commit()

    projection = SqlAlchemyHandoffQueryStore(database).load(
        workspace_id, seed.worker_item_id
    )

    assert projection is not None
    assert projection.attention_required is True
    assert projection.failure_code == "operation_unknown"


def test_known_retryable_handoff_block_does_not_raise_attention(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)
    with database.session() as session:
        session.execute(
            update(HandoffTransitionModel)
            .where(
                HandoffTransitionModel.id == seed.transition.id,
                HandoffTransitionModel.workspace_id == workspace_id,
            )
            .values(status="blocked", failure_code="adapter_rejected")
        )
        session.commit()

    projection = SqlAlchemyHandoffQueryStore(database).load(
        workspace_id, seed.worker_item_id
    )

    assert projection is not None
    assert projection.attention_required is False
    assert projection.available_recovery_strategies
