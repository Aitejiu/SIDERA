from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.sql.elements import ColumnElement

from sidera_control_plane.application.subtask_workspace_queries import (
    SubtaskWorkspaceQuerySnapshot,
)
from sidera_control_plane.domain.plan_normalization import PlanNormalizer
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.scheduling import scheduling_block_summary
from sidera_control_plane.domain.subtask_projections import (
    SubtaskActivityFact,
    SubtaskArtifactFact,
    SubtaskCheckpointFact,
    SubtaskEvidenceFact,
    SubtaskExecutionFact,
    SubtaskPlacementFact,
    SubtaskSessionFact,
    SubtaskTaskFact,
    SubtaskWorkerFact,
    SubtaskWorkerItemFact,
)
from sidera_control_plane.persistence.collaboration_query_store import (
    SqlAlchemyCollaborationQueryStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
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
    WorkerModel,
    WorkspaceProjectionCursorModel,
)
from sidera_control_plane.persistence.plan_convergence_query import (
    load_plan_convergence_facts,
)


class SqlAlchemySubtaskWorkspaceQueryStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._normalizer = PlanNormalizer()
        self._collaboration_store = SqlAlchemyCollaborationQueryStore(database)

    def load(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        subtask_key: str,
    ) -> SubtaskWorkspaceQuerySnapshot | None:
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
            subtask = next(
                (item for item in plan.subtasks if item.key == subtask_key), None
            )
            if subtask is None:
                return None

            task_ids = {
                item.task_id for item in plan.subtasks if item.task_id
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
                    WorkerItemPlacementModel.subtask_key == subtask_key,
                )
                .order_by(WorkerItemPlacementModel.worker_item_key.asc())
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

            execution_filters: list[ColumnElement[bool]] = [
                ExecutionModel.worker_item_id.in_(
                    worker_item_ids or {UUID(int=0)}
                )
            ]
            if subtask.task_id is not None:
                execution_filters.append(ExecutionModel.task_id == subtask.task_id)
            execution_rows = session.scalars(
                select(ExecutionModel)
                .where(
                    ExecutionModel.workspace_id == workspace_id,
                    or_(*execution_filters),
                )
                .order_by(ExecutionModel.updated_at.asc(), ExecutionModel.id.asc())
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
            session_facts = tuple(_session(row) for row in session_rows)
            session_by_execution = _latest_session_ids(session_facts)
            executions = tuple(
                _execution(
                    row,
                    session_by_execution.get(row.id),
                    active_blocks.get(row.id),
                )
                for row in execution_rows
            )

            worker_ids = {
                worker_id
                for row in execution_rows
                for worker_id in (row.assigned_worker_id, row.pinned_worker_id)
                if worker_id
            }
            worker_ids.update(item.worker_id for item in session_facts)
            worker_rows = session.scalars(
                select(WorkerModel).where(
                    WorkerModel.workspace_id == workspace_id,
                    WorkerModel.id.in_(worker_ids or {UUID(int=0)}),
                )
            ).all()
            workers = tuple(_worker(row) for row in worker_rows)

            subtask_task_id = subtask.task_id or UUID(int=0)
            artifact_rows = session.scalars(
                select(ArtifactModel)
                .where(
                    ArtifactModel.workspace_id == workspace_id,
                    ArtifactModel.task_id == subtask_task_id,
                )
                .order_by(ArtifactModel.created_at.asc(), ArtifactModel.id.asc())
            ).all()
            artifacts = tuple(_artifact(row) for row in artifact_rows)
            evidence_rows = session.scalars(
                select(EvidenceClaimModel)
                .where(
                    EvidenceClaimModel.workspace_id == workspace_id,
                    EvidenceClaimModel.task_id == subtask_task_id,
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
            activity: list[SubtaskActivityFact] = []
            checkpoints: list[SubtaskCheckpointFact] = []
            for row in event_rows:
                execution = execution_by_id.get(row.execution_id)
                if execution is None:
                    continue
                payload = dict(row.payload)
                summary = str(
                    payload.get("summary") or payload.get("title") or row.event_type
                )
                activity.append(
                    SubtaskActivityFact(
                        id=row.id,
                        task_id=execution.task_id,
                        worker_item_id=execution.worker_item_id,
                        occurred_at=_as_utc(row.occurred_at),
                        kind=row.event_type,
                        summary=summary,
                        correlation_id=row.id,
                    )
                )
                if row.event_type == "checkpoint.created":
                    checkpoints.append(
                        SubtaskCheckpointFact(
                            execution_id=row.execution_id,
                            sequence=row.sequence,
                            title=summary,
                            artifact_ids=tuple(
                                _uuid_values(payload.get("artifact_ids", ()))
                            ),
                            occurred_at=_as_utc(row.occurred_at),
                        )
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
                snapshot.published_at,
                *(fact.occurred_at for fact in activity),
                *(fact.updated_at for fact in worker_items.values()),
                *(fact.updated_at for fact in tasks.values()),
            )
            convergence_facts = load_plan_convergence_facts(
                session,
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                plan=plan,
            )
            return SubtaskWorkspaceQuerySnapshot(
                workspace_id=workspace_id,
                plan=plan,
                tasks=tasks,
                worker_items=worker_items,
                placements=placements,
                executions=executions,
                sessions=session_facts,
                workers=workers,
                artifacts=artifacts,
                evidence=evidence,
                checkpoints=tuple(checkpoints),
                activity=tuple(activity),
                last_change_cursor=int(last_cursor),
                as_of=as_of,
                convergence_facts=convergence_facts,
                collaboration_facts=self._collaboration_store.load_in_session(
                    session,
                    workspace_id=workspace_id,
                    root_task_id=root_task_id,
                    plan=plan,
                ),
            )


def _plan(row: PlanSnapshotModel) -> TaskPlanSnapshot:
    snapshot = TaskPlanSnapshot.from_payload(dict(row.payload))
    if snapshot.id != row.id or snapshot.generation != row.generation:
        raise ValueError("task plan snapshot payload does not match identity columns")
    return snapshot


def _task(row: TaskModel) -> SubtaskTaskFact:
    return SubtaskTaskFact(
        id=row.id,
        title=row.title,
        status=row.status,
        version=row.version,
        updated_at=_as_utc(row.updated_at),
    )


def _worker_item(row: WorkerItemModel) -> SubtaskWorkerItemFact:
    return SubtaskWorkerItemFact(
        id=row.id,
        stable_key=row.stable_key,
        title=row.title,
        responsibility=row.responsibility,
        status=row.status,
        context_version=row.context_version,
        updated_at=_as_utc(row.updated_at),
    )


def _placement(row: WorkerItemPlacementModel) -> SubtaskPlacementFact:
    return SubtaskPlacementFact(
        plan_snapshot_id=row.plan_snapshot_id,
        plan_generation=row.plan_generation,
        worker_item_id=row.worker_item_id,
        worker_item_key=row.worker_item_key,
        subtask_key=row.subtask_key,
        subtask_task_id=row.subtask_task_id,
        role=row.role,
    )


def _execution(
    row: ExecutionModel,
    session_id: UUID | None,
    block: SchedulingBlockModel | None,
) -> SubtaskExecutionFact:
    return SubtaskExecutionFact(
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
            scheduling_block_summary(block.reason_code, block.reason_details)
            if block is not None
            else None
        ),
    )


def _session(row: ExecutionSessionModel) -> SubtaskSessionFact:
    return SubtaskSessionFact(
        id=row.id,
        execution_id=row.execution_id,
        worker_id=row.worker_id,
        status=row.status,
        last_activity_at=_as_utc(row.last_activity_at),
        version=row.version,
    )


def _worker(row: WorkerModel) -> SubtaskWorkerFact:
    return SubtaskWorkerFact(
        id=row.id,
        display_name=row.display_name,
        adapter_type=row.adapter_type,
    )


def _artifact(row: ArtifactModel) -> SubtaskArtifactFact:
    return SubtaskArtifactFact(
        id=row.id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        artifact_type=row.artifact_type,
        media_type=row.media_type,
        size_bytes=row.size_bytes,
        content_available=row.storage_ref.startswith("artifact-local://"),
    )


def _evidence(row: EvidenceClaimModel) -> SubtaskEvidenceFact:
    return SubtaskEvidenceFact(
        id=row.id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        acceptance_ref=row.acceptance_criterion_ref,
        conclusion=row.conclusion,
        artifact_ids=tuple(_uuid_values(row.artifact_ids)),
    )


def _latest_session_ids(
    values: Sequence[SubtaskSessionFact],
) -> dict[UUID, UUID]:
    grouped: dict[UUID, list[SubtaskSessionFact]] = defaultdict(list)
    for value in values:
        grouped[value.execution_id].append(value)
    return {
        execution_id: max(
            sessions,
            key=lambda item: (item.last_activity_at, item.id),
        ).id
        for execution_id, sessions in grouped.items()
    }


def _uuid_values(values: Any) -> tuple[UUID, ...]:
    result: list[UUID] = []
    for value in values or ():
        try:
            result.append(value if isinstance(value, UUID) else UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return tuple(result)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
