from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select, text

from sidera_control_plane.application.task_first_queries import (
    ActivityFact,
    ArtifactFact,
    BindingFact,
    CheckpointFact,
    EvidenceFact,
    ExecutionFact,
    LeaseFact,
    SessionFact,
    TaskFact,
    WorkerFact,
)
from sidera_control_plane.application.worker_trace_queries import RawTraceFact
from sidera_control_plane.domain.attention import AttentionAcknowledgement
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    AssignmentLeaseModel,
    AttentionAcknowledgementModel,
    EvidenceClaimModel,
    ExecutionBindingModel,
    ExecutionEventModel,
    ExecutionModel,
    ExecutionSessionModel,
    PlanSnapshotModel,
    ProjectionChangeModel,
    TaskModel,
    WorkerModel,
    WorkspaceProjectionCursorModel,
)


@dataclass(frozen=True, slots=True)
class TaskFirstQuerySnapshot:
    workspace_id: UUID
    plan: TaskPlanSnapshot
    tasks: dict[UUID, TaskFact]
    executions: dict[UUID, tuple[ExecutionFact, ...]]
    leases: dict[UUID, LeaseFact]
    bindings: dict[UUID, BindingFact]
    sessions: dict[UUID, SessionFact]
    workers: dict[UUID, WorkerFact]
    artifacts: dict[UUID, tuple[ArtifactFact, ...]]
    evidence: dict[UUID, tuple[EvidenceFact, ...]]
    checkpoints: dict[UUID, tuple[CheckpointFact, ...]]
    activity: tuple[ActivityFact, ...]
    acknowledgements: tuple[AttentionAcknowledgement, ...]
    last_change_cursor: int
    as_of: datetime


@dataclass(frozen=True, slots=True)
class WorkerTraceQuerySnapshot:
    workspace_id: UUID
    root_task_id: UUID
    execution_id: UUID
    session_id: UUID
    worker_id: UUID | None
    status: str
    lineage_id: UUID
    plan_snapshot_id: UUID | None
    plan_generation: int | None
    work_item_key: str | None
    worker_slot_key: str | None
    events: tuple[RawTraceFact, ...]
    artifact_refs: tuple[UUID, ...]
    last_change_cursor: int
    previous_before_sequence: int | None


class TaskFirstQueryStore(Protocol):
    def load(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        actor_type: str | None = None,
        actor_id: str | None = None,
    ) -> TaskFirstQuerySnapshot | None: ...

    def list_current_root_ids(self, workspace_id: UUID) -> tuple[UUID, ...]: ...

    def get_plan(
        self, workspace_id: UUID, root_task_id: UUID, generation: int
    ) -> TaskPlanSnapshot | None: ...

    def list_executions_for_tasks(
        self, workspace_id: UUID, task_ids: set[UUID]
    ) -> dict[UUID, tuple[ExecutionFact, ...]]: ...

    def load_trace(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        execution_id: UUID,
        session_id: UUID,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> WorkerTraceQuerySnapshot | None: ...


class SqlAlchemyTaskFirstQueryStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def load(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        actor_type: str | None = None,
        actor_id: str | None = None,
    ) -> TaskFirstQuerySnapshot | None:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            plan_row = session.scalar(
                select(PlanSnapshotModel)
                .where(
                    PlanSnapshotModel.workspace_id == workspace_id,
                    PlanSnapshotModel.root_task_id == root_task_id,
                )
                .order_by(PlanSnapshotModel.generation.desc())
                .limit(1)
            )
            if plan_row is None:
                return None
            plan = _plan(plan_row)
            task_ids = {item.task_id for item in plan.work_items if item.task_id}
            task_ids.add(root_task_id)
            task_rows = session.scalars(
                select(TaskModel).where(
                    TaskModel.workspace_id == workspace_id,
                    TaskModel.id.in_(task_ids),
                )
            ).all()
            tasks = {row.id: _task(row) for row in task_rows}

            execution_rows = session.scalars(
                select(ExecutionModel)
                .where(
                    ExecutionModel.workspace_id == workspace_id,
                    ExecutionModel.task_id.in_(task_ids),
                )
                .order_by(ExecutionModel.created_at.asc(), ExecutionModel.id.asc())
            ).all()
            execution_ids = {row.id for row in execution_rows}

            lease_rows = session.scalars(
                select(AssignmentLeaseModel)
                .where(
                    AssignmentLeaseModel.workspace_id == workspace_id,
                    AssignmentLeaseModel.execution_id.in_(execution_ids),
                )
                .order_by(
                    AssignmentLeaseModel.fencing_token.desc(),
                    AssignmentLeaseModel.id.desc(),
                )
            ).all()
            leases = _latest_leases(lease_rows)

            binding_rows = session.scalars(
                select(ExecutionBindingModel).where(
                    ExecutionBindingModel.workspace_id == workspace_id,
                    ExecutionBindingModel.execution_id.in_(execution_ids),
                )
            ).all()
            bindings = {row.execution_id: _binding(row) for row in binding_rows}

            session_rows = session.scalars(
                select(ExecutionSessionModel)
                .where(
                    ExecutionSessionModel.workspace_id == workspace_id,
                    ExecutionSessionModel.execution_id.in_(execution_ids),
                )
                .order_by(
                    ExecutionSessionModel.last_activity_at.desc(),
                    ExecutionSessionModel.id.desc(),
                )
            ).all()
            sessions = {row.id: _session(row) for row in session_rows}
            session_by_execution = _latest_sessions(session_rows)
            executions = _group_executions(
                execution_rows,
                session_by_execution=session_by_execution,
            )

            worker_ids = {
                worker_id
                for row in execution_rows
                for worker_id in (row.assigned_worker_id, row.pinned_worker_id)
                if worker_id
            }
            worker_ids.update(lease.worker_id for lease in leases.values())
            worker_ids.update(binding.worker_id for binding in bindings.values())
            worker_ids.update(item.worker_id for item in sessions.values())
            worker_rows = session.scalars(
                select(WorkerModel).where(
                    WorkerModel.workspace_id == workspace_id,
                    WorkerModel.id.in_(worker_ids or {UUID(int=0)}),
                )
            ).all()
            workers = {row.id: _worker(row) for row in worker_rows}

            artifact_rows = session.scalars(
                select(ArtifactModel)
                .where(
                    ArtifactModel.workspace_id == workspace_id,
                    ArtifactModel.task_id.in_(task_ids),
                )
                .order_by(ArtifactModel.created_at.asc(), ArtifactModel.id.asc())
            ).all()
            artifacts = _group_by_task(artifact_rows, _artifact)

            evidence_rows = session.scalars(
                select(EvidenceClaimModel)
                .where(
                    EvidenceClaimModel.workspace_id == workspace_id,
                    EvidenceClaimModel.task_id.in_(task_ids),
                )
                .order_by(EvidenceClaimModel.created_at.asc(), EvidenceClaimModel.id.asc())
            ).all()
            evidence = _group_by_task(evidence_rows, _evidence)

            event_rows = session.scalars(
                select(ExecutionEventModel)
                .where(
                    ExecutionEventModel.workspace_id == workspace_id,
                    ExecutionEventModel.execution_id.in_(execution_ids),
                )
                .order_by(
                    ExecutionEventModel.occurred_at.asc(),
                    ExecutionEventModel.id.asc(),
                )
            ).all()
            task_by_execution = {
                row.id: row.task_id for row in execution_rows
            }
            activity, checkpoints = _events(
                event_rows,
                task_by_execution=task_by_execution,
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
                    item.occurred_at
                    for item in activity
                ),
                default=plan.published_at,
            )
            acknowledgements: tuple[AttentionAcknowledgement, ...] = ()
            if actor_type is not None and actor_id is not None:
                acknowledgement_rows = session.scalars(
                    select(AttentionAcknowledgementModel).where(
                        AttentionAcknowledgementModel.workspace_id == workspace_id,
                        AttentionAcknowledgementModel.root_task_id == root_task_id,
                        AttentionAcknowledgementModel.actor_type == actor_type,
                        AttentionAcknowledgementModel.actor_id == actor_id,
                    )
                ).all()
                acknowledgements = tuple(
                    _attention_acknowledgement(row)
                    for row in acknowledgement_rows
                )
            return TaskFirstQuerySnapshot(
                workspace_id=workspace_id,
                plan=plan,
                tasks=tasks,
                executions=executions,
                leases=leases,
                bindings=bindings,
                sessions=sessions,
                workers=workers,
                artifacts=artifacts,
                evidence=evidence,
                checkpoints=checkpoints,
                activity=activity,
                acknowledgements=acknowledgements,
                last_change_cursor=int(last_cursor),
                as_of=as_of,
            )

    def list_current_root_ids(self, workspace_id: UUID) -> tuple[UUID, ...]:
        with self._database.session() as session:
            rows = session.execute(
                select(
                    PlanSnapshotModel.root_task_id,
                    PlanSnapshotModel.generation,
                )
                .where(PlanSnapshotModel.workspace_id == workspace_id)
                .order_by(
                    PlanSnapshotModel.root_task_id.asc(),
                    PlanSnapshotModel.generation.desc(),
                )
            ).all()
        seen: set[UUID] = set()
        result: list[UUID] = []
        for root_task_id, _generation in rows:
            if root_task_id not in seen:
                seen.add(root_task_id)
                result.append(root_task_id)
        return tuple(result)

    def get_plan(
        self, workspace_id: UUID, root_task_id: UUID, generation: int
    ) -> TaskPlanSnapshot | None:
        with self._database.session() as session:
            row = session.scalar(
                select(PlanSnapshotModel).where(
                    PlanSnapshotModel.workspace_id == workspace_id,
                    PlanSnapshotModel.root_task_id == root_task_id,
                    PlanSnapshotModel.generation == generation,
                )
            )
        return _plan(row) if row is not None else None

    def list_executions_for_tasks(
        self, workspace_id: UUID, task_ids: set[UUID]
    ) -> dict[UUID, tuple[ExecutionFact, ...]]:
        if not task_ids:
            return {}
        with self._database.session() as session:
            rows = session.scalars(
                select(ExecutionModel)
                .where(
                    ExecutionModel.workspace_id == workspace_id,
                    ExecutionModel.task_id.in_(task_ids),
                )
                .order_by(ExecutionModel.created_at.asc(), ExecutionModel.id.asc())
            ).all()
        return _group_executions(rows)

    def load_trace(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        execution_id: UUID,
        session_id: UUID,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> WorkerTraceQuerySnapshot | None:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            execution = session.scalar(
                select(ExecutionModel).where(
                    ExecutionModel.workspace_id == workspace_id,
                    ExecutionModel.id == execution_id,
                    ExecutionModel.task_id.in_(
                        select(TaskModel.id).where(
                            TaskModel.workspace_id == workspace_id
                        )
                    ),
                )
            )
            if execution is None:
                return None
            session_row = session.scalar(
                select(ExecutionSessionModel).where(
                    ExecutionSessionModel.workspace_id == workspace_id,
                    ExecutionSessionModel.id == session_id,
                    ExecutionSessionModel.execution_id == execution_id,
                )
            )
            if session_row is None:
                return None
            plan_row = session.scalar(
                select(PlanSnapshotModel)
                .where(
                    PlanSnapshotModel.workspace_id == workspace_id,
                    PlanSnapshotModel.root_task_id == root_task_id,
                )
                .order_by(PlanSnapshotModel.generation.desc())
                .limit(1)
            )
            plan = _plan(plan_row) if plan_row is not None else None
            work_item_key: str | None = None
            worker_slot_key: str | None = None
            if plan is not None:
                item = next(
                    (
                        item
                        for item in plan.work_items
                        if item.task_id == execution.task_id
                    ),
                    None,
                )
                if item is not None:
                    work_item_key = item.key
                    worker_slot_key = item.worker_slot_key
            statement = select(ExecutionEventModel).where(
                ExecutionEventModel.workspace_id == workspace_id,
                ExecutionEventModel.execution_id == execution_id,
                ExecutionEventModel.session_id == session_id,
            )
            if before_sequence is not None:
                statement = statement.where(
                    ExecutionEventModel.sequence < before_sequence
                )
            event_rows = session.scalars(
                statement.order_by(ExecutionEventModel.sequence.desc()).limit(limit + 1)
            ).all()
            selected_rows = list(event_rows[:limit])
            selected_rows.reverse()
            previous_before = (
                min(row.sequence for row in selected_rows)
                if len(event_rows) > limit and selected_rows
                else None
            )
            facts = tuple(
                RawTraceFact(
                    schema_version=1,
                    workspace_id=workspace_id,
                    root_task_id=root_task_id,
                    plan_snapshot_id=plan.id if plan else None,
                    work_item_key=work_item_key,
                    execution_id=execution_id,
                    session_id=session_id,
                    sequence=row.sequence,
                    occurred_at=row.occurred_at,
                    event_type=row.event_type,
                    status=execution.status,
                    payload=dict(row.payload),
                    provider_payload_ref=None,
                )
                for row in selected_rows
            )
            artifact_rows = session.scalars(
                select(ArtifactModel.id)
                .where(
                    ArtifactModel.workspace_id == workspace_id,
                    ArtifactModel.execution_id == execution_id,
                )
                .order_by(ArtifactModel.created_at.asc(), ArtifactModel.id.asc())
            ).all()
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
            return WorkerTraceQuerySnapshot(
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                execution_id=execution_id,
                session_id=session_id,
                worker_id=session_row.worker_id or execution.assigned_worker_id,
                status=execution.status,
                lineage_id=execution.lineage_id,
                plan_snapshot_id=plan.id if plan else None,
                plan_generation=plan.generation if plan else None,
                work_item_key=work_item_key,
                worker_slot_key=worker_slot_key,
                events=facts,
                artifact_refs=tuple(artifact_rows),
                last_change_cursor=int(last_cursor),
                previous_before_sequence=previous_before,
            )


def _plan(row: PlanSnapshotModel) -> TaskPlanSnapshot:
    snapshot = TaskPlanSnapshot.from_payload(dict(row.payload))
    if snapshot.id != row.id or snapshot.generation != row.generation:
        raise ValueError("task plan snapshot payload does not match identity columns")
    return snapshot


def _attention_acknowledgement(
    row: AttentionAcknowledgementModel,
) -> AttentionAcknowledgement:
    return AttentionAcknowledgement(
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


def _task(row: TaskModel) -> TaskFact:
    return TaskFact(
        id=row.id,
        workspace_id=row.workspace_id,
        title=row.title,
        acceptance_criteria=tuple(row.acceptance_criteria),
        status=row.status,
        version=row.version,
        updated_at=row.updated_at,
    )


def _group_executions(
    rows: Sequence[ExecutionModel],
    *,
    session_by_execution: dict[UUID, UUID] | None = None,
) -> dict[UUID, tuple[ExecutionFact, ...]]:
    grouped: dict[UUID, list[ExecutionFact]] = defaultdict(list)
    for row in rows:
        grouped[row.task_id].append(
            ExecutionFact(
                id=row.id,
                task_id=row.task_id,
                status=row.status,
                attempt=row.attempt,
                session_id=(
                    session_by_execution.get(row.id) if session_by_execution else None
                ),
                assigned_worker_id=row.assigned_worker_id,
                started_at=row.started_at,
                finished_at=row.finished_at,
                updated_at=row.updated_at,
                capability_requirements=tuple(sorted(row.capability_requirements)),
                version=row.version,
                lineage_id=row.lineage_id,
                source_execution_id=row.source_execution_id,
                source_session_id=row.source_session_id,
            )
        )
    return {key: tuple(value) for key, value in grouped.items()}


def _latest_leases(rows: Sequence[AssignmentLeaseModel]) -> dict[UUID, LeaseFact]:
    result: dict[UUID, LeaseFact] = {}
    for row in rows:
        result.setdefault(row.execution_id, _lease(row))
    return result


def _lease(row: AssignmentLeaseModel) -> LeaseFact:
    return LeaseFact(
        id=row.id,
        execution_id=row.execution_id,
        status=row.status,
        worker_id=row.worker_id,
        version=row.version,
        fencing_token=row.fencing_token,
    )


def _binding(row: ExecutionBindingModel) -> BindingFact:
    return BindingFact(
        execution_id=row.execution_id,
        worker_id=row.worker_id,
        version=row.version,
        lease_id=row.lease_id,
        fencing_token=row.fencing_token,
    )


def _session(row: ExecutionSessionModel) -> SessionFact:
    return SessionFact(
        id=row.id,
        execution_id=row.execution_id,
        worker_id=row.worker_id,
        status=row.status,
        last_activity_at=row.last_activity_at,
        version=row.version,
    )


def _latest_sessions(rows: Sequence[ExecutionSessionModel]) -> dict[UUID, UUID]:
    result: dict[UUID, UUID] = {}
    for row in rows:
        result.setdefault(row.execution_id, row.id)
    return result


def _worker(row: WorkerModel) -> WorkerFact:
    return WorkerFact(
        id=row.id,
        display_name=row.display_name,
        capabilities=tuple(sorted(row.capabilities)),
    )


def _artifact(row: ArtifactModel) -> ArtifactFact:
    return ArtifactFact(
        id=row.id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        artifact_type=row.artifact_type,
        media_type=row.media_type,
        size_bytes=row.size_bytes,
        content_available=row.storage_ref.startswith("artifact-local://"),
        created_at=row.created_at,
    )


def _evidence(row: EvidenceClaimModel) -> EvidenceFact:
    return EvidenceFact(
        id=row.id,
        task_id=row.task_id,
        acceptance_ref=row.acceptance_criterion_ref,
        created_at=row.created_at,
        conclusion=row.conclusion,
        artifact_ids=tuple(_uuid_values(row.artifact_ids)),
        execution_id=row.execution_id,
    )


def _group_by_task(rows: Sequence[Any], converter: Any) -> dict[UUID, tuple[Any, ...]]:
    grouped: dict[UUID, list[Any]] = defaultdict(list)
    for row in rows:
        value = converter(row)
        grouped[value.task_id].append(value)
    return {key: tuple(value) for key, value in grouped.items()}


def _events(
    rows: Sequence[ExecutionEventModel], *, task_by_execution: dict[UUID, UUID]
) -> tuple[tuple[ActivityFact, ...], dict[UUID, tuple[CheckpointFact, ...]]]:
    activity: list[ActivityFact] = []
    checkpoints: dict[UUID, list[CheckpointFact]] = defaultdict(list)
    for row in rows:
        task_id = task_by_execution[row.execution_id]
        payload = dict(row.payload)
        summary = str(payload.get("summary") or payload.get("title") or row.event_type)
        activity.append(
            ActivityFact(
                id=row.id,
                task_id=task_id,
                source_type="execution_event",
                occurred_at=row.occurred_at,
                kind=_activity_kind(row.event_type),
                title=row.event_type,
                summary=summary,
                correlation_id=row.id,
            )
        )
        if row.event_type == "checkpoint.created":
            checkpoints[row.execution_id].append(
                CheckpointFact(
                    execution_id=row.execution_id,
                    sequence=row.sequence,
                    title=summary,
                    artifact_ids=tuple(_uuid_values(payload.get("artifact_ids", []))),
                    occurred_at=row.occurred_at,
                )
            )
    return tuple(activity), {key: tuple(value) for key, value in checkpoints.items()}


def _activity_kind(event_type: str) -> str:
    if event_type.startswith("checkpoint"):
        return "checkpoint"
    if event_type.startswith("error"):
        return "error"
    if event_type.startswith("input"):
        return "input"
    if event_type.startswith("tool"):
        return "tool"
    return "assistant_summary"


def _uuid_values(values: Any) -> tuple[UUID, ...]:
    result: list[UUID] = []
    for value in values or ():
        try:
            result.append(value if isinstance(value, UUID) else UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return tuple(result)
