from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sidera_control_plane.domain.worker_item_projections import (
    ActiveHandoffProjection,
    TraceDescriptorProjection,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    ExecutionSessionModel,
    HandoffProposalModel,
    HandoffTransitionModel,
    WorkerModel,
)

_ACTIVE_STATUS_VALUES = ("pending", "running", "blocked", "blocked_unknown", "failed")
_RETRY_STRATEGIES = ("retry_current_phase",)


class SqlAlchemyHandoffQueryStore:
    """Read the current handoff state in bounded, workspace-scoped batches."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def load(
        self, workspace_id: UUID, worker_item_id: UUID
    ) -> ActiveHandoffProjection | None:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            loaded = self.load_in_session(
                session,
                workspace_id,
                worker_item_ids=(worker_item_id,),
            )
            return loaded.get(worker_item_id)

    def load_for_root(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        worker_item_ids: tuple[UUID, ...] = (),
    ) -> dict[UUID, ActiveHandoffProjection]:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            return self.load_for_root_in_session(
                session,
                workspace_id,
                root_task_id,
                worker_item_ids,
            )

    def load_in_session(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        worker_item_ids: tuple[UUID, ...],
    ) -> dict[UUID, ActiveHandoffProjection]:
        ordered_ids = tuple(sorted(set(worker_item_ids), key=str))
        if not ordered_ids:
            return {}
        transition_rows = session.scalars(
            select(HandoffTransitionModel)
            .where(
                HandoffTransitionModel.workspace_id == workspace_id,
                HandoffTransitionModel.worker_item_id.in_(ordered_ids),
                HandoffTransitionModel.status.in_(_ACTIVE_STATUS_VALUES),
            )
            .order_by(
                HandoffTransitionModel.worker_item_id.asc(),
                HandoffTransitionModel.updated_at.desc(),
                HandoffTransitionModel.id.desc(),
            )
        ).all()
        return self._project_rows(session, workspace_id, transition_rows)

    def load_for_root_in_session(
        self,
        session: Session,
        workspace_id: UUID,
        root_task_id: UUID,
        worker_item_ids: tuple[UUID, ...] = (),
    ) -> dict[UUID, ActiveHandoffProjection]:
        ordered_ids = tuple(sorted(set(worker_item_ids), key=str))
        statement = (
            select(HandoffTransitionModel)
            .join(
                HandoffProposalModel,
                (HandoffProposalModel.id == HandoffTransitionModel.proposal_id)
                & (HandoffProposalModel.workspace_id == HandoffTransitionModel.workspace_id),
            )
            .where(
                HandoffTransitionModel.workspace_id == workspace_id,
                HandoffProposalModel.root_task_id == root_task_id,
                HandoffTransitionModel.status.in_(_ACTIVE_STATUS_VALUES),
            )
            .order_by(
                HandoffTransitionModel.worker_item_id.asc(),
                HandoffTransitionModel.updated_at.desc(),
                HandoffTransitionModel.id.desc(),
            )
        )
        if ordered_ids:
            statement = statement.where(
                HandoffTransitionModel.worker_item_id.in_(ordered_ids)
            )
        transition_rows = session.scalars(statement).all()
        return self._project_rows(session, workspace_id, transition_rows)

    @classmethod
    def _project_rows(
        cls,
        session: Session,
        workspace_id: UUID,
        transition_rows: Iterable[HandoffTransitionModel],
    ) -> dict[UUID, ActiveHandoffProjection]:
        rows = tuple(transition_rows)
        if not rows:
            return {}
        proposal_ids = {row.proposal_id for row in rows}
        proposals = {
            row.id: row
            for row in session.scalars(
                select(HandoffProposalModel).where(
                    HandoffProposalModel.workspace_id == workspace_id,
                    HandoffProposalModel.id.in_(proposal_ids),
                )
            ).all()
        }
        execution_ids = {
            execution_id
            for row in rows
            for execution_id in (row.source_execution_id, row.target_execution_id)
            if execution_id is not None
        }
        executions = {
            row.id: row
            for row in session.scalars(
                select(ExecutionModel).where(
                    ExecutionModel.workspace_id == workspace_id,
                    ExecutionModel.id.in_(execution_ids or {UUID(int=0)}),
                )
            ).all()
        }
        session_ids = {
            session_id
            for row in rows
            for session_id in (row.source_session_id, row.target_session_id)
            if session_id is not None
        }
        sessions = {
            row.id: row
            for row in session.scalars(
                select(ExecutionSessionModel).where(
                    ExecutionSessionModel.workspace_id == workspace_id,
                    ExecutionSessionModel.id.in_(session_ids or {UUID(int=0)}),
                )
            ).all()
        }
        worker_ids = {
            worker_id
            for row in rows
            for worker_id in (
                proposals[row.proposal_id].source_worker_id
                if row.proposal_id in proposals
                else None,
                row.target_worker_id,
            )
            if worker_id is not None
        }
        session.scalars(
            select(WorkerModel).where(
                WorkerModel.workspace_id == workspace_id,
                WorkerModel.id.in_(worker_ids or {UUID(int=0)}),
            )
        ).all()

        result: dict[UUID, ActiveHandoffProjection] = {}
        for row in rows:
            if row.worker_item_id in result:
                continue
            proposal = proposals.get(row.proposal_id)
            if proposal is None:
                continue
            source_trace = _trace_descriptor(
                proposal.root_task_id,
                row.source_execution_id,
                row.source_session_id,
                executions,
                sessions,
            )
            target_trace = None
            if row.target_execution_id is not None and row.target_session_id is not None:
                target_trace = _trace_descriptor(
                    proposal.root_task_id,
                    row.target_execution_id,
                    row.target_session_id,
                    executions,
                    sessions,
                )
            strategies = _recovery_strategies(row)
            result[row.worker_item_id] = ActiveHandoffProjection(
                transition_id=row.id,
                proposal_id=row.proposal_id,
                status=row.status,
                phase=row.phase,
                writer_state=row.writer_state,
                source_execution_id=row.source_execution_id,
                source_session_id=row.source_session_id,
                source_worker_id=proposal.source_worker_id,
                target_worker_id=row.target_worker_id,
                target_execution_id=row.target_execution_id,
                target_session_id=row.target_session_id,
                attention_required=_attention_required(row.status, strategies),
                failure_code=row.failure_code,
                blocked_operation_id=row.blocked_operation_id,
                available_recovery_strategies=strategies,
                source_trace_descriptor=source_trace,
                target_trace_descriptor=target_trace,
                started_at=_as_utc_optional(row.started_at),
                updated_at=_as_utc(row.updated_at),
            )
        return result


def _trace_descriptor(
    root_task_id: UUID,
    execution_id: UUID,
    session_id: UUID,
    executions: dict[UUID, ExecutionModel],
    sessions: dict[UUID, ExecutionSessionModel],
) -> TraceDescriptorProjection | None:
    if execution_id not in executions or session_id not in sessions:
        return None
    return TraceDescriptorProjection(
        root_task_id=root_task_id,
        execution_id=execution_id,
        session_id=session_id,
        route=(
            f"/api/v1/tasks/{root_task_id}/executions/{execution_id}/"
            f"sessions/{session_id}/trace"
        ),
        raw_ref=None,
    )


def _recovery_strategies(row: HandoffTransitionModel) -> tuple[str, ...]:
    if row.status == "blocked" and row.failure_code not in {
        "gateway_recovery_required",
        "recovery_required",
        "recovery_decision_required",
    }:
        return _RETRY_STRATEGIES
    return ()


def _attention_required(status: str, strategies: tuple[str, ...]) -> bool:
    if status == "blocked_unknown":
        return True
    if status == "failed":
        return not strategies
    if status == "blocked":
        return not strategies
    return False


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _as_utc_optional(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None
