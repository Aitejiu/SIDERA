from uuid import UUID

import pytest
from test_handoff_repository import _seed

from sidera_control_plane.persistence.handoff_query_store import (
    SqlAlchemyHandoffQueryStore,
)

pytestmark = pytest.mark.integration


def test_handoff_query_store_returns_bounded_active_projection(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)

    projection = SqlAlchemyHandoffQueryStore(database).load(
        workspace_id, seed.worker_item_id
    )

    assert projection is not None
    assert projection.transition_id == seed.transition.id
    assert projection.proposal_id == seed.proposal.id
    assert projection.source_execution_id == seed.source_execution_id
    assert projection.target_execution_id is None
    assert projection.source_trace_descriptor is not None
    assert projection.target_trace_descriptor is None
    assert str(seed.transition.id) not in projection.source_trace_descriptor.route
    assert projection.source_trace_descriptor.session_id == seed.source_session_id


def test_handoff_query_store_is_workspace_scoped(
    database, workspace_id: UUID
) -> None:
    seed = _seed(database, workspace_id, with_transition=True)

    assert SqlAlchemyHandoffQueryStore(database).load(
        UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"), seed.worker_item_id
    ) is None
