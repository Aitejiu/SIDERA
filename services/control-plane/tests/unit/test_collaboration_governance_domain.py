from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sidera_control_plane.domain.collaboration_governance import (
    CollaborationGovernanceHold,
    CollaborationGovernanceResolution,
    CollaborationReleaseProposal,
    GovernanceHoldStatus,
    GovernanceResolutionAction,
    GovernanceResolutionResult,
    GovernanceTransport,
    ReleaseProposalStatus,
)
from sidera_control_plane.domain.errors import InvalidTransition, ValidationError

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
WORKSPACE_ID = uuid4()
ROOT_TASK_ID = uuid4()
RUN_ID = uuid4()
PLAN_SNAPSHOT_ID = uuid4()
WORKER_ITEM_ID = uuid4()
HOLD_ID = uuid4()
PROPOSAL_ID = uuid4()
DECISION_ID = uuid4()
INTENT_ID = uuid4()
EXECUTION_REQUEST_ID = uuid4()


def active_hold(**overrides: object) -> CollaborationGovernanceHold:
    values: dict[str, object] = {
        "id": HOLD_ID,
        "workspace_id": WORKSPACE_ID,
        "root_task_id": ROOT_TASK_ID,
        "run_id": RUN_ID,
        "plan_snapshot_id": PLAN_SNAPSHOT_ID,
        "plan_generation": 1,
        "plan_content_hash": "a" * 64,
        "worker_item_id": WORKER_ITEM_ID,
        "created_by_actor_id": "developer",
        "created_via": GovernanceTransport.WEB,
        "reason": "Need to inspect the current worker output",
        "now": NOW,
    }
    values.update(overrides)
    return CollaborationGovernanceHold.create(**values)


def pending_proposal(**overrides: object) -> CollaborationReleaseProposal:
    values: dict[str, object] = {
        "id": PROPOSAL_ID,
        "workspace_id": WORKSPACE_ID,
        "hold_id": HOLD_ID,
        "run_id": RUN_ID,
        "projection_snapshot_id": PLAN_SNAPSHOT_ID,
        "projection_hash": "b" * 64,
        "decision_id": DECISION_ID,
        "intent_id": INTENT_ID,
        "execution_request_id": EXECUTION_REQUEST_ID,
        "target_request_version": 1,
        "input_fingerprint_version": 1,
        "input_fingerprint": "c" * 64,
        "reason_code": "release_requested",
        "summary": "Review the requested release",
        "now": NOW,
    }
    values.update(overrides)
    return CollaborationReleaseProposal.create(**values)


def test_approve_once_does_not_resume_hold() -> None:
    hold = active_hold()
    proposal = pending_proposal(hold_id=hold.id)

    approved = proposal.approve(now=NOW)

    assert approved.status is ReleaseProposalStatus.APPROVED
    assert hold.status is GovernanceHoldStatus.ACTIVE


def test_hold_and_proposal_transitions_increment_resource_version_once() -> None:
    hold = active_hold()
    resumed = hold.resume(now=NOW)
    assert resumed.status is GovernanceHoldStatus.RESUMED
    assert resumed.resource_version == hold.resource_version + 1
    with pytest.raises(InvalidTransition):
        resumed.supersede(now=NOW)

    proposal = pending_proposal()
    rejected = proposal.reject(now=NOW)
    assert rejected.status is ReleaseProposalStatus.REJECTED
    assert rejected.resource_version == proposal.resource_version + 1
    with pytest.raises(InvalidTransition):
        rejected.approve(now=NOW)


def test_governance_reason_is_normalized_and_strictly_bounded() -> None:
    assert active_hold(reason=" \n\t ").reason is None
    assert active_hold(reason="line 1\nline 2\tvalue").reason == "line 1\nline 2\tvalue"
    with pytest.raises(ValidationError):
        active_hold(reason="x" * 2001)
    with pytest.raises(ValidationError):
        active_hold(reason="bad\x00reason")
    with pytest.raises(ValidationError):
        active_hold(reason="bad\x01reason")


def test_active_and_pending_records_are_unresolved() -> None:
    with pytest.raises(ValidationError):
        active_hold(resolved_at=NOW)
    with pytest.raises(ValidationError):
        pending_proposal(resolved_at=NOW)


def test_resolution_rejects_stale_result() -> None:
    with pytest.raises(ValidationError):
        CollaborationGovernanceResolution.create(
            id=uuid4(),
            workspace_id=WORKSPACE_ID,
            proposal_id=PROPOSAL_ID,
            action=GovernanceResolutionAction.REJECT,
            result=GovernanceResolutionResult.STALE,
            actor_id="developer",
            transport=GovernanceTransport.WEB,
            reason=None,
            command_id=uuid4(),
            now=NOW,
        )
