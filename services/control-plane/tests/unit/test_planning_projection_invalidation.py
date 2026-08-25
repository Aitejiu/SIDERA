from uuid import UUID

import pytest

from sidera_control_plane.application.projection_invalidation import (
    PROJECTION_TYPES,
    projection_targets_for_event,
)
from sidera_control_plane.domain.events import EventData

WORKSPACE_ID = UUID("21000000-0000-0000-0000-000000000001")
REQUEST_ID = UUID("31000000-0000-0000-0000-000000000001")
WORK_CONTEXT_ID = UUID("32000000-0000-0000-0000-000000000001")


def _event(event_type: str, *, aggregate_type: str = "planning_request") -> EventData:
    return EventData(
        id=UUID("41000000-0000-0000-0000-000000000001"),
        workspace_id=WORKSPACE_ID,
        aggregate_type=aggregate_type,
        aggregate_id=REQUEST_ID,
        aggregate_version=3,
        event_type=event_type,
        payload={"planning_request_id": str(REQUEST_ID)},
        actor_type="test",
        actor_id="projection-test",
        causation_id=UUID("51000000-0000-0000-0000-000000000001"),
        correlation_id=UUID("61000000-0000-0000-0000-000000000001"),
    )


@pytest.mark.parametrize(
    ("event_type", "change_type"),
    [
        ("PlanningRequestSubmitted", "planning_request_updated"),
        ("PlanningRequestCancelled", "planning_request_updated"),
        ("PlanningClarificationAnswered", "planning_request_updated"),
        ("PlanningProposalRevisionRequested", "planning_request_updated"),
        ("PlanningProposalRejected", "planning_request_updated"),
        ("PlanningBlockedRunRetried", "planning_request_updated"),
        ("PlanningRunUpdated", "planning_run_updated"),
        ("PlanningClarificationRequired", "planning_clarification_required"),
        ("PlanningProposalReady", "planning_proposal_ready"),
        ("PlanningProposalConflicted", "planning_proposal_conflicted"),
        ("PlanningMaterialized", "planning_materialized"),
    ],
)
def test_planning_events_invalidate_one_request_projection(
    event_type: str, change_type: str
) -> None:
    targets = projection_targets_for_event(_event(event_type))

    assert len(targets) == 1
    assert targets[0].projection_type == "planning_request"
    assert targets[0].resource_id == REQUEST_ID
    assert targets[0].change_type == change_type


def test_planning_run_events_scope_to_request_from_payload() -> None:
    run_id = UUID("71000000-0000-0000-0000-000000000001")
    event = _event("PlanningRunUpdated", aggregate_type="planning_run")
    event = EventData(
        id=event.id,
        workspace_id=event.workspace_id,
        aggregate_type=event.aggregate_type,
        aggregate_id=run_id,
        aggregate_version=event.aggregate_version,
        event_type=event.event_type,
        payload=event.payload,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        causation_id=event.causation_id,
        correlation_id=event.correlation_id,
    )

    target = projection_targets_for_event(event)[0]

    assert target.projection_type == "planning_request"
    assert target.resource_id == REQUEST_ID
    assert target.change_type == "planning_run_updated"


def test_planning_projection_type_is_public() -> None:
    assert "planning_request" in PROJECTION_TYPES


@pytest.mark.parametrize(
    ("event_type", "change_type"),
    [
        ("WorkContextCreated", "work_context_created"),
        ("WorkContextRevised", "work_context_revised"),
    ],
)
def test_work_context_events_invalidate_the_work_context_projection(
    event_type: str, change_type: str
) -> None:
    event = EventData(
        id=UUID("41000000-0000-0000-0000-000000000002"),
        workspace_id=WORKSPACE_ID,
        aggregate_type="work_context",
        aggregate_id=WORK_CONTEXT_ID,
        aggregate_version=2,
        event_type=event_type,
        payload={
            "planning_request_id": str(REQUEST_ID),
            "revision_id": str(UUID("33000000-0000-0000-0000-000000000001")),
            "revision_number": 2,
            "reason": "clarification_accepted",
            "content_hash": "a" * 64,
        },
        actor_type="test",
        actor_id="projection-test",
        causation_id=UUID("51000000-0000-0000-0000-000000000002"),
        correlation_id=UUID("61000000-0000-0000-0000-000000000002"),
    )

    targets = projection_targets_for_event(event)

    assert "work_context" in PROJECTION_TYPES
    assert targets == (
        type(targets[0])(
            projection_type="work_context",
            resource_id=WORK_CONTEXT_ID,
            change_type=change_type,
        ),
    )
