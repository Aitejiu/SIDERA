from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from sidera_control_plane.application.collaboration_governance_reconciler import (
    CollaborationGovernanceReconciler,
)
from sidera_control_plane.persistence.models import (
    CollaborationGovernanceHoldModel,
    CollaborationReleaseProposalModel,
    EventModel,
    ProjectionChangeModel,
    RootTaskPlanHeadModel,
    RuntimeIntentApplicationModel,
)

from .test_collaboration_governance_commands import _prepare_held_proposal

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _advance_generation(database: Any, uow_factory: Any, snapshot: Any) -> Any:
    next_snapshot = replace(
        snapshot,
        id=uuid4(),
        generation=snapshot.generation + 1,
        parent_snapshot_id=snapshot.id,
        published_at=NOW,
    )
    with uow_factory() as uow:
        head = uow.plans.get_head(
            snapshot.root_task_id,
            snapshot.workspace_id,
            for_update=True,
        )
        assert head is not None
        uow.plans.add_snapshot_and_advance_head(
            next_snapshot,
            expected_head=head,
        )
    return next_snapshot


def test_generation_supersede_stales_only_open_governance_and_keeps_history(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, snapshot, prepared, hold, proposal = _prepare_held_proposal(
        database,
        workspace_id,
        uow_factory,
    )
    next_snapshot = _advance_generation(database, uow_factory, snapshot)
    governance = CollaborationGovernanceReconciler()

    with database.session() as session:
        governance_history_before = session.scalars(
            select(ProjectionChangeModel.change_type)
            .where(
                ProjectionChangeModel.workspace_id == workspace_id,
                ProjectionChangeModel.projection_type == "collaboration_governance",
            )
            .order_by(ProjectionChangeModel.cursor.asc())
        ).all()

    with uow_factory() as uow:
        run = uow.collaboration_runs.get(
            workspace_id,
            prepared.run.id,
            for_update=True,
        )
        assert run is not None
        previous_version = run.resource_version
        run.supersede(now=NOW)
        uow.collaboration_runs.save(run, previous_version)
        result = governance.supersede_run(
            uow,
            run,
            now=NOW,
            actor_id="collaboration-governance-reconciler",
            correlation_id=run.id,
        )

    assert result.superseded_holds == 1
    assert result.staled_proposals == 1
    assert result.staled_applications == 1

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
        events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.event_type.in_(
                    (
                        "CollaborationReleaseProposalStale",
                        "CollaborationGovernanceHoldSuperseded",
                    )
                ),
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
        governance_changes = session.scalars(
            select(ProjectionChangeModel.change_type)
            .where(
                ProjectionChangeModel.workspace_id == workspace_id,
                ProjectionChangeModel.projection_type == "collaboration_governance",
            )
            .order_by(ProjectionChangeModel.cursor.asc())
        ).all()
        heads = session.scalars(
            select(RootTaskPlanHeadModel).where(
                RootTaskPlanHeadModel.workspace_id == workspace_id,
                RootTaskPlanHeadModel.root_task_id == ids["root_task"],
            )
        ).all()

    assert stored_hold is not None and stored_hold.status == "superseded"
    assert stored_proposal is not None and stored_proposal.status == "stale"
    assert application is not None and application.status == "stale"
    assert [event.event_type for event in events] == [
        "CollaborationReleaseProposalStale",
        "CollaborationGovernanceHoldSuperseded",
    ]
    assert runtime_changes == []
    assert governance_changes[: len(governance_history_before)] == (
        governance_history_before
    )
    assert governance_changes[len(governance_history_before) :] == [
        "CollaborationReleaseProposalStale",
        "collaboration_release_proposal_stale",
        "CollaborationGovernanceHoldSuperseded",
        "collaboration_governance_hold_superseded",
    ]
    assert len(heads) == 1
    assert heads[0].active_snapshot_id == next_snapshot.id


def test_recovery_query_repairs_committed_head_once_and_is_bounded(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, snapshot, _prepared, hold, proposal = _prepare_held_proposal(
        database,
        workspace_id,
        uow_factory,
    )
    _advance_generation(database, uow_factory, snapshot)

    with uow_factory() as uow, pytest.raises(ValueError):
        uow.collaboration_governance.list_supersede_candidates(
            now=NOW,
            limit=0,
        )
    governance = CollaborationGovernanceReconciler(uow_factory=uow_factory)
    first = governance.recover(
        None,
        now=NOW,
        actor_id="collaboration-governance-reconciler",
        correlation_id=uuid4(),
        limit=10,
    )
    second = governance.recover(
        None,
        now=NOW,
        actor_id="collaboration-governance-reconciler",
        correlation_id=uuid4(),
        limit=10,
    )

    assert first.candidates == 1
    assert first.repaired == 1
    assert first.failed == 0
    assert second.candidates == 0
    assert second.repaired == 0
    assert second.failed == 0

    with database.session() as session:
        hold_events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.aggregate_id == hold.id,
                EventModel.event_type == "CollaborationGovernanceHoldSuperseded",
            )
        ).all()
        stale_events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.aggregate_id == proposal.id,
                EventModel.event_type == "CollaborationReleaseProposalStale",
            )
        ).all()

    assert len(hold_events) == 1
    assert len(stale_events) == 1
