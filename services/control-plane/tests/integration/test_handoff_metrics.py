from uuid import UUID

import pytest

from sidera_control_plane.persistence.handoff_metrics_store import (
    SqlAlchemyHandoffMetricsStore,
)
from tests.integration.test_handoff_repository import _seed

pytestmark = pytest.mark.integration


def test_handoff_metrics_are_bounded_and_use_only_contract_labels(
    database, workspace_id: UUID
) -> None:
    _seed(database, workspace_id, with_transition=True)

    view = SqlAlchemyHandoffMetricsStore(database).get_metrics(workspace_id)

    names = {sample.name for sample in view.samples}
    assert "sidera_handoff_proposals_total" in names
    assert "sidera_handoff_transitions" in names
    assert all(
        set(dict(sample.labels)) <= {"outcome", "status", "phase", "transfer_mode", "adapter_type", "strategy"}
        for sample in view.samples
    )
    assert all(sample.labels == tuple(sorted(sample.labels)) for sample in view.samples)
    assert all(
        str(workspace_id) not in str(dict(sample.labels))
        for sample in view.samples
    )
