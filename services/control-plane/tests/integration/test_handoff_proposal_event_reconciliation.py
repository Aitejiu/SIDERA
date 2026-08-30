from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.projection_invalidation import (
    projection_targets_for_event,
)
from sidera_control_plane.domain.events import EventData

pytestmark = pytest.mark.integration


def test_handoff_proposal_events_invalidate_task_and_worker_item_views() -> None:
    workspace_id = UUID("00000000-0000-0000-0000-000000000001")
    root_task_id = UUID("00000000-0000-0000-0000-000000000002")
    worker_item_id = UUID("00000000-0000-0000-0000-000000000003")
    event = EventData(
        id=uuid4(),
        workspace_id=workspace_id,
        aggregate_type="handoff_proposal",
        aggregate_id=uuid4(),
        aggregate_version=1,
        event_type="HandoffProposalStale",
        payload={
            "root_task_id": str(root_task_id),
            "worker_item_id": str(worker_item_id),
            "status": "stale",
            "stale_reason": "source_execution_changed",
        },
        actor_type="system",
        actor_id="reconciler",
        causation_id=uuid4(),
        correlation_id=uuid4(),
    )

    targets = projection_targets_for_event(event)

    assert {target.projection_type for target in targets} == {
        "task_overview",
        "task_graph",
        "subtask_workspace",
        "worker_item_workspace",
        "worker_item_activity",
    }
    assert all(target.resource_id in {root_task_id, worker_item_id} for target in targets)
