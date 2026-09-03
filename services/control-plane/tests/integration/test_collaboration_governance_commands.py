from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.application.collaboration_governance_commands import (
    CollaborationGovernanceCommandHandler,
)
from sidera_control_plane.application.collaboration_runtime_invoker import (
    RuntimeInvocationResult,
)
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.domain.collaboration_governance import (
    CollaborationGovernanceHold,
    GovernanceHoldStatus,
    GovernanceTransport,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.persistence.models import (
    CollaborationGovernanceHoldModel,
    CollaborationGovernanceResolutionModel,
    CollaborationReleaseProposalModel,
    EventModel,
    ProjectionChangeModel,
    RuntimeIntentApplicationModel,
    WorkerItemPlacementModel,
)

from .test_collaboration_decision_application import _service
from .test_collaboration_evaluation_recovery import _invoker, _prepare

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
LOCAL_ACTOR_TYPE = "local"
LOCAL_ACTOR_ID = "aitejiu"


def _command_service(uow_factory):
    handler = CollaborationGovernanceCommandHandler(
        clock=lambda: NOW,
        local_actor_type=LOCAL_ACTOR_TYPE,
        local_actor_id=LOCAL_ACTOR_ID,
    )
    return CommandService(uow_factory, (handler,))


def _envelope(
    command_type: str,
    *,
    target_type: str,
    target_id: UUID,
    expected_version: int,
    payload: dict[str, object],
) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=UUID("00000000-0000-0000-0000-000000000001"),
        command_type=command_type,
        target_type=target_type,
        actor_type=LOCAL_ACTOR_TYPE,
        actor_id=LOCAL_ACTOR_ID,
        target_id=target_id,
        payload=payload,
        expected_version=expected_version,
        idempotency_key=str(uuid4()),
        correlation_id=uuid4(),
    )


def _add_current_placement(
    database, workspace_id: UUID, ids: dict[str, UUID], snapshot
) -> None:
    with database.session() as session:
        session.add(
            WorkerItemPlacementModel(
                plan_snapshot_id=snapshot.id,
                plan_generation=snapshot.generation,
                workspace_id=workspace_id,
                root_task_id=ids["root_task"],
                worker_item_id=ids["worker_item"],
                worker_item_key="backend",
                subtask_key="backend",
                subtask_task_id=ids["subtask"],
                role="implementer",
                capability_requirements=["python"],
                worker_policy_ref=None,
                configuration_ref=None,
                created_at=NOW,
            )
        )
        session.commit()


def _prepare_held_proposal(database, workspace_id: UUID, uow_factory):
    ids, snapshot, _reconciler, prepared = _prepare(database, workspace_id, uow_factory)
    _add_current_placement(database, workspace_id, ids, snapshot)
    hold = CollaborationGovernanceHold.create(
        id=uuid4(),
        workspace_id=workspace_id,
        root_task_id=prepared.run.root_task_id,
        run_id=prepared.run.id,
        plan_snapshot_id=prepared.run.plan_snapshot_id,
        plan_generation=prepared.run.plan_generation,
        plan_content_hash=prepared.run.plan_content_hash,
        worker_item_id=ids["worker_item"],
        created_by_actor_id=LOCAL_ACTOR_ID,
        created_via=GovernanceTransport.WEB,
        reason="review before release",
        now=NOW,
    )
    with uow_factory() as uow:
        uow.collaboration_governance.add_hold(hold)

    invocation = _invoker(ReferenceCollaborationRuntime(clock=lambda: NOW)).invoke(
        prepared, now=NOW
    )
    assert isinstance(invocation, RuntimeInvocationResult)
    applied = _service(uow_factory).apply(prepared, invocation, now=NOW)
    assert len(applied.proposals) == 1
    proposal = applied.proposals[0]
    return ids, snapshot, prepared, hold, proposal


def test_pause_is_idempotent_and_preserves_running_worker_state(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, snapshot, _reconciler, prepared = _prepare(database, workspace_id, uow_factory)
    _add_current_placement(database, workspace_id, ids, snapshot)
    service = _command_service(uow_factory)
    command = _envelope(
        "PauseCollaborationRelease",
        target_type="worker_item",
        target_id=ids["worker_item"],
        expected_version=1,
        payload={"transport": "web", "target_plan_generation": snapshot.generation},
    )

    first = service.handle(command)
    replay = service.handle(command)

    assert replay == first
    with database.session() as session:
        holds = session.scalars(
            select(CollaborationGovernanceHoldModel).where(
                CollaborationGovernanceHoldModel.workspace_id == workspace_id,
                CollaborationGovernanceHoldModel.worker_item_id == ids["worker_item"],
            )
        ).all()
        events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.event_type == "CollaborationReleasePaused",
            )
        ).all()
    assert len(holds) == 1
    assert holds[0].status == GovernanceHoldStatus.ACTIVE.value
    assert len(events) == 1
    assert first.data["governance_mode"] == "paused"
    assert prepared.run.id is not None


def test_reject_is_atomic_and_does_not_publish_runtime_invalidation(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, prepared, hold, proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )
    service = _command_service(uow_factory)
    command = _envelope(
        "RejectCollaborationReleaseProposal",
        target_type="collaboration_release_proposal",
        target_id=proposal.id,
        expected_version=proposal.resource_version,
        payload={"transport": "web", "reason": "needs evidence"},
    )

    result = service.handle(command)

    with database.session() as session:
        stored_proposal = session.scalar(
            select(CollaborationReleaseProposalModel).where(
                CollaborationReleaseProposalModel.workspace_id == workspace_id,
                CollaborationReleaseProposalModel.id == proposal.id,
            )
        )
        application = session.scalar(
            select(RuntimeIntentApplicationModel).where(
                RuntimeIntentApplicationModel.workspace_id == workspace_id,
                RuntimeIntentApplicationModel.intent_id == proposal.intent_id,
            )
        )
        resolution = session.scalar(
            select(CollaborationGovernanceResolutionModel).where(
                CollaborationGovernanceResolutionModel.workspace_id == workspace_id,
                CollaborationGovernanceResolutionModel.proposal_id == proposal.id,
            )
        )
        runtime_changes = session.scalars(
            select(ProjectionChangeModel).where(
                ProjectionChangeModel.workspace_id == workspace_id,
                ProjectionChangeModel.projection_type == "collaboration_runtime",
                ProjectionChangeModel.change_type == "collaboration_governance_resumed",
            )
        ).all()
        rejected_events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.event_type == "CollaborationReleaseProposalRejected",
            )
        ).all()
    assert stored_proposal is not None and stored_proposal.status == "rejected"
    assert application is not None and application.status == "rejected"
    assert resolution is not None and resolution.reason == "needs evidence"
    assert runtime_changes == []
    assert len(rejected_events) == 1
    assert result.data["hold_id"] == str(hold.id)
    assert prepared.run.id is not None
    assert ids["request"] is not None


def test_resume_stales_pending_proposal_and_wakes_runtime_once(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, _snapshot, _prepared, hold, proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )
    service = _command_service(uow_factory)
    command = _envelope(
        "ResumeAutomaticCollaboration",
        target_type="collaboration_governance_hold",
        target_id=hold.id,
        expected_version=hold.resource_version,
        payload={"transport": "web"},
    )

    result = service.handle(command)

    with database.session() as session:
        stored_hold = session.scalar(
            select(CollaborationGovernanceHoldModel).where(
                CollaborationGovernanceHoldModel.workspace_id == workspace_id,
                CollaborationGovernanceHoldModel.id == hold.id,
            )
        )
        stored_proposal = session.scalar(
            select(CollaborationReleaseProposalModel).where(
                CollaborationReleaseProposalModel.workspace_id == workspace_id,
                CollaborationReleaseProposalModel.id == proposal.id,
            )
        )
        application = session.scalar(
            select(RuntimeIntentApplicationModel).where(
                RuntimeIntentApplicationModel.workspace_id == workspace_id,
                RuntimeIntentApplicationModel.intent_id == proposal.intent_id,
            )
        )
        runtime_changes = session.scalars(
            select(ProjectionChangeModel).where(
                ProjectionChangeModel.workspace_id == workspace_id,
                ProjectionChangeModel.projection_type == "collaboration_runtime",
                ProjectionChangeModel.change_type == "collaboration_governance_resumed",
            )
        ).all()
        events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.correlation_id == command.correlation_id,
            )
        ).all()
    assert stored_hold is not None and stored_hold.status == "resumed"
    assert stored_proposal is not None and stored_proposal.status == "stale"
    assert application is not None and application.status == "stale"
    assert len(runtime_changes) == 1
    assert {event.event_type for event in events} == {
        "CollaborationAutomaticReleaseResumed",
        "CollaborationReleaseProposalStale",
    }
    assert result.data["staled_proposal_count"] == 1
