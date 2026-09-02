from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sidera_control_plane.domain.collaboration_projections import (
    SubtaskCollaborationSummaryProjection,
    WorkerItemCollaborationSummaryProjection,
)
from sidera_control_plane.domain.plan_convergence_projections import (
    PlanConvergenceFacts,
    PlanConvergenceProjectionService,
    SubtaskConvergenceProjection,
    WorkerPlanSyncProjection,
)
from sidera_control_plane.domain.plan_normalization import CanonicalTaskPlan
from sidera_control_plane.domain.plans import SubtaskPlanItem


@dataclass(frozen=True, slots=True)
class SubtaskTaskFact:
    id: UUID
    title: str
    status: str
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SubtaskWorkerItemFact:
    id: UUID
    stable_key: str
    title: str
    responsibility: str
    status: str
    context_version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SubtaskPlacementFact:
    plan_snapshot_id: UUID
    plan_generation: int
    worker_item_id: UUID
    worker_item_key: str
    subtask_key: str
    subtask_task_id: UUID | None
    role: str


@dataclass(frozen=True, slots=True)
class SubtaskExecutionFact:
    id: UUID
    task_id: UUID
    worker_item_id: UUID | None
    status: str
    updated_at: datetime
    session_id: UUID | None = None
    assigned_worker_id: UUID | None = None
    version: int = 1
    active_block_id: UUID | None = None
    active_block_version: int | None = None
    active_block_reason: str | None = None
    active_block_requires_operator: bool = False
    active_block_first_seen_at: datetime | None = None
    active_block_summary: str | None = None


@dataclass(frozen=True, slots=True)
class SubtaskSessionFact:
    id: UUID
    execution_id: UUID
    worker_id: UUID
    status: str
    last_activity_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class SubtaskWorkerFact:
    id: UUID
    display_name: str
    adapter_type: str


@dataclass(frozen=True, slots=True)
class SubtaskArtifactFact:
    id: UUID
    task_id: UUID
    execution_id: UUID
    artifact_type: str
    media_type: str
    size_bytes: int
    content_available: bool


@dataclass(frozen=True, slots=True)
class SubtaskEvidenceFact:
    id: UUID
    task_id: UUID
    execution_id: UUID
    acceptance_ref: str
    conclusion: str
    artifact_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class SubtaskCheckpointFact:
    execution_id: UUID
    sequence: int
    title: str
    artifact_ids: tuple[UUID, ...]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class SubtaskActivityFact:
    id: UUID
    task_id: UUID
    worker_item_id: UUID | None
    occurred_at: datetime
    kind: str
    summary: str
    correlation_id: UUID
    source_type: str = "execution_event"


@dataclass(frozen=True, slots=True)
class SubtaskSummaryProjection:
    key: str
    task_id: UUID | None
    kind: str
    title: str
    goal_summary: str
    state: str
    milestone_key: str | None
    acceptance_refs: tuple[str, ...]
    evidence_required: int
    evidence_satisfied: int
    current_action: str | None
    convergence: SubtaskConvergenceProjection | None = None


@dataclass(frozen=True, slots=True)
class RelatedSubtaskProjection:
    subtask_key: str
    title: str
    state: str


@dataclass(frozen=True, slots=True)
class WorkerItemSummaryProjection:
    worker_item_id: UUID
    stable_key: str
    title: str
    responsibility: str
    role: str
    status: str
    context_version: int
    current_step: str | None
    blocker: str | None
    current_agent_summary: str | None
    recent_activity_at: datetime | None
    plan_sync: WorkerPlanSyncProjection | None = None
    collaboration_state: str | None = None
    collaboration_waiting_summary: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerItemGraphEdgeProjection:
    upstream_worker_item_id: UUID
    downstream_worker_item_id: UUID
    kind: str


@dataclass(frozen=True, slots=True)
class WorkerItemGraphProjection:
    worker_item_ids: tuple[UUID, ...]
    edges: tuple[WorkerItemGraphEdgeProjection, ...]


@dataclass(frozen=True, slots=True)
class SubtaskArtifactSummaryProjection:
    artifact_id: UUID
    artifact_type: str
    media_type: str
    size_bytes: int
    content_available: bool


@dataclass(frozen=True, slots=True)
class SubtaskEvidenceSummaryProjection:
    evidence_id: UUID
    acceptance_ref: str
    conclusion: str
    artifact_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class SubtaskCheckpointSummaryProjection:
    execution_id: UUID
    sequence: int
    title: str
    artifact_ids: tuple[UUID, ...]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class SubtaskAttentionProjection:
    key: str
    kind: str
    worker_item_id: UUID | None
    worker_item_title: str | None
    reason: str
    occurred_at: datetime
    waiting_reason: str | None = None
    waiting_details_summary: str | None = None
    requires_operator: bool = False
    blocked_since: datetime | None = None


@dataclass(frozen=True, slots=True)
class SubtaskActivityProjection:
    source_type: str
    source_id: UUID
    occurred_at: datetime
    kind: str
    summary: str
    correlation_id: UUID
    worker_item_id: UUID | None


@dataclass(frozen=True, slots=True)
class SubtaskWorkspaceProjection:
    workspace_id: UUID
    root_task_id: UUID
    task_title: str
    plan_snapshot_id: UUID
    plan_generation: int
    subtask: SubtaskSummaryProjection
    upstream_subtasks: tuple[RelatedSubtaskProjection, ...]
    downstream_subtasks: tuple[RelatedSubtaskProjection, ...]
    worker_items: tuple[WorkerItemSummaryProjection, ...]
    worker_graph: WorkerItemGraphProjection
    artifacts: tuple[SubtaskArtifactSummaryProjection, ...]
    evidence: tuple[SubtaskEvidenceSummaryProjection, ...]
    checkpoints: tuple[SubtaskCheckpointSummaryProjection, ...]
    attention_items: tuple[SubtaskAttentionProjection, ...]
    recent_activity: tuple[SubtaskActivityProjection, ...]
    last_change_cursor: int
    as_of: datetime
    convergence: SubtaskConvergenceProjection | None = None
    collaboration: SubtaskCollaborationSummaryProjection | None = None


class SubtaskWorkspaceProjectionService:
    def __init__(self) -> None:
        self._convergence_service = PlanConvergenceProjectionService()

    def compose(
        self,
        plan: CanonicalTaskPlan,
        *,
        subtask_key: str,
        tasks: Mapping[UUID, SubtaskTaskFact],
        worker_items: Mapping[UUID, SubtaskWorkerItemFact],
        placements: Iterable[SubtaskPlacementFact],
        executions: Iterable[SubtaskExecutionFact],
        sessions: Iterable[SubtaskSessionFact],
        workers: Iterable[SubtaskWorkerFact],
        artifacts: Iterable[SubtaskArtifactFact],
        evidence: Iterable[SubtaskEvidenceFact],
        checkpoints: Iterable[SubtaskCheckpointFact],
        activity: Iterable[SubtaskActivityFact],
        last_change_cursor: int,
        as_of: datetime,
        convergence_facts: PlanConvergenceFacts | None = None,
        collaboration: SubtaskCollaborationSummaryProjection | None = None,
        collaboration_workers: Mapping[UUID, WorkerItemCollaborationSummaryProjection]
        | None = None,
    ) -> SubtaskWorkspaceProjection:
        subtask = _find_subtask(plan, subtask_key)
        task = tasks.get(subtask.task_id) if subtask.task_id else None
        placement_values = tuple(
            placement
            for placement in placements
            if placement.plan_snapshot_id == plan.snapshot_id
            and placement.subtask_key == subtask_key
        )
        execution_values = tuple(executions)
        session_values = tuple(sessions)
        worker_values = {worker.id: worker for worker in workers}
        artifact_values = tuple(
            artifact
            for artifact in artifacts
            if artifact.task_id == subtask.task_id
        )
        evidence_values = tuple(
            claim for claim in evidence if claim.task_id == subtask.task_id
        )
        activity_values = tuple(
            item for item in activity if item.task_id == subtask.task_id
        )
        execution_by_worker_item = _group_executions_by_worker_item(
            execution_values
        )
        session_by_execution = _latest_sessions(session_values)
        activity_by_worker_item = _group_activity_by_worker_item(activity_values)
        activity_by_task = _group_activity_by_task(activity_values)
        canonical_worker_items = {item.key: item for item in plan.worker_items}
        collaboration_worker_values = collaboration_workers or {}
        placement_by_key = {
            placement.worker_item_key: placement for placement in placement_values
        }
        item_states = {
            placement.worker_item_id: _worker_item_state(
                worker_items.get(placement.worker_item_id),
                _latest(
                    execution_by_worker_item.get(placement.worker_item_id, ())
                ),
                task,
            )
            for placement in placement_values
        }
        state_by_subtask = _apply_dependency_state(
            plan,
            _subtask_states(
                plan,
                tasks=tasks,
                current_subtask_key=subtask_key,
                current_state=_subtask_state(
                    subtask,
                    task,
                    tuple(item_states.values()),
                    evidence_satisfied=_evidence_satisfied(subtask, evidence_values),
                    evidence_required=len(subtask.acceptance_refs),
                    task_executions=tuple(
                        execution
                        for execution in execution_values
                        if execution.task_id == subtask.task_id
                    ),
                ),
            ),
        )
        current_state = state_by_subtask[subtask_key]
        convergence = (
            self._convergence_service.compose_subtask(
                plan, subtask_key, convergence_facts
            )
            if convergence_facts is not None
            else None
        )
        latest_task_activity = (
            _latest(activity_by_task.get(subtask.task_id, ()))
            if subtask.task_id is not None
            else None
        )
        current_action = latest_task_activity.summary if latest_task_activity else None
        worker_summaries: list[WorkerItemSummaryProjection] = []
        attention: list[SubtaskAttentionProjection] = []
        for placement in placement_values:
            item = worker_items.get(placement.worker_item_id)
            if item is None:
                continue
            execution = _latest(
                execution_by_worker_item.get(placement.worker_item_id, ())
            )
            session = (
                session_by_execution.get(execution.id) if execution else None
            )
            worker_id = (
                execution.assigned_worker_id
                if execution and execution.assigned_worker_id
                else session.worker_id if session else None
            )
            worker = worker_values.get(worker_id) if worker_id else None
            latest_activity = _latest(
                activity_by_worker_item.get(placement.worker_item_id, ())
            )
            state = item_states[placement.worker_item_id]
            current_agent_summary = worker.display_name if worker else None
            worker_summaries.append(
                WorkerItemSummaryProjection(
                    worker_item_id=item.id,
                    stable_key=item.stable_key,
                    title=item.title,
                    responsibility=item.responsibility,
                    role=(
                        placement.role
                        or canonical_worker_items[placement.worker_item_key].role
                    ),
                    status=state,
                    context_version=item.context_version,
                    current_step=latest_activity.summary if latest_activity else None,
                    blocker=(
                        latest_activity.summary
                        if latest_activity and _is_attention_state(state)
                        else None
                    ),
                    current_agent_summary=current_agent_summary,
                    recent_activity_at=(
                        latest_activity.occurred_at if latest_activity else None
                    ),
                    plan_sync=(
                        self._convergence_service.compose_worker(
                            plan, item.id, convergence_facts
                        )
                        if convergence_facts is not None
                        else None
                    ),
                    collaboration_state=(
                        collaboration_worker_values[item.id].state
                        if item.id in collaboration_worker_values
                        else None
                    ),
                    collaboration_waiting_summary=(
                        collaboration_worker_values[item.id].waiting_summary
                        if item.id in collaboration_worker_values
                        else None
                    ),
                )
            )
            if _is_attention_state(state):
                active_block = _active_scheduling_block(execution)
                attention.append(
                    SubtaskAttentionProjection(
                        key=(
                            f"scheduling_block:{plan.root_task_id}:{subtask.key}:"
                            f"{active_block.active_block_id}"
                            if active_block is not None
                            else f"worker-item:{item.id}:{state}"
                        ),
                        kind=state,
                        worker_item_id=item.id,
                        worker_item_title=item.title,
                        reason=(
                            active_block.active_block_summary
                            if active_block is not None
                            and active_block.active_block_summary is not None
                            else latest_activity.summary
                            if latest_activity
                            else _default_attention_reason(state)
                        ),
                        occurred_at=(
                            active_block.active_block_first_seen_at
                            if active_block is not None
                            and active_block.active_block_first_seen_at is not None
                            else latest_activity.occurred_at
                            if latest_activity
                            else item.updated_at
                        ),
                        waiting_reason=(
                            active_block.active_block_reason
                            if active_block is not None
                            else None
                        ),
                        waiting_details_summary=(
                            active_block.active_block_summary
                            if active_block is not None
                            else None
                        ),
                        requires_operator=(
                            active_block.active_block_requires_operator
                            if active_block is not None
                            else False
                        ),
                        blocked_since=(
                            active_block.active_block_first_seen_at
                            if active_block is not None
                            else None
                        ),
                    )
                )

        if current_state in {"waiting_user", "blocked", "failed"} and not attention:
            attention.append(
                SubtaskAttentionProjection(
                    key=f"subtask:{plan.root_task_id}:{subtask.key}:{current_state}",
                    kind=current_state,
                    worker_item_id=None,
                    worker_item_title=None,
                    reason=current_action or _default_attention_reason(current_state),
                    occurred_at=latest_task_activity.occurred_at
                    if latest_task_activity
                    else as_of,
                )
            )

        worker_graph = _worker_graph(plan, placement_by_key)
        related_upstream, related_downstream = _related_subtasks(
            plan, subtask, state_by_subtask
        )
        subtask_projection = SubtaskSummaryProjection(
            key=subtask.key,
            task_id=subtask.task_id,
            kind=subtask.kind.value,
            title=subtask.title,
            goal_summary=subtask.goal_summary,
            state=current_state,
            milestone_key=subtask.milestone_key,
            acceptance_refs=subtask.acceptance_refs,
            evidence_required=len(subtask.acceptance_refs),
            evidence_satisfied=_evidence_satisfied(subtask, evidence_values),
            current_action=current_action,
            convergence=convergence,
        )
        execution_ids = {execution.id for execution in execution_values}
        checkpoint_values = tuple(
            checkpoint
            for checkpoint in checkpoints
            if checkpoint.execution_id in execution_ids
        )
        recent_activity = tuple(
            SubtaskActivityProjection(
                source_type=item.source_type,
                source_id=item.id,
                occurred_at=item.occurred_at,
                kind=item.kind,
                summary=item.summary,
                correlation_id=item.correlation_id,
                worker_item_id=item.worker_item_id,
            )
            for item in sorted(
                activity_values,
                key=_activity_sort_key,
                reverse=True,
            )[:50]
        )
        return SubtaskWorkspaceProjection(
            workspace_id=plan.workspace_id,
            root_task_id=plan.root_task_id,
            task_title=plan.title,
            plan_snapshot_id=plan.snapshot_id,
            plan_generation=plan.generation,
            subtask=subtask_projection,
            upstream_subtasks=related_upstream,
            downstream_subtasks=related_downstream,
            worker_items=tuple(worker_summaries),
            worker_graph=worker_graph,
            artifacts=tuple(
                SubtaskArtifactSummaryProjection(
                    artifact_id=artifact.id,
                    artifact_type=artifact.artifact_type,
                    media_type=artifact.media_type,
                    size_bytes=artifact.size_bytes,
                    content_available=artifact.content_available,
                )
                for artifact in artifact_values
            ),
            evidence=tuple(
                SubtaskEvidenceSummaryProjection(
                    evidence_id=claim.id,
                    acceptance_ref=claim.acceptance_ref,
                    conclusion=claim.conclusion,
                    artifact_ids=claim.artifact_ids,
                )
                for claim in evidence_values
            ),
            checkpoints=tuple(
                SubtaskCheckpointSummaryProjection(
                    execution_id=checkpoint.execution_id,
                    sequence=checkpoint.sequence,
                    title=checkpoint.title,
                    artifact_ids=checkpoint.artifact_ids,
                    occurred_at=checkpoint.occurred_at,
                )
                for checkpoint in sorted(
                    checkpoint_values,
                    key=lambda value: (value.occurred_at, value.sequence),
                )
            ),
            attention_items=tuple(attention),
            recent_activity=recent_activity,
            last_change_cursor=last_change_cursor,
            as_of=as_of,
            convergence=convergence,
        )


def _find_subtask(plan: CanonicalTaskPlan, key: str) -> SubtaskPlanItem:
    for item in plan.subtasks:
        if item.key == key:
            return item
    raise KeyError(f"unknown subtask: {key}")


def _group_executions_by_worker_item(
    values: Iterable[SubtaskExecutionFact],
) -> dict[UUID, tuple[SubtaskExecutionFact, ...]]:
    grouped: dict[UUID, list[SubtaskExecutionFact]] = defaultdict(list)
    for value in values:
        if value.worker_item_id is not None:
            grouped[value.worker_item_id].append(value)
    return {key: tuple(value) for key, value in grouped.items()}


def _group_activity_by_worker_item(
    values: Iterable[SubtaskActivityFact],
) -> dict[UUID, tuple[SubtaskActivityFact, ...]]:
    grouped: dict[UUID, list[SubtaskActivityFact]] = defaultdict(list)
    for value in values:
        if value.worker_item_id is not None:
            grouped[value.worker_item_id].append(value)
    return {key: tuple(value) for key, value in grouped.items()}


def _group_activity_by_task(
    values: Iterable[SubtaskActivityFact],
) -> dict[UUID, tuple[SubtaskActivityFact, ...]]:
    grouped: dict[UUID, list[SubtaskActivityFact]] = defaultdict(list)
    for value in values:
        grouped[value.task_id].append(value)
    return {key: tuple(value) for key, value in grouped.items()}


def _latest_sessions(
    values: Iterable[SubtaskSessionFact],
) -> dict[UUID, SubtaskSessionFact]:
    grouped: dict[UUID, list[SubtaskSessionFact]] = defaultdict(list)
    for value in values:
        grouped[value.execution_id].append(value)
    return {
        key: _latest(session_values)
        for key, session_values in grouped.items()
        if _latest(session_values) is not None
    }


def _subtask_states(
    plan: CanonicalTaskPlan,
    *,
    tasks: Mapping[UUID, SubtaskTaskFact],
    current_subtask_key: str,
    current_state: str,
) -> dict[str, str]:
    states: dict[str, str] = {}
    for subtask in plan.subtasks:
        if subtask.key == current_subtask_key:
            states[subtask.key] = current_state
            continue
        task = tasks.get(subtask.task_id) if subtask.task_id else None
        states[subtask.key] = _task_state(task)
    return states


def _apply_dependency_state(
    plan: CanonicalTaskPlan, states: Mapping[str, str]
) -> dict[str, str]:
    result = dict(states)
    for dependency in plan.subtask_dependencies:
        if dependency.enforcement.value != "control_plane":
            continue
        if result.get(dependency.upstream_subtask_key) == "succeeded":
            continue
        downstream_state = result.get(dependency.downstream_subtask_key)
        if downstream_state in {"planned", "succeeded"}:
            result[dependency.downstream_subtask_key] = "waiting_dependency"
    return result


def _task_state(task: SubtaskTaskFact | None) -> str:
    if task is None:
        return "planned"
    return {
        "pending": "planned",
        "ready": "planned",
        "running": "running",
        "waiting": "waiting_user",
        "waiting_user": "waiting_user",
        "blocked": "blocked",
        "paused": "paused",
        "succeeded": "succeeded",
        "failed": "failed",
        "canceled": "canceled",
    }.get(task.status, "planned")


def _subtask_state(
    subtask: SubtaskPlanItem,
    task: SubtaskTaskFact | None,
    item_states: tuple[str, ...],
    *,
    evidence_satisfied: int,
    evidence_required: int,
    task_executions: tuple[SubtaskExecutionFact, ...],
) -> str:
    del subtask
    if "waiting_user" in item_states or (
        task and task.status in {"waiting", "waiting_user"}
    ):
        return "waiting_user"
    if "blocked" in item_states or (task and task.status == "blocked"):
        return "blocked"
    if "failed" in item_states or (task and task.status == "failed"):
        return "failed"
    if any(_is_running_state(state) for state in item_states):
        return "running"
    if task and task.status in {"running", "ready"}:
        return "running"
    if (
        (not item_states or all(state in {"succeeded", "canceled"} for state in item_states))
        and task
        and task.status == "succeeded"
        and evidence_satisfied >= evidence_required
    ):
        return "succeeded"
    if not item_states and task_executions:
        execution = _latest(task_executions)
        if execution is not None:
            if execution.status == "waiting_user":
                return "waiting_user"
            if execution.status in {"blocked", "failed"}:
                return execution.status
            if execution.status in {"queued", "leased", "running", "lost"}:
                return "running"
    return "planned"


def _worker_item_state(
    worker_item: SubtaskWorkerItemFact | None,
    execution: SubtaskExecutionFact | None,
    task: SubtaskTaskFact | None,
) -> str:
    if worker_item is not None:
        if worker_item.status == "retired":
            return "retired"
        if worker_item.status == "paused":
            return "paused"
    if execution is not None:
        if execution.status == "queued" and execution.active_block_id is not None:
            return "blocked"
        return {
            "queued": "queued",
            "leased": "assigned",
            "running": "running",
            "waiting_user": "waiting_user",
            "blocked": "blocked",
            "succeeded": "succeeded",
            "failed": "failed",
            "canceled": "canceled",
            "lost": "retrying",
        }.get(execution.status, "queued")
    if worker_item is not None:
        return "planned"
    return _task_state(task)


def _evidence_satisfied(
    subtask: SubtaskPlanItem,
    evidence: Iterable[SubtaskEvidenceFact],
) -> int:
    refs = set(subtask.acceptance_refs)
    return len({claim.acceptance_ref for claim in evidence if claim.acceptance_ref in refs})


def _worker_graph(
    plan: CanonicalTaskPlan,
    placements: Mapping[str, SubtaskPlacementFact],
) -> WorkerItemGraphProjection:
    worker_item_ids = tuple(placement.worker_item_id for placement in placements.values())
    edges: list[WorkerItemGraphEdgeProjection] = []
    for dependency in plan.worker_item_dependencies:
        upstream = placements.get(dependency.upstream_worker_item_key)
        downstream = placements.get(dependency.downstream_worker_item_key)
        if upstream is None or downstream is None:
            continue
        edges.append(
            WorkerItemGraphEdgeProjection(
                upstream_worker_item_id=upstream.worker_item_id,
                downstream_worker_item_id=downstream.worker_item_id,
                kind=dependency.kind,
            )
        )
    return WorkerItemGraphProjection(
        worker_item_ids=worker_item_ids,
        edges=tuple(edges),
    )


def _related_subtasks(
    plan: CanonicalTaskPlan,
    subtask: SubtaskPlanItem,
    states: Mapping[str, str],
) -> tuple[tuple[RelatedSubtaskProjection, ...], tuple[RelatedSubtaskProjection, ...]]:
    by_key = {item.key: item for item in plan.subtasks}
    upstream: list[RelatedSubtaskProjection] = []
    downstream: list[RelatedSubtaskProjection] = []
    for dependency in plan.subtask_dependencies:
        if dependency.downstream_subtask_key == subtask.key:
            item = by_key[dependency.upstream_subtask_key]
            upstream.append(
                RelatedSubtaskProjection(
                    subtask_key=item.key,
                    title=item.title,
                    state=states[item.key],
                )
            )
        if dependency.upstream_subtask_key == subtask.key:
            item = by_key[dependency.downstream_subtask_key]
            downstream.append(
                RelatedSubtaskProjection(
                    subtask_key=item.key,
                    title=item.title,
                    state=states[item.key],
                )
            )
    return tuple(upstream), tuple(downstream)


def _is_running_state(state: str) -> bool:
    return state in {"queued", "assigned", "running", "retrying"}


def _is_attention_state(state: str) -> bool:
    return state in {"waiting_user", "blocked", "failed"}


def _active_scheduling_block(
    execution: SubtaskExecutionFact | None,
) -> SubtaskExecutionFact | None:
    if execution is not None and execution.status == "queued" and execution.active_block_id is not None:
        return execution
    return None


def _default_attention_reason(state: str) -> str:
    return {
        "waiting_user": "等待用户确认",
        "blocked": "WorkerItem 已阻塞",
        "failed": "WorkerItem 执行失败",
    }.get(state, f"WorkerItem 状态为 {state}")


def _latest(values: Iterable[Any]) -> Any:
    return max(values, key=_fact_sort_key, default=None)


def _fact_sort_key(value: object) -> tuple[datetime, UUID | str]:
    timestamp = (
        getattr(value, "updated_at", None)
        or getattr(value, "occurred_at", None)
        or getattr(value, "last_activity_at", None)
    )
    identifier = getattr(value, "id", "")
    return timestamp or datetime.min.replace(tzinfo=UTC), identifier


def _activity_sort_key(value: SubtaskActivityFact) -> tuple[datetime, str, UUID]:
    return value.occurred_at, value.source_type, value.id
