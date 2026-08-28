from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.plan_revisions import WorkerTransitionAction
from sidera_control_plane.domain.plan_transitions import (
    WORKER_TRANSITION_IN_FLIGHT,
    PlanEntityLineage,
    PlanTransition,
    PlanTransitionStatus,
    WorkerTransition,
    WorkerTransitionStatus,
    WriterState,
)
from sidera_control_plane.persistence.models import (
    PlanEntityLineageModel,
    PlanTransitionModel,
    WorkerTransitionModel,
)


class PlanTransitionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, transition: PlanTransition) -> None:
        self._session.add(
            PlanTransitionModel(
                id=transition.id,
                workspace_id=transition.workspace_id,
                root_task_id=transition.root_task_id,
                planning_request_id=transition.planning_request_id,
                proposal_revision_id=transition.proposal_revision_id,
                from_snapshot_id=transition.from_snapshot_id,
                from_generation=transition.from_generation,
                to_snapshot_id=transition.to_snapshot_id,
                to_generation=transition.to_generation,
                status=transition.status.value,
                started_at=transition.started_at,
                completed_at=transition.completed_at,
                version=transition.version,
            )
        )

    def get(
        self,
        transition_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> PlanTransition | None:
        statement = select(PlanTransitionModel).where(
            PlanTransitionModel.id == transition_id,
            PlanTransitionModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_plan_transition(row) if row is not None else None

    def save(self, transition: PlanTransition, previous_version: int) -> None:
        result = self._session.execute(
            update(PlanTransitionModel)
            .where(
                PlanTransitionModel.id == transition.id,
                PlanTransitionModel.workspace_id == transition.workspace_id,
                PlanTransitionModel.version == previous_version,
            )
            .values(
                status=transition.status.value,
                completed_at=transition.completed_at,
                version=transition.version,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                previous_version,
                self._current_version(transition.id, transition.workspace_id),
            )

    def claim_converging(
        self, *, now: datetime, limit: int
    ) -> tuple[PlanTransition, ...]:
        del now
        rows = self._session.scalars(
            select(PlanTransitionModel)
            .where(PlanTransitionModel.status == PlanTransitionStatus.CONVERGING.value)
            .order_by(PlanTransitionModel.started_at.asc(), PlanTransitionModel.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        return tuple(_to_plan_transition(row) for row in rows)

    def list_for_root(
        self, root_task_id: UUID, workspace_id: UUID
    ) -> tuple[PlanTransition, ...]:
        rows = self._session.scalars(
            select(PlanTransitionModel)
            .where(
                PlanTransitionModel.root_task_id == root_task_id,
                PlanTransitionModel.workspace_id == workspace_id,
            )
            .order_by(PlanTransitionModel.started_at.asc(), PlanTransitionModel.id.asc())
        ).all()
        return tuple(_to_plan_transition(row) for row in rows)

    def _current_version(self, transition_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(PlanTransitionModel.version).where(
                PlanTransitionModel.id == transition_id,
                PlanTransitionModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


class WorkerTransitionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, transition: WorkerTransition) -> None:
        self._session.add(
            WorkerTransitionModel(
                id=transition.id,
                plan_transition_id=transition.plan_transition_id,
                workspace_id=transition.workspace_id,
                root_task_id=transition.root_task_id,
                worker_item_id=transition.worker_item_id,
                from_subtask_id=transition.from_subtask_id,
                to_subtask_id=transition.to_subtask_id,
                action=transition.action.value,
                fallback_action=(
                    transition.fallback_action.value
                    if transition.fallback_action is not None
                    else None
                ),
                target_context_version=transition.target_context_version,
                source_execution_id=transition.source_execution_id,
                source_session_id=transition.source_session_id,
                target_execution_id=transition.target_execution_id,
                target_session_id=transition.target_session_id,
                writer_state=transition.writer_state.value,
                status=transition.status.value,
                failure_code=transition.failure_code,
                lease_id=transition.lease_id,
                fencing_token=transition.fencing_token,
                attempt_count=transition.attempt_count,
                next_attempt_at=transition.next_attempt_at,
                started_at=transition.started_at,
                completed_at=transition.completed_at,
                version=transition.version,
            )
        )

    def get(
        self,
        transition_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkerTransition | None:
        statement = select(WorkerTransitionModel).where(
            WorkerTransitionModel.id == transition_id,
            WorkerTransitionModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_worker_transition(row) if row is not None else None

    def get_active_for_worker_item(
        self,
        worker_item_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkerTransition | None:
        active_statuses = tuple(
            item.value
            for item in (
                *WORKER_TRANSITION_IN_FLIGHT,
                WorkerTransitionStatus.BLOCKED_UNKNOWN,
            )
        )
        statement = select(WorkerTransitionModel).where(
            WorkerTransitionModel.worker_item_id == worker_item_id,
            WorkerTransitionModel.workspace_id == workspace_id,
            WorkerTransitionModel.status.in_(active_statuses),
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_worker_transition(row) if row is not None else None

    def save(self, transition: WorkerTransition, previous_version: int) -> None:
        result = self._session.execute(
            update(WorkerTransitionModel)
            .where(
                WorkerTransitionModel.id == transition.id,
                WorkerTransitionModel.workspace_id == transition.workspace_id,
                WorkerTransitionModel.version == previous_version,
            )
            .values(
                action=transition.action.value,
                fallback_action=(
                    transition.fallback_action.value
                    if transition.fallback_action is not None
                    else None
                ),
                target_context_version=transition.target_context_version,
                source_execution_id=transition.source_execution_id,
                source_session_id=transition.source_session_id,
                target_execution_id=transition.target_execution_id,
                target_session_id=transition.target_session_id,
                writer_state=transition.writer_state.value,
                status=transition.status.value,
                failure_code=transition.failure_code,
                lease_id=transition.lease_id,
                fencing_token=transition.fencing_token,
                attempt_count=transition.attempt_count,
                next_attempt_at=transition.next_attempt_at,
                started_at=transition.started_at,
                completed_at=transition.completed_at,
                version=transition.version,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                previous_version,
                self._current_version(transition.id, transition.workspace_id),
            )

    def claim_pending(
        self, *, now: datetime, limit: int
    ) -> tuple[WorkerTransition, ...]:
        rows = self._session.scalars(
            select(WorkerTransitionModel)
            .where(
                WorkerTransitionModel.status.in_(
                    (
                        WorkerTransitionStatus.PENDING.value,
                        WorkerTransitionStatus.WAITING_SAFE_BOUNDARY.value,
                    )
                ),
                or_(
                    WorkerTransitionModel.next_attempt_at.is_(None),
                    WorkerTransitionModel.next_attempt_at <= now,
                ),
            )
            .order_by(
                func.coalesce(
                    WorkerTransitionModel.next_attempt_at,
                    WorkerTransitionModel.created_at,
                ).asc(),
                WorkerTransitionModel.id.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        return tuple(_to_worker_transition(row) for row in rows)

    def list_for_plan_transition(
        self, plan_transition_id: UUID, workspace_id: UUID
    ) -> tuple[WorkerTransition, ...]:
        rows = self._session.scalars(
            select(WorkerTransitionModel)
            .where(
                WorkerTransitionModel.plan_transition_id == plan_transition_id,
                WorkerTransitionModel.workspace_id == workspace_id,
            )
            .order_by(WorkerTransitionModel.created_at.asc(), WorkerTransitionModel.id.asc())
        ).all()
        return tuple(_to_worker_transition(row) for row in rows)

    def _current_version(self, transition_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(WorkerTransitionModel.version).where(
                WorkerTransitionModel.id == transition_id,
                WorkerTransitionModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


class PlanEntityLineageRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, lineage: PlanEntityLineage) -> None:
        self._session.add(
            PlanEntityLineageModel(
                id=lineage.id,
                plan_transition_id=lineage.plan_transition_id,
                workspace_id=lineage.workspace_id,
                target_type=lineage.target_type,
                target_id=lineage.target_id,
                source_type=lineage.source_type,
                source_id=lineage.source_id,
                reason=lineage.reason,
            )
        )

    def add_mapping(
        self,
        *,
        plan_transition_id: UUID,
        workspace_id: UUID,
        target_type: str,
        target_id: UUID,
        source_type: str,
        source_id: UUID,
        reason: str,
    ) -> PlanEntityLineage:
        lineage = PlanEntityLineage(
            id=uuid4(),
            plan_transition_id=plan_transition_id,
            workspace_id=workspace_id,
            target_type=target_type,
            target_id=target_id,
            source_type=source_type,
            source_id=source_id,
            reason=reason,
        )
        self.add(lineage)
        return lineage

    def get(self, lineage_id: UUID, workspace_id: UUID) -> PlanEntityLineage | None:
        row = self._session.scalar(
            select(PlanEntityLineageModel).where(
                PlanEntityLineageModel.id == lineage_id,
                PlanEntityLineageModel.workspace_id == workspace_id,
            )
        )
        return _to_lineage(row) if row is not None else None

    def list_for_transition(
        self, plan_transition_id: UUID, workspace_id: UUID
    ) -> tuple[PlanEntityLineage, ...]:
        rows = self._session.scalars(
            select(PlanEntityLineageModel)
            .where(
                PlanEntityLineageModel.plan_transition_id == plan_transition_id,
                PlanEntityLineageModel.workspace_id == workspace_id,
            )
            .order_by(PlanEntityLineageModel.created_at.asc(), PlanEntityLineageModel.id.asc())
        ).all()
        return tuple(_to_lineage(row) for row in rows)


def _to_plan_transition(row: PlanTransitionModel) -> PlanTransition:
    return PlanTransition(
        id=row.id,
        workspace_id=row.workspace_id,
        root_task_id=row.root_task_id,
        planning_request_id=row.planning_request_id,
        proposal_revision_id=row.proposal_revision_id,
        from_snapshot_id=row.from_snapshot_id,
        from_generation=row.from_generation,
        to_snapshot_id=row.to_snapshot_id,
        to_generation=row.to_generation,
        status=PlanTransitionStatus(row.status),
        started_at=row.started_at,
        completed_at=row.completed_at,
        version=row.version,
    )


def _to_worker_transition(row: WorkerTransitionModel) -> WorkerTransition:
    return WorkerTransition(
        id=row.id,
        plan_transition_id=row.plan_transition_id,
        workspace_id=row.workspace_id,
        root_task_id=row.root_task_id,
        worker_item_id=row.worker_item_id,
        from_subtask_id=row.from_subtask_id,
        to_subtask_id=row.to_subtask_id,
        action=WorkerTransitionAction(row.action),
        fallback_action=(
            WorkerTransitionAction(row.fallback_action)
            if row.fallback_action is not None
            else None
        ),
        target_context_version=row.target_context_version,
        source_execution_id=row.source_execution_id,
        source_session_id=row.source_session_id,
        target_execution_id=row.target_execution_id,
        target_session_id=row.target_session_id,
        writer_state=WriterState(row.writer_state),
        status=WorkerTransitionStatus(row.status),
        failure_code=row.failure_code,
        lease_id=row.lease_id,
        fencing_token=row.fencing_token,
        attempt_count=row.attempt_count,
        next_attempt_at=row.next_attempt_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        version=row.version,
    )


def _to_lineage(row: PlanEntityLineageModel) -> PlanEntityLineage:
    return PlanEntityLineage(
        id=row.id,
        plan_transition_id=row.plan_transition_id,
        workspace_id=row.workspace_id,
        target_type=row.target_type,
        target_id=row.target_id,
        source_type=row.source_type,
        source_id=row.source_id,
        reason=row.reason,
    )
