from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update

from sidera_control_plane.application.collaboration_governance_commands import (
    CollaborationGovernanceCommandHandler,
)
from sidera_control_plane.application.collaboration_governance_release import (
    CollaborationGovernanceReleaseApplier,
)
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.domain.collaboration_governance import (
    GovernanceHoldStatus,
    GovernanceResolutionAction,
    GovernanceResolutionResult,
    GovernanceTransport,
    ReleaseProposalStatus,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.models import (
    CollaborationGovernanceHoldModel,
    CollaborationGovernanceResolutionModel,
    CollaborationReleaseProposalModel,
    EventModel,
    ExecutionRequestModel,
    OutboxMessageModel,
    ProjectionChangeModel,
    RootTaskPlanHeadModel,
    RuntimeIntentApplicationModel,
    RuntimeReleaseModel,
)

from .test_collaboration_governance_commands import _prepare_held_proposal

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
LOCAL_ACTOR_TYPE = "local"
LOCAL_ACTOR_ID = "aitejiu"


def _command_service(uow_factory: Any) -> CommandService:
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda uow: SqlAlchemyCollaborationFactStore(
            session=uow.session
        )
    )
    handler = CollaborationGovernanceCommandHandler(
        clock=lambda: NOW,
        local_actor_type=LOCAL_ACTOR_TYPE,
        local_actor_id=LOCAL_ACTOR_ID,
        approver=applier,
    )
    return CommandService(uow_factory, (handler,))


def _approve_command(
    proposal_id: UUID, *, workspace_id: UUID, version: int
) -> CommandEnvelope:
    from uuid import uuid4

    return CommandEnvelope(
        id=uuid4(),
        workspace_id=workspace_id,
        command_type="ApproveCollaborationReleaseProposal",
        target_type="collaboration_release_proposal",
        actor_type=LOCAL_ACTOR_TYPE,
        actor_id=LOCAL_ACTOR_ID,
        target_id=proposal_id,
        payload={"transport": GovernanceTransport.WEB.value, "reason": "reviewed"},
        expected_version=version,
        idempotency_key=f"approve:{proposal_id}",
        correlation_id=uuid4(),
    )


def test_approval_applies_original_lineage_and_activation_gate_atomically(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, snapshot, prepared, hold, proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )
    command = _approve_command(
        proposal.id,
        workspace_id=workspace_id,
        version=proposal.resource_version,
    )

    result = _command_service(uow_factory).handle(command)

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
        resolution = session.scalar(
            select(CollaborationGovernanceResolutionModel).where(
                CollaborationGovernanceResolutionModel.workspace_id == workspace_id,
                CollaborationGovernanceResolutionModel.proposal_id == proposal.id,
            )
        )
        application = session.scalar(
            select(RuntimeIntentApplicationModel).where(
                RuntimeIntentApplicationModel.workspace_id == workspace_id,
                RuntimeIntentApplicationModel.intent_id == proposal.intent_id,
            )
        )
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id == ids["request"],
            )
        ).all()
        request = session.scalar(
            select(ExecutionRequestModel).where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.id == ids["request"],
            )
        )
        approved_events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.event_type == "CollaborationReleaseProposalApproved",
            )
        ).all()
        approved_outbox = session.scalars(
            select(OutboxMessageModel).where(
                OutboxMessageModel.topic
                == "collaboration_release_proposal.CollaborationReleaseProposalApproved",
            )
        ).all()

    assert result.data["result"] == GovernanceResolutionResult.APPLIED.value
    assert stored_hold is not None
    assert stored_hold.status == GovernanceHoldStatus.ACTIVE.value
    assert stored_proposal is not None
    assert stored_proposal.status == ReleaseProposalStatus.APPROVED.value
    assert resolution is not None
    assert resolution.action == GovernanceResolutionAction.APPROVE.value
    assert resolution.result == GovernanceResolutionResult.APPLIED.value
    assert application is not None
    assert application.status == "applied"
    assert application.release_id == releases[0].id
    assert len(releases) == 1
    assert releases[0].run_id == prepared.run.id
    assert releases[0].plan_snapshot_id == snapshot.id
    assert releases[0].plan_generation == snapshot.generation
    assert releases[0].decision_id == proposal.decision_id
    assert releases[0].intent_id == proposal.intent_id
    assert releases[0].request_version_at_release == proposal.target_request_version
    assert request is not None
    assert request.status == "ready"
    assert request.gate_reason == "ready"
    assert len(approved_events) == 1
    assert len(approved_outbox) == 1


def test_changed_target_fingerprint_is_a_stale_approval_and_replays_exactly(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, _prepared, hold, proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )
    with database.session() as session:
        session.execute(
            update(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.id == ids["request"],
            )
            .values(expected_evidence_refs=["acceptance:changed"])
        )
        session.commit()

    command = _approve_command(
        proposal.id,
        workspace_id=workspace_id,
        version=proposal.resource_version,
    )
    service = _command_service(uow_factory)
    result = service.handle(command)
    replay = service.handle(command)

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
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id == ids["request"],
            )
        ).all()

    assert result.data["result"] == GovernanceResolutionResult.STALE.value
    assert replay == result
    assert stored_proposal is not None
    assert stored_proposal.status == ReleaseProposalStatus.STALE.value
    assert application is not None
    assert application.status == "stale"
    assert releases == []
    assert hold.id is not None


def test_changed_request_version_stales_and_publishes_one_runtime_wakeup(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, _prepared, hold, proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )
    with database.session() as session:
        session.execute(
            update(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.id == ids["request"],
            )
            .values(version=2)
        )
        session.commit()

    result = _command_service(uow_factory).handle(
        _approve_command(
            proposal.id,
            workspace_id=workspace_id,
            version=proposal.resource_version,
        )
    )

    with database.session() as session:
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id == ids["request"],
            )
        ).all()
        runtime_changes = session.scalars(
            select(ProjectionChangeModel).where(
                ProjectionChangeModel.workspace_id == workspace_id,
                ProjectionChangeModel.projection_type == "collaboration_runtime",
                ProjectionChangeModel.change_type
                == "collaboration_governance_approval_stale",
            )
        ).all()
        stored_hold = session.scalar(
            select(CollaborationGovernanceHoldModel).where(
                CollaborationGovernanceHoldModel.workspace_id == workspace_id,
                CollaborationGovernanceHoldModel.id == hold.id,
            )
        )

    assert result.data["result"] == GovernanceResolutionResult.STALE.value
    assert result.data["stale_reason"] == "projection_stale"
    assert releases == []
    assert stored_hold is not None
    assert stored_hold.status == GovernanceHoldStatus.ACTIVE.value
    assert len(runtime_changes) == 1


def test_changed_plan_generation_stales_without_runtime_wakeup(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, snapshot, _prepared, hold, proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )
    with database.session() as session:
        session.execute(
            update(RootTaskPlanHeadModel)
            .where(
                RootTaskPlanHeadModel.workspace_id == workspace_id,
                RootTaskPlanHeadModel.root_task_id == snapshot.root_task_id,
            )
            .values(active_generation=snapshot.generation + 1)
        )
        session.commit()

    result = _command_service(uow_factory).handle(
        _approve_command(
            proposal.id,
            workspace_id=workspace_id,
            version=proposal.resource_version,
        )
    )

    with database.session() as session:
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id
                == proposal.execution_request_id,
            )
        ).all()
        runtime_changes = session.scalars(
            select(ProjectionChangeModel).where(
                ProjectionChangeModel.workspace_id == workspace_id,
                ProjectionChangeModel.projection_type == "collaboration_runtime",
                ProjectionChangeModel.change_type
                == "collaboration_governance_approval_stale",
            )
        ).all()
        stored_hold = session.scalar(
            select(CollaborationGovernanceHoldModel).where(
                CollaborationGovernanceHoldModel.workspace_id == workspace_id,
                CollaborationGovernanceHoldModel.id == hold.id,
            )
        )

    assert result.data["result"] == GovernanceResolutionResult.STALE.value
    assert result.data["stale_reason"] == "plan_generation_stale"
    assert releases == []
    assert stored_hold is not None
    assert stored_hold.status == GovernanceHoldStatus.ACTIVE.value
    assert runtime_changes == []
