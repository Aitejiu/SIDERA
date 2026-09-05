from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

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
from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.persistence.models import (
    CollaborationGovernanceHoldModel,
    CollaborationGovernanceResolutionModel,
    CollaborationReleaseProposalModel,
    CollaborationRunModel,
    RootTaskPlanHeadModel,
)


class CollaborationGovernanceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_hold(
        self, workspace_id: UUID, hold_id: UUID, *, for_update: bool = False
    ) -> CollaborationGovernanceHold | None:
        statement = select(CollaborationGovernanceHoldModel).where(
            CollaborationGovernanceHoldModel.workspace_id == workspace_id,
            CollaborationGovernanceHoldModel.id == hold_id,
        )
        row = self._one(statement, for_update)
        return _to_hold(row) if row is not None else None

    def get_active_hold(
        self,
        workspace_id: UUID,
        run_id: UUID,
        worker_item_id: UUID,
        *,
        for_update: bool = False,
    ) -> CollaborationGovernanceHold | None:
        statement = select(CollaborationGovernanceHoldModel).where(
            CollaborationGovernanceHoldModel.workspace_id == workspace_id,
            CollaborationGovernanceHoldModel.run_id == run_id,
            CollaborationGovernanceHoldModel.worker_item_id == worker_item_id,
            CollaborationGovernanceHoldModel.status
            == GovernanceHoldStatus.ACTIVE.value,
        )
        row = self._one(statement, for_update)
        return _to_hold(row) if row is not None else None

    def get_active_holds(
        self,
        workspace_id: UUID,
        run_id: UUID,
        worker_item_ids: tuple[UUID, ...],
        *,
        for_update: bool = False,
    ) -> tuple[CollaborationGovernanceHold, ...]:
        if not worker_item_ids:
            return ()
        statement = (
            select(CollaborationGovernanceHoldModel)
            .where(
                CollaborationGovernanceHoldModel.workspace_id == workspace_id,
                CollaborationGovernanceHoldModel.run_id == run_id,
                CollaborationGovernanceHoldModel.worker_item_id.in_(worker_item_ids),
                CollaborationGovernanceHoldModel.status
                == GovernanceHoldStatus.ACTIVE.value,
            )
            .order_by(CollaborationGovernanceHoldModel.worker_item_id.asc())
        )
        rows = self._many(statement, for_update)
        return tuple(_to_hold(row) for row in rows)

    def list_active_holds_for_run(
        self, workspace_id: UUID, run_id: UUID, *, for_update: bool = False
    ) -> tuple[CollaborationGovernanceHold, ...]:
        statement = (
            select(CollaborationGovernanceHoldModel)
            .where(
                CollaborationGovernanceHoldModel.workspace_id == workspace_id,
                CollaborationGovernanceHoldModel.run_id == run_id,
                CollaborationGovernanceHoldModel.status
                == GovernanceHoldStatus.ACTIVE.value,
            )
            .order_by(
                CollaborationGovernanceHoldModel.created_at.asc(),
                CollaborationGovernanceHoldModel.id.asc(),
            )
        )
        rows = self._many(statement, for_update)
        return tuple(_to_hold(row) for row in rows)

    def add_hold(self, hold: CollaborationGovernanceHold) -> None:
        self._session.add(_hold_model(hold))

    def save_hold(
        self, hold: CollaborationGovernanceHold, previous_version: int
    ) -> None:
        result = self._session.execute(
            update(CollaborationGovernanceHoldModel)
            .where(
                CollaborationGovernanceHoldModel.workspace_id == hold.workspace_id,
                CollaborationGovernanceHoldModel.id == hold.id,
                CollaborationGovernanceHoldModel.resource_version == previous_version,
            )
            .values(
                status=hold.status.value,
                reason=hold.reason,
                resource_version=hold.resource_version,
                resolved_at=hold.resolved_at,
            )
        )
        _check_cas(
            result,
            previous_version,
            hold.id,
            hold.workspace_id,
            self._session,
            CollaborationGovernanceHoldModel,
        )

    def get_proposal(
        self, workspace_id: UUID, proposal_id: UUID, *, for_update: bool = False
    ) -> CollaborationReleaseProposal | None:
        statement = select(CollaborationReleaseProposalModel).where(
            CollaborationReleaseProposalModel.workspace_id == workspace_id,
            CollaborationReleaseProposalModel.id == proposal_id,
        )
        row = self._one(statement, for_update)
        return _to_proposal(row) if row is not None else None

    def get_proposal_for_intent(
        self, workspace_id: UUID, intent_id: UUID
    ) -> CollaborationReleaseProposal | None:
        row = self._session.scalar(
            select(CollaborationReleaseProposalModel).where(
                CollaborationReleaseProposalModel.workspace_id == workspace_id,
                CollaborationReleaseProposalModel.intent_id == intent_id,
            )
        )
        return _to_proposal(row) if row is not None else None

    def find_matching_proposal(
        self,
        workspace_id: UUID,
        hold_id: UUID,
        execution_request_id: UUID,
        target_request_version: int,
        fingerprint_version: int,
        fingerprint: str,
        *,
        for_update: bool = False,
    ) -> CollaborationReleaseProposal | None:
        statement = select(CollaborationReleaseProposalModel).where(
            CollaborationReleaseProposalModel.workspace_id == workspace_id,
            CollaborationReleaseProposalModel.hold_id == hold_id,
            CollaborationReleaseProposalModel.execution_request_id
            == execution_request_id,
            CollaborationReleaseProposalModel.target_request_version
            == target_request_version,
            CollaborationReleaseProposalModel.input_fingerprint_version
            == fingerprint_version,
            CollaborationReleaseProposalModel.input_fingerprint == fingerprint,
        )
        row = self._one(statement, for_update)
        return _to_proposal(row) if row is not None else None

    def list_pending_proposals_for_hold(
        self, workspace_id: UUID, hold_id: UUID, *, for_update: bool = False
    ) -> tuple[CollaborationReleaseProposal, ...]:
        statement = (
            select(CollaborationReleaseProposalModel)
            .where(
                CollaborationReleaseProposalModel.workspace_id == workspace_id,
                CollaborationReleaseProposalModel.hold_id == hold_id,
                CollaborationReleaseProposalModel.status
                == ReleaseProposalStatus.PENDING.value,
            )
            .order_by(
                CollaborationReleaseProposalModel.created_at.asc(),
                CollaborationReleaseProposalModel.id.asc(),
            )
        )
        rows = self._many(statement, for_update)
        return tuple(_to_proposal(row) for row in rows)

    def list_pending_proposals_for_run(
        self, workspace_id: UUID, run_id: UUID, *, for_update: bool = False
    ) -> tuple[CollaborationReleaseProposal, ...]:
        statement = (
            select(CollaborationReleaseProposalModel)
            .where(
                CollaborationReleaseProposalModel.workspace_id == workspace_id,
                CollaborationReleaseProposalModel.run_id == run_id,
                CollaborationReleaseProposalModel.status
                == ReleaseProposalStatus.PENDING.value,
            )
            .order_by(
                CollaborationReleaseProposalModel.created_at.asc(),
                CollaborationReleaseProposalModel.id.asc(),
            )
        )
        rows = self._many(statement, for_update)
        return tuple(_to_proposal(row) for row in rows)

    def list_supersede_candidates(
        self,
        *,
        now,
        limit: int,
        workspace_id: UUID | None = None,
    ) -> tuple[CollaborationGovernanceHold, ...]:
        _positive_limit(limit)
        _aware(now)
        run = CollaborationRunModel
        head = RootTaskPlanHeadModel
        statement = (
            select(CollaborationGovernanceHoldModel)
            .join(
                run,
                and_(
                    run.id == CollaborationGovernanceHoldModel.run_id,
                    run.workspace_id == CollaborationGovernanceHoldModel.workspace_id,
                ),
            )
            .outerjoin(
                head,
                and_(
                    head.root_task_id == CollaborationGovernanceHoldModel.root_task_id,
                    head.workspace_id == CollaborationGovernanceHoldModel.workspace_id,
                ),
            )
            .where(
                CollaborationGovernanceHoldModel.status
                == GovernanceHoldStatus.ACTIVE.value,
                CollaborationGovernanceHoldModel.created_at <= now,
                or_(
                    run.status == "superseded",
                    head.root_task_id.is_(None),
                    head.active_snapshot_id != run.plan_snapshot_id,
                    head.active_generation != run.plan_generation,
                    head.active_snapshot_hash != run.plan_content_hash,
                ),
            )
            .order_by(
                CollaborationGovernanceHoldModel.created_at.asc(),
                CollaborationGovernanceHoldModel.id.asc(),
            )
            .limit(limit)
            .with_for_update(of=CollaborationGovernanceHoldModel, skip_locked=True)
        )
        if workspace_id is not None:
            statement = statement.where(
                CollaborationGovernanceHoldModel.workspace_id == workspace_id
            )
        rows = self._session.scalars(statement).all()
        return tuple(_to_hold(row) for row in rows)

    def add_proposal(self, proposal: CollaborationReleaseProposal) -> None:
        self._session.add(_proposal_model(proposal))

    def save_proposal(
        self, proposal: CollaborationReleaseProposal, previous_version: int
    ) -> None:
        result = self._session.execute(
            update(CollaborationReleaseProposalModel)
            .where(
                CollaborationReleaseProposalModel.workspace_id == proposal.workspace_id,
                CollaborationReleaseProposalModel.id == proposal.id,
                CollaborationReleaseProposalModel.resource_version == previous_version,
            )
            .values(
                status=proposal.status.value,
                resource_version=proposal.resource_version,
                resolved_at=proposal.resolved_at,
            )
        )
        _check_cas(
            result,
            previous_version,
            proposal.id,
            proposal.workspace_id,
            self._session,
            CollaborationReleaseProposalModel,
        )

    def get_resolution_for_proposal(
        self, workspace_id: UUID, proposal_id: UUID
    ) -> CollaborationGovernanceResolution | None:
        row = self._session.scalar(
            select(CollaborationGovernanceResolutionModel).where(
                CollaborationGovernanceResolutionModel.workspace_id == workspace_id,
                CollaborationGovernanceResolutionModel.proposal_id == proposal_id,
            )
        )
        return _to_resolution(row) if row is not None else None

    def add_resolution(self, resolution: CollaborationGovernanceResolution) -> None:
        self._session.add(_resolution_model(resolution))

    def _one(self, statement, for_update: bool):
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def _many(self, statement, for_update: bool):
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalars(statement).all()


def _hold_model(hold: CollaborationGovernanceHold) -> CollaborationGovernanceHoldModel:
    return CollaborationGovernanceHoldModel(
        id=hold.id,
        workspace_id=hold.workspace_id,
        root_task_id=hold.root_task_id,
        run_id=hold.run_id,
        plan_snapshot_id=hold.plan_snapshot_id,
        plan_generation=hold.plan_generation,
        plan_content_hash=hold.plan_content_hash,
        worker_item_id=hold.worker_item_id,
        status=hold.status.value,
        created_by_actor_id=hold.created_by_actor_id,
        created_via=hold.created_via.value,
        reason=hold.reason,
        resource_version=hold.resource_version,
        created_at=hold.created_at,
        resolved_at=hold.resolved_at,
    )


def _proposal_model(
    proposal: CollaborationReleaseProposal,
) -> CollaborationReleaseProposalModel:
    return CollaborationReleaseProposalModel(
        id=proposal.id,
        workspace_id=proposal.workspace_id,
        hold_id=proposal.hold_id,
        run_id=proposal.run_id,
        projection_snapshot_id=proposal.projection_snapshot_id,
        projection_hash=proposal.projection_hash,
        decision_id=proposal.decision_id,
        intent_id=proposal.intent_id,
        execution_request_id=proposal.execution_request_id,
        target_request_version=proposal.target_request_version,
        input_fingerprint_version=proposal.input_fingerprint_version,
        input_fingerprint=proposal.input_fingerprint,
        input_selection_id=proposal.input_selection_id,
        input_selection_hash=proposal.input_selection_hash,
        status=proposal.status.value,
        reason_code=proposal.reason_code,
        summary=proposal.summary,
        resource_version=proposal.resource_version,
        created_at=proposal.created_at,
        resolved_at=proposal.resolved_at,
    )


def _resolution_model(
    resolution: CollaborationGovernanceResolution,
) -> CollaborationGovernanceResolutionModel:
    return CollaborationGovernanceResolutionModel(
        id=resolution.id,
        workspace_id=resolution.workspace_id,
        proposal_id=resolution.proposal_id,
        action=resolution.action.value,
        result=resolution.result.value,
        actor_id=resolution.actor_id,
        transport=resolution.transport.value,
        reason=resolution.reason,
        command_id=resolution.command_id,
        created_at=resolution.created_at,
    )


def _to_hold(row: CollaborationGovernanceHoldModel) -> CollaborationGovernanceHold:
    return CollaborationGovernanceHold(
        id=row.id,
        workspace_id=row.workspace_id,
        root_task_id=row.root_task_id,
        run_id=row.run_id,
        plan_snapshot_id=row.plan_snapshot_id,
        plan_generation=row.plan_generation,
        plan_content_hash=row.plan_content_hash,
        worker_item_id=row.worker_item_id,
        status=GovernanceHoldStatus(row.status),
        created_by_actor_id=row.created_by_actor_id,
        created_via=GovernanceTransport(row.created_via),
        reason=row.reason,
        resource_version=row.resource_version,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
    )


def _to_proposal(
    row: CollaborationReleaseProposalModel,
) -> CollaborationReleaseProposal:
    return CollaborationReleaseProposal(
        id=row.id,
        workspace_id=row.workspace_id,
        hold_id=row.hold_id,
        run_id=row.run_id,
        projection_snapshot_id=row.projection_snapshot_id,
        projection_hash=row.projection_hash,
        decision_id=row.decision_id,
        intent_id=row.intent_id,
        execution_request_id=row.execution_request_id,
        target_request_version=row.target_request_version,
        input_fingerprint_version=row.input_fingerprint_version,
        input_fingerprint=row.input_fingerprint,
        input_selection_id=row.input_selection_id,
        input_selection_hash=row.input_selection_hash,
        status=ReleaseProposalStatus(row.status),
        reason_code=row.reason_code,
        summary=row.summary,
        resource_version=row.resource_version,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
    )


def _to_resolution(
    row: CollaborationGovernanceResolutionModel,
) -> CollaborationGovernanceResolution:
    return CollaborationGovernanceResolution(
        id=row.id,
        workspace_id=row.workspace_id,
        proposal_id=row.proposal_id,
        action=GovernanceResolutionAction(row.action),
        result=GovernanceResolutionResult(row.result),
        actor_id=row.actor_id,
        transport=GovernanceTransport(row.transport),
        reason=row.reason,
        command_id=row.command_id,
        created_at=row.created_at,
    )


def _check_cas(
    result: Any,
    previous_version: int,
    entity_id: UUID,
    workspace_id: UUID,
    session: Session,
    model,
) -> None:
    if result.rowcount == 1:
        return
    current = session.scalar(
        select(model.resource_version).where(
            model.id == entity_id,
            model.workspace_id == workspace_id,
        )
    )
    raise VersionConflict(previous_version, int(current or 0))


def _positive_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be positive")


def _aware(value: Any) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
