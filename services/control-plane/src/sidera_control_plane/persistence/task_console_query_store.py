from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from sidera_control_plane.application.task_console_queries import (
    TaskConsoleQuerySnapshot,
)
from sidera_control_plane.domain.attention import AttentionAcknowledgement
from sidera_control_plane.domain.plan_normalization import PlanNormalizer
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.scheduling import scheduling_block_summary
from sidera_control_plane.domain.task_console_projections import (
    TaskConsoleActivityFact,
    TaskConsoleEvidenceFact,
    TaskConsoleExecutionFact,
    TaskConsolePlacementFact,
    TaskConsoleTaskFact,
    TaskConsoleWorkerItemFact,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AttentionAcknowledgementModel,
    EvidenceClaimModel,
    ExecutionEventModel,
    ExecutionModel,
    ExecutionSessionModel,
    PlanSnapshotModel,
    ProjectionChangeModel,
    RootTaskPlanHeadModel,
    SchedulingBlockModel,
    TaskModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
    WorkspaceProjectionCursorModel,
)
from sidera_control_plane.persistence.plan_convergence_query import (
    load_plan_convergence_facts,
)


class SqlAlchemyTaskConsoleQueryStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._normalizer = PlanNormalizer()

    def load(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        actor_type: str | None = None,
        actor_id: str | None = None,
    ) -> TaskConsoleQuerySnapshot | None:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            plan_row = session.scalar(
                select(PlanSnapshotModel)
                .join(
                    RootTaskPlanHeadModel,
                    (RootTaskPlanHeadModel.active_snapshot_id == PlanSnapshotModel.id)
                    & (
                        RootTaskPlanHeadModel.workspace_id
                        == PlanSnapshotModel.workspace_id
                    )
                    & (
                        RootTaskPlanHeadModel.root_task_id
                        == PlanSnapshotModel.root_task_id
                    ),
                )
                .where(
                    PlanSnapshotModel.workspace_id == workspace_id,
                    PlanSnapshotModel.root_task_id == root_task_id,
                )
            )
            if plan_row is None:
                return None
            snapshot = _plan(plan_row)
            plan = self._normalizer.normalize(snapshot)

            task_ids = {
                subtask.task_id for subtask in plan.subtasks if subtask.task_id
            }
            task_ids.add(root_task_id)
            task_rows = session.scalars(
                select(TaskModel).where(
                    TaskModel.workspace_id == workspace_id,
                    TaskModel.id.in_(task_ids or {UUID(int=0)}),
                )
            ).all()
            tasks = {row.id: _task(row) for row in task_rows}

            placement_rows = session.scalars(
                select(WorkerItemPlacementModel)
                .where(
                    WorkerItemPlacementModel.workspace_id == workspace_id,
                    WorkerItemPlacementModel.root_task_id == root_task_id,
                    WorkerItemPlacementModel.plan_snapshot_id == snapshot.id,
                )
                .order_by(
                    WorkerItemPlacementModel.subtask_key.asc(),
                    WorkerItemPlacementModel.worker_item_key.asc(),
                )
            ).all()
            placements = tuple(_placement(row) for row in placement_rows)
            worker_item_ids = {placement.worker_item_id for placement in placements}
            worker_item_rows = session.scalars(
                select(WorkerItemModel).where(
                    WorkerItemModel.workspace_id == workspace_id,
                    WorkerItemModel.id.in_(worker_item_ids or {UUID(int=0)}),
                )
            ).all()
            worker_items = {row.id: _worker_item(row) for row in worker_item_rows}

            execution_statement = select(ExecutionModel).where(
                ExecutionModel.workspace_id == workspace_id,
                or_(
                    ExecutionModel.task_id.in_(task_ids or {UUID(int=0)}),
                    ExecutionModel.worker_item_id.in_(
                        worker_item_ids or {UUID(int=0)}
                    ),
                ),
            )
            execution_rows = session.scalars(
                execution_statement.order_by(
                    ExecutionModel.updated_at.asc(), ExecutionModel.id.asc()
                )
            ).all()
            execution_ids = {row.id for row in execution_rows}

            block_rows = session.scalars(
                select(SchedulingBlockModel).where(
                    SchedulingBlockModel.workspace_id == workspace_id,
                    SchedulingBlockModel.execution_id.in_(
                        execution_ids or {UUID(int=0)}
                    ),
                    SchedulingBlockModel.status == "active",
                )
            ).all()
            active_blocks = {row.execution_id: row for row in block_rows}

            session_rows = session.scalars(
                select(ExecutionSessionModel)
                .where(
                    ExecutionSessionModel.workspace_id == workspace_id,
                    ExecutionSessionModel.execution_id.in_(
                        execution_ids or {UUID(int=0)}
                    ),
                )
                .order_by(
                    ExecutionSessionModel.last_activity_at.desc(),
                    ExecutionSessionModel.id.desc(),
                )
            ).all()
            session_by_execution = _latest_session_ids(session_rows)
            executions = tuple(
                _execution(
                    row,
                    session_by_execution.get(row.id),
                    active_blocks.get(row.id),
                )
                for row in execution_rows
            )

            evidence_rows = session.scalars(
                select(EvidenceClaimModel)
                .where(
                    EvidenceClaimModel.workspace_id == workspace_id,
                    EvidenceClaimModel.task_id.in_(task_ids or {UUID(int=0)}),
                )
                .order_by(EvidenceClaimModel.created_at.asc(), EvidenceClaimModel.id.asc())
            ).all()
            evidence = tuple(_evidence(row) for row in evidence_rows)

            event_rows = session.scalars(
                select(ExecutionEventModel)
                .where(
                    ExecutionEventModel.workspace_id == workspace_id,
                    ExecutionEventModel.execution_id.in_(
                        execution_ids or {UUID(int=0)}
                    ),
                )
                .order_by(
                    ExecutionEventModel.occurred_at.asc(),
                    ExecutionEventModel.id.asc(),
                )
            ).all()
            execution_by_id = {execution.id: execution for execution in executions}
            activity = tuple(
                _activity(row, execution_by_id[row.execution_id])
                for row in event_rows
                if row.execution_id in execution_by_id
            )

            acknowledgements = self._acknowledgements(
                session,
                workspace_id,
                root_task_id,
                actor_type=actor_type,
                actor_id=actor_id,
            )
            last_cursor = session.scalar(
                select(WorkspaceProjectionCursorModel.last_cursor).where(
                    WorkspaceProjectionCursorModel.workspace_id == workspace_id
                )
            )
            if last_cursor is None:
                last_cursor = session.scalar(
                    select(ProjectionChangeModel.cursor)
                    .where(ProjectionChangeModel.workspace_id == workspace_id)
                    .order_by(ProjectionChangeModel.cursor.desc())
                    .limit(1)
                ) or 0
            as_of = max(
                (
                    value.occurred_at
                    for value in activity
                ),
                default=snapshot.published_at,
            )
            as_of = max(
                as_of,
                *(
                    value.updated_at
                    for value in worker_items.values()
                ),
                *(
                    value.updated_at
                    for value in tasks.values()
                ),
            )
            convergence_facts = load_plan_convergence_facts(
                session,
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                plan=plan,
            )
            return TaskConsoleQuerySnapshot(
                workspace_id=workspace_id,
                plan=plan,
                tasks=tasks,
                worker_items=worker_items,
                placements=placements,
                executions=executions,
                evidence=evidence,
                activity=activity,
                acknowledgements=acknowledgements,
                last_change_cursor=int(last_cursor),
                as_of=as_of,
                convergence_facts=convergence_facts,
            )

    def list_current_root_ids(self, workspace_id: UUID) -> tuple[UUID, ...]:
        with self._database.session() as session:
            rows = session.scalars(
                select(RootTaskPlanHeadModel.root_task_id)
                .where(RootTaskPlanHeadModel.workspace_id == workspace_id)
                .order_by(RootTaskPlanHeadModel.root_task_id.asc())
            ).all()
        return tuple(rows)

    @staticmethod
    def _acknowledgements(
        session: Session,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        actor_type: str | None,
        actor_id: str | None,
    ) -> tuple[AttentionAcknowledgement, ...]:
        if actor_type is None or actor_id is None:
            return ()
        rows = session.scalars(
            select(AttentionAcknowledgementModel).where(
                AttentionAcknowledgementModel.workspace_id == workspace_id,
                AttentionAcknowledgementModel.root_task_id == root_task_id,
                AttentionAcknowledgementModel.actor_type == actor_type,
                AttentionAcknowledgementModel.actor_id == actor_id,
            )
        ).all()
        return tuple(
            AttentionAcknowledgement(
                workspace_id=row.workspace_id,
                actor_type=row.actor_type,
                actor_id=row.actor_id,
                attention_key=row.attention_key,
                root_task_id=row.root_task_id,
                source_correlation_id=row.source_correlation_id,
                source_resource_version=row.source_resource_version,
                acknowledged_command_id=row.acknowledged_command_id,
                acknowledged_at=row.acknowledged_at,
            )
            for row in rows
        )


def _plan(row: PlanSnapshotModel) -> TaskPlanSnapshot:
    snapshot = TaskPlanSnapshot.from_payload(dict(row.payload))
    if snapshot.id != row.id or snapshot.generation != row.generation:
        raise ValueError("task plan snapshot payload does not match identity columns")
    return snapshot


def _task(row: TaskModel) -> TaskConsoleTaskFact:
    return TaskConsoleTaskFact(
        id=row.id,
        title=row.title,
        status=row.status,
        version=row.version,
        updated_at=_as_utc(row.updated_at),
    )


def _worker_item(row: WorkerItemModel) -> TaskConsoleWorkerItemFact:
    return TaskConsoleWorkerItemFact(
        id=row.id,
        stable_key=row.stable_key,
        title=row.title,
        responsibility=row.responsibility,
        status=row.status,
        context_version=row.context_version,
        updated_at=_as_utc(row.updated_at),
    )


def _placement(row: WorkerItemPlacementModel) -> TaskConsolePlacementFact:
    return TaskConsolePlacementFact(
        plan_snapshot_id=row.plan_snapshot_id,
        plan_generation=row.plan_generation,
        worker_item_id=row.worker_item_id,
        worker_item_key=row.worker_item_key,
        subtask_key=row.subtask_key,
        subtask_task_id=row.subtask_task_id,
    )


def _execution(
    row: ExecutionModel,
    session_id: UUID | None,
    block: SchedulingBlockModel | None,
) -> TaskConsoleExecutionFact:
    return TaskConsoleExecutionFact(
        id=row.id,
        task_id=row.task_id,
        worker_item_id=row.worker_item_id,
        status=row.status,
        updated_at=_as_utc(row.updated_at),
        session_id=session_id,
        assigned_worker_id=row.assigned_worker_id,
        version=row.version,
        active_block_id=block.id if block is not None else None,
        active_block_version=block.version if block is not None else None,
        active_block_reason=block.reason_code if block is not None else None,
        active_block_requires_operator=(
            block.requires_operator if block is not None else False
        ),
        active_block_first_seen_at=(
            _as_utc(block.first_seen_at) if block is not None else None
        ),
        active_block_summary=(
            _scheduling_block_summary(block) if block is not None else None
        ),
    )


def _evidence(row: EvidenceClaimModel) -> TaskConsoleEvidenceFact:
    return TaskConsoleEvidenceFact(
        id=row.id,
        task_id=row.task_id,
        acceptance_ref=row.acceptance_criterion_ref,
        created_at=_as_utc(row.created_at),
    )


def _activity(
    row: ExecutionEventModel, execution: TaskConsoleExecutionFact
) -> TaskConsoleActivityFact:
    payload = dict(row.payload)
    summary = str(payload.get("summary") or payload.get("title") or row.event_type)
    return TaskConsoleActivityFact(
        id=row.id,
        task_id=execution.task_id,
        worker_item_id=execution.worker_item_id,
        occurred_at=_as_utc(row.occurred_at),
        kind=row.event_type,
        summary=summary,
        correlation_id=row.id,
    )


def _scheduling_block_summary(block: SchedulingBlockModel) -> str:
    return scheduling_block_summary(block.reason_code, block.reason_details)


def _latest_session_ids(
    rows: Sequence[ExecutionSessionModel],
) -> dict[UUID, UUID]:
    result: dict[UUID, UUID] = {}
    for row in rows:
        result.setdefault(row.execution_id, row.id)
    return result


def _as_utc(value: datetime) -> datetime:
    return (
        value.astimezone(UTC)
        if value.tzinfo is not None
        else value.replace(tzinfo=UTC)
    )
