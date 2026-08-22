from uuid import UUID

import pytest

from sidera_control_plane.application.projection_invalidation import (
    PROJECTION_TYPES,
    projection_targets_for_event,
)
from sidera_control_plane.domain.events import EventData

WORKSPACE_ID = UUID("21000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("31000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("41000000-0000-0000-0000-000000000001")


def _event(event_type: str) -> EventData:
    return EventData(
        id=UUID("51000000-0000-0000-0000-000000000001"),
        workspace_id=WORKSPACE_ID,
        aggregate_type="execution",
        aggregate_id=UUID("61000000-0000-0000-0000-000000000001"),
        aggregate_version=4,
        event_type=event_type,
        payload={
            "root_task_id": str(ROOT_TASK_ID),
            "worker_item_id": str(WORKER_ITEM_ID),
        },
        actor_type="test",
        actor_id="projection-test",
        causation_id=UUID("71000000-0000-0000-0000-000000000001"),
        correlation_id=UUID("81000000-0000-0000-0000-000000000001"),
    )


@pytest.mark.parametrize(
    ("event_type", "expected_projection_types"),
    [
        (
            "worker_item.created",
            {
                "task_overview",
                "task_graph",
                "subtask_workspace",
                "worker_item_workspace",
            },
        ),
        (
            "worker_item.placement_materialized",
            {"task_graph", "subtask_workspace", "worker_item_workspace"},
        ),
        (
            "ExecutionStarted",
            {
                "task_graph",
                "subtask_workspace",
                "worker_item_workspace",
                "worker_item_activity",
            },
        ),
        (
            "ExecutionInputAccepted",
            {"worker_item_workspace", "worker_item_activity"},
        ),
        (
            "ArtifactRecorded",
            {"subtask_workspace", "worker_item_workspace", "worker_item_activity"},
        ),
        (
            "EvidenceRecorded",
            {
                "task_overview",
                "task_graph",
                "subtask_workspace",
                "worker_item_workspace",
                "worker_item_activity",
            },
        ),
    ],
)
def test_projection_change_targets_only_affected_resources(
    event_type: str, expected_projection_types: set[str]
) -> None:
    targets = projection_targets_for_event(_event(event_type))

    assert {target.projection_type for target in targets} == expected_projection_types
    assert all(target.projection_type in PROJECTION_TYPES for target in targets)
    assert {
        target.resource_id
        for target in targets
        if target.projection_type
        in {"worker_item_workspace", "worker_item_activity"}
    } == {WORKER_ITEM_ID}
    subtask_resources = {
        target.resource_id
        for target in targets
        if target.projection_type == "subtask_workspace"
    }
    if "subtask_workspace" in expected_projection_types:
        assert subtask_resources == {ROOT_TASK_ID}
    else:
        assert subtask_resources == set()


def test_projection_targets_use_workspace_scope_when_identity_is_unavailable() -> None:
    event = _event("ExecutionStarted")
    event = EventData(
        id=event.id,
        workspace_id=event.workspace_id,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        aggregate_version=event.aggregate_version,
        event_type=event.event_type,
        payload={},
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        causation_id=event.causation_id,
        correlation_id=event.correlation_id,
    )

    targets = projection_targets_for_event(event)

    assert targets
    assert all(target.resource_id is None for target in targets)
