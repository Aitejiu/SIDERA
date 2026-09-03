from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text

from sidera_control_plane.application.worker_workspace_queries import (
    WorkerWorkspaceQuerySnapshot,
)
from sidera_control_plane.domain.plan_normalization import PlanNormalizer
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.worker_item_projections import (
    ArtifactSummaryProjection,
    BindingFact,
    EvidenceSummaryProjection,
    ExecutionFact,
    InputFact,
    LeaseFact,
    SessionFact,
    WorkerEventFact,
    WorkerFact,
    WorkerItemFact,
    WorkerItemPlacementFact,
)
from sidera_control_plane.persistence.collaboration_query_store import (
    SqlAlchemyCollaborationQueryStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.handoff_query_store import (
    SqlAlchemyHandoffQueryStore,
)
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    AssignmentLeaseModel,
    EvidenceClaimModel,
    ExecutionBindingModel,
    ExecutionEventModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionSessionModel,
    PlanSnapshotModel,
    ProjectionChangeModel,
    RootTaskPlanHeadModel,
    SchedulingBlockModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
    WorkerModel,
    WorkspaceProjectionCursorModel,
)
from sidera_control_plane.persistence.plan_convergence_query import (
    load_plan_convergence_facts,
)


class SqlAlchemyWorkerWorkspaceQueryStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._handoff_store = SqlAlchemyHandoffQueryStore(database)
        self._normalizer = PlanNormalizer()
        self._collaboration_store = SqlAlchemyCollaborationQueryStore(database)

    def load(
        self, workspace_id: UUID, worker_item_id: UUID
    ) -> WorkerWorkspaceQuerySnapshot | None:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            worker_item_row = session.scalar(
                select(WorkerItemModel).where(
                    WorkerItemModel.workspace_id == workspace_id,
                    WorkerItemModel.id == worker_item_id,
                )
            )
            if worker_item_row is None:
                return None
            worker_item = _worker_item(worker_item_row)

            placement_rows = session.scalars(
                select(WorkerItemPlacementModel)
                .where(
                    WorkerItemPlacementModel.workspace_id == workspace_id,
                    WorkerItemPlacementModel.worker_item_id == worker_item_id,
                )
                .order_by(
                    WorkerItemPlacementModel.plan_generation.desc(),
                    WorkerItemPlacementModel.plan_snapshot_id.desc(),
                )
            ).all()
            placements = tuple(_placement(row) for row in placement_rows)

            execution_rows = session.scalars(
                select(ExecutionModel)
                .where(
                    ExecutionModel.workspace_id == workspace_id,
                    ExecutionModel.worker_item_id == worker_item_id,
                )
                .order_by(ExecutionModel.created_at.asc(), ExecutionModel.id.asc())
            ).all()
            execution_ids = {row.id for row in execution_rows}

            active_block_rows = session.scalars(
                select(SchedulingBlockModel).where(
                    SchedulingBlockModel.workspace_id == workspace_id,
                    SchedulingBlockModel.execution_id.in_(
                        execution_ids or {UUID(int=0)}
                    ),
                    SchedulingBlockModel.status == "active",
                )
            ).all()
            active_blocks = {row.execution_id: row for row in active_block_rows}

            session_rows = session.scalars(
                select(ExecutionSessionModel)
                .where(
                    ExecutionSessionModel.workspace_id == workspace_id,
                    ExecutionSessionModel.execution_id.in_(
                        execution_ids or {UUID(int=0)}
                    ),
                )
                .order_by(
                    ExecutionSessionModel.last_activity_at.asc(),
                    ExecutionSessionModel.id.asc(),
                )
            ).all()
            sessions = tuple(_session(row) for row in session_rows)

            binding_rows = session.scalars(
                select(ExecutionBindingModel).where(
                    ExecutionBindingModel.workspace_id == workspace_id,
                    ExecutionBindingModel.execution_id.in_(
                        execution_ids or {UUID(int=0)}
                    ),
                )
            ).all()
            bindings = tuple(_binding(row) for row in binding_rows)

            lease_rows = session.scalars(
                select(AssignmentLeaseModel)
                .where(
                    AssignmentLeaseModel.workspace_id == workspace_id,
                    AssignmentLeaseModel.execution_id.in_(
                        execution_ids or {UUID(int=0)}
                    ),
                )
                .order_by(
                    AssignmentLeaseModel.updated_at.asc(),
                    AssignmentLeaseModel.id.asc(),
                )
            ).all()
            leases = tuple(_lease(row) for row in lease_rows)

            worker_ids = {
                worker_id
                for row in execution_rows
                for worker_id in (row.assigned_worker_id, row.pinned_worker_id)
                if worker_id is not None
            }
            worker_ids.update(item.worker_id for item in sessions)
            worker_ids.update(item.worker_id for item in bindings)
            worker_ids.update(item.worker_id for item in leases)
            worker_rows = session.scalars(
                select(WorkerModel).where(
                    WorkerModel.workspace_id == workspace_id,
                    WorkerModel.id.in_(worker_ids or {UUID(int=0)}),
                )
            ).all()
            workers = tuple(_worker(row) for row in worker_rows)

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
                    ExecutionEventModel.sequence.asc(),
                    ExecutionEventModel.id.asc(),
                )
            ).all()
            events = tuple(_event(row) for row in event_rows)

            input_rows = session.scalars(
                select(ExecutionInputModel)
                .where(
                    ExecutionInputModel.workspace_id == workspace_id,
                    ExecutionInputModel.execution_id.in_(
                        execution_ids or {UUID(int=0)}
                    ),
                )
                .order_by(
                    ExecutionInputModel.updated_at.asc(), ExecutionInputModel.id.asc()
                )
            ).all()
            inputs = tuple(_input(row) for row in input_rows)

            artifact_rows = session.scalars(
                select(ArtifactModel)
                .where(
                    ArtifactModel.workspace_id == workspace_id,
                    ArtifactModel.execution_id.in_(execution_ids or {UUID(int=0)}),
                )
                .order_by(ArtifactModel.created_at.asc(), ArtifactModel.id.asc())
            ).all()
            artifacts = tuple(_artifact(row) for row in artifact_rows)

            evidence_rows = session.scalars(
                select(EvidenceClaimModel)
                .where(
                    EvidenceClaimModel.workspace_id == workspace_id,
                    EvidenceClaimModel.execution_id.in_(execution_ids or {UUID(int=0)}),
                )
                .order_by(
                    EvidenceClaimModel.created_at.asc(), EvidenceClaimModel.id.asc()
                )
            ).all()
            evidence = tuple(_evidence(row) for row in evidence_rows)

            last_cursor = session.scalar(
                select(WorkspaceProjectionCursorModel.last_cursor).where(
                    WorkspaceProjectionCursorModel.workspace_id == workspace_id
                )
            )
            if last_cursor is None:
                last_cursor = (
                    session.scalar(
                        select(ProjectionChangeModel.cursor)
                        .where(ProjectionChangeModel.workspace_id == workspace_id)
                        .order_by(ProjectionChangeModel.cursor.desc())
                        .limit(1)
                    )
                    or 0
                )
            active_handoff = self._handoff_store.load_in_session(
                session,
                workspace_id,
                worker_item_ids=(worker_item_id,),
            ).get(worker_item_id)
            collaboration_facts = None
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
                    PlanSnapshotModel.root_task_id == worker_item.root_task_id,
                )
            )
            if plan_row is not None:
                plan = self._normalizer.normalize(_plan(plan_row))
                collaboration_facts = self._collaboration_store.load_in_session(
                    session,
                    workspace_id=workspace_id,
                    root_task_id=worker_item.root_task_id,
                    plan=plan,
                )
            governance_timestamps = (
                tuple(value.created_at for value in collaboration_facts.holds)
                + tuple(value.created_at for value in collaboration_facts.proposals)
                + tuple(value.created_at for value in collaboration_facts.resolutions)
                if collaboration_facts is not None
                else ()
            )
            as_of = max(
                (
                    worker_item.updated_at,
                    *(execution.updated_at for execution in execution_rows),
                    *(session.last_activity_at for session in sessions),
                    *(event.occurred_at for event in events),
                    *(item.updated_at for item in inputs),
                    *(artifact.created_at for artifact in artifacts),
                    *(claim.created_at for claim in evidence),
                    *(block.first_seen_at for block in active_block_rows),
                    *(block.last_seen_at for block in active_block_rows),
                    *(
                        (active_handoff.updated_at,)
                        if active_handoff is not None
                        else ()
                    ),
                    *governance_timestamps,
                )
            )
            convergence_facts = load_plan_convergence_facts(
                session,
                workspace_id=workspace_id,
                root_task_id=worker_item.root_task_id,
                worker_item_ids=(worker_item_id,),
            )
            return WorkerWorkspaceQuerySnapshot(
                worker_item=worker_item,
                placements=placements,
                executions=tuple(
                    _execution(row, active_blocks.get(row.id)) for row in execution_rows
                ),
                sessions=sessions,
                workers=workers,
                bindings=bindings,
                leases=leases,
                events=events,
                inputs=inputs,
                artifacts=artifacts,
                evidence=evidence,
                last_change_cursor=int(last_cursor),
                as_of=as_of,
                convergence_facts=convergence_facts,
                active_handoff=active_handoff,
                collaboration_facts=collaboration_facts,
            )


def _plan(row: PlanSnapshotModel) -> TaskPlanSnapshot:
    snapshot = TaskPlanSnapshot.from_payload(dict(row.payload))
    if snapshot.id != row.id or snapshot.generation != row.generation:
        raise ValueError("task plan snapshot payload does not match identity columns")
    return snapshot


def _worker_item(row: WorkerItemModel) -> WorkerItemFact:
    return WorkerItemFact(
        id=row.id,
        workspace_id=row.workspace_id,
        root_task_id=row.root_task_id,
        stable_key=row.stable_key,
        title=row.title,
        responsibility=row.responsibility,
        status=row.status,
        context_version=row.context_version,
        acknowledged_context_version=row.acknowledged_context_version,
        version=row.version,
        updated_at=_as_utc(row.updated_at),
    )


def _placement(row: WorkerItemPlacementModel) -> WorkerItemPlacementFact:
    return WorkerItemPlacementFact(
        plan_snapshot_id=row.plan_snapshot_id,
        plan_generation=row.plan_generation,
        worker_item_id=row.worker_item_id,
        worker_item_key=row.worker_item_key,
        subtask_key=row.subtask_key,
        subtask_task_id=row.subtask_task_id,
        role=row.role,
        capability_requirements=tuple(row.capability_requirements),
    )


def _execution(
    row: ExecutionModel,
    active_block: SchedulingBlockModel | None,
) -> ExecutionFact:
    if row.worker_item_id is None:
        raise ValueError("WorkerItem execution must have worker_item_id")
    return ExecutionFact(
        id=row.id,
        task_id=row.task_id,
        worker_item_id=row.worker_item_id,
        lineage_id=row.lineage_id,
        attempt=row.attempt,
        status=row.status,
        failure_code=row.failure_code,
        assigned_worker_id=row.assigned_worker_id,
        pinned_worker_id=row.pinned_worker_id,
        active_lease_id=row.active_lease_id,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        started_at=_as_utc_optional(row.started_at),
        finished_at=_as_utc_optional(row.finished_at),
        version=row.version,
        capability_requirements=tuple(sorted(row.capability_requirements or ())),
        adapter_preferences=tuple(row.adapter_preferences or ()),
        adapter_forbidden=tuple(sorted(row.adapter_forbidden or ())),
        scheduling_status=(
            "waiting" if row.status == "queued" and active_block is not None else ""
        ),
        waiting_reason=active_block.reason_code if active_block is not None else None,
        waiting_details_summary=(
            _waiting_details_summary(
                active_block.reason_code,
                active_block.reason_details,
            )
            if active_block is not None
            else None
        ),
        requires_operator=(
            bool(active_block.requires_operator) if active_block is not None else False
        ),
        blocked_since=(
            _as_utc(active_block.first_seen_at) if active_block is not None else None
        ),
    )


def _session(row: ExecutionSessionModel) -> SessionFact:
    return SessionFact(
        id=row.id,
        execution_id=row.execution_id,
        worker_id=row.worker_id,
        adapter_session_id=row.adapter_session_id,
        status=row.status,
        input_mode=row.input_mode,
        version=row.version,
        created_at=_as_utc(row.created_at),
        last_activity_at=_as_utc(row.last_activity_at),
    )


def _worker(row: WorkerModel) -> WorkerFact:
    return WorkerFact(
        id=row.id,
        display_name=row.display_name,
        adapter_type=row.adapter_type,
        status=row.status,
        desired_state=row.desired_state,
        capabilities=tuple(sorted(row.capabilities or ())),
        current_load=row.current_load,
        capacity=row.capacity,
    )


def _binding(row: ExecutionBindingModel) -> BindingFact:
    return BindingFact(
        execution_id=row.execution_id,
        worker_id=row.worker_id,
        lease_id=row.lease_id,
        fencing_token=row.fencing_token,
        updated_at=_as_utc(row.updated_at),
        id=row.id,
        adapter_module_id=row.adapter_module_id,
        workspace_module_id=row.workspace_module_id,
        adapter_config_version=row.adapter_config_version,
        workspace_config_version=row.workspace_config_version,
        version=row.version,
    )


def _lease(row: AssignmentLeaseModel) -> LeaseFact:
    return LeaseFact(
        id=row.id,
        execution_id=row.execution_id,
        worker_id=row.worker_id,
        status=row.status,
        fencing_token=row.fencing_token,
        updated_at=_as_utc(row.updated_at),
    )


def _event(row: ExecutionEventModel) -> WorkerEventFact:
    return WorkerEventFact(
        id=row.id,
        execution_id=row.execution_id,
        session_id=row.session_id,
        sequence=row.sequence,
        event_type=row.event_type,
        payload=dict(row.payload),
        occurred_at=_as_utc(row.occurred_at),
        raw_ref=row.raw_ref,
    )


def _input(row: ExecutionInputModel) -> InputFact:
    return InputFact(
        id=row.id,
        execution_id=row.execution_id,
        session_id=row.session_id,
        text=row.text,
        status=row.status,
        operation_id=row.operation_id,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _artifact(row: ArtifactModel) -> ArtifactSummaryProjection:
    return ArtifactSummaryProjection(
        artifact_id=row.id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        artifact_type=row.artifact_type,
        media_type=row.media_type,
        size_bytes=row.size_bytes,
        content_available=row.storage_ref.startswith("artifact-local://"),
        created_at=_as_utc(row.created_at),
    )


def _evidence(row: EvidenceClaimModel) -> EvidenceSummaryProjection:
    return EvidenceSummaryProjection(
        evidence_id=row.id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        acceptance_ref=row.acceptance_criterion_ref,
        conclusion=row.conclusion,
        artifact_ids=tuple(_uuid_values(row.artifact_ids)),
        created_at=_as_utc(row.created_at),
    )


def _uuid_values(values: object) -> tuple[UUID, ...]:
    if not isinstance(values, (list, tuple, set)):
        return ()
    result: list[UUID] = []
    for value in values:
        try:
            result.append(value if isinstance(value, UUID) else UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return tuple(result)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _as_utc_optional(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None


_WAITING_REASON_LABELS = {
    "no_eligible_worker": "暂无可用执行节点",
    "pinned_worker_unavailable": "固定执行节点不可用",
    "runtime_configuration_missing": "运行环境未配置",
    "runtime_configuration_invalid": "运行环境配置无效",
    "adapter_module_unavailable": "适配器不可用",
    "workspace_module_unavailable": "工作区提供器不可用",
    "runtime_capability_mismatch": "运行能力不匹配",
    "adapter_policy_rejected": "适配器策略已过滤",
}


def _waiting_details_summary(
    reason: str | None, details: Mapping[str, object] | None
) -> str:
    if reason is None:
        return "执行暂时无法分配"
    safe_details = details if isinstance(details, Mapping) else {}
    candidate_count = safe_details.get("candidate_count")
    if not isinstance(candidate_count, int) or isinstance(candidate_count, bool):
        candidate_count = None

    raw_counts = safe_details.get("candidate_issue_counts")
    counts: Mapping[str, object]
    if isinstance(raw_counts, Mapping):
        counts = raw_counts
    else:
        counts = {
            key: safe_details[key]
            for key in _WAITING_REASON_LABELS
            if key in safe_details
        }
    parts = [
        f"{_WAITING_REASON_LABELS.get(key, '其他原因')} {value} 个"
        for key, value in sorted(counts.items())
        if key in _WAITING_REASON_LABELS
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    ]
    if candidate_count is not None and parts:
        return f"{candidate_count} 个候选执行节点暂不可用：" + "，".join(parts)
    if candidate_count is not None and reason == "no_eligible_worker":
        return f"{candidate_count} 个候选执行节点暂不可用"
    if candidate_count is not None and reason == "runtime_configuration_missing":
        return f"{candidate_count} 个候选执行节点未配置运行环境"
    return _WAITING_REASON_LABELS.get(reason, "执行暂时无法分配")
