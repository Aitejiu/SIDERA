from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sidera_control_plane.domain.attention import (
    AttentionAcknowledgement,
    AttentionSource,
)
from sidera_control_plane.domain.plan_convergence_projections import (
    PlanConvergenceFacts,
    PlanConvergenceProjection,
    PlanConvergenceProjectionService,
    PlanHeadProjection,
    PlanRevisionAttentionProjection,
    PlanRevisionProjection,
    PlanRootProjection,
)
from sidera_control_plane.domain.plan_normalization import CanonicalTaskPlan
from sidera_control_plane.domain.plans import SubtaskPlanItem


@dataclass(frozen=True, slots=True)
class TaskConsoleTaskFact:
    id: UUID
    title: str
    status: str
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskConsoleWorkerItemFact:
    id: UUID
    stable_key: str
    title: str
    responsibility: str
    status: str
    context_version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskConsolePlacementFact:
    plan_snapshot_id: UUID
    plan_generation: int
    worker_item_id: UUID
    worker_item_key: str
    subtask_key: str
    subtask_task_id: UUID | None


@dataclass(frozen=True, slots=True)
class TaskConsoleExecutionFact:
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
class TaskConsoleEvidenceFact:
    id: UUID
    task_id: UUID
    acceptance_ref: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TaskConsoleActivityFact:
    id: UUID
    task_id: UUID
    worker_item_id: UUID | None
    occurred_at: datetime
    kind: str
    summary: str
    correlation_id: UUID
    source_type: str = "execution_event"


@dataclass(frozen=True, slots=True)
class SubtaskNodeProjection:
    subtask_key: str
    task_id: UUID | None
    kind: str
    title: str
    goal_summary: str
    state: str
    milestone_key: str | None
    worker_item_count: int
    running_worker_item_count: int
    attention_worker_item_count: int
    evidence_required: int
    evidence_satisfied: int
    current_action: str | None
    waiting_reason: str | None = None
    waiting_details_summary: str | None = None
    requires_operator: bool = False
    blocked_since: datetime | None = None


@dataclass(frozen=True, slots=True)
class SubtaskEdgeProjection:
    upstream_subtask_key: str
    downstream_subtask_key: str
    kind: str


@dataclass(frozen=True, slots=True)
class TaskConsoleAttentionProjection:
    key: str
    kind: str
    task_title: str
    subtask_key: str
    subtask_title: str
    reason: str
    resource_id: UUID
    resource_version: int
    correlation_id: UUID
    occurred_at: datetime
    waiting_reason: str | None = None
    waiting_details_summary: str | None = None
    requires_operator: bool = False
    blocked_since: datetime | None = None


@dataclass(frozen=True, slots=True)
class SubtaskMilestoneProjection:
    key: str
    title: str
    state: str
    subtask_count: int
    completed_count: int
    planned_count: int
    order_hint: int


@dataclass(frozen=True, slots=True)
class TaskConsoleOverviewProjection:
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    plan_snapshot_id: UUID
    generation: int
    title: str
    goal_summary: str
    acceptance_refs: tuple[str, ...]
    current_milestone_key: str | None
    subtask_count: int
    planned_subtask_count: int
    running_subtask_count: int
    completed_subtask_count: int
    failed_subtask_count: int
    next_subtask_keys: tuple[str, ...]
    attention_items: tuple[TaskConsoleAttentionProjection, ...]
    last_change_cursor: int
    as_of: datetime
    plan_head: PlanHeadProjection | None = None
    active_revision: PlanRevisionProjection | None = None
    convergence: PlanConvergenceProjection | None = None
    revision_attention: PlanRevisionAttentionProjection | None = None


@dataclass(frozen=True, slots=True)
class TaskConsoleGraphProjection:
    schema_version: int
    overview: TaskConsoleOverviewProjection
    nodes: tuple[SubtaskNodeProjection, ...]
    edges: tuple[SubtaskEdgeProjection, ...]
    milestones: tuple[SubtaskMilestoneProjection, ...]


@dataclass(frozen=True, slots=True)
class TaskConsoleActivityItemProjection:
    source_type: str
    source_id: UUID
    occurred_at: datetime
    kind: str
    title: str
    summary: str
    correlation_id: UUID
    subtask_key: str | None
    subtask_title: str | None


@dataclass(frozen=True, slots=True)
class TaskConsoleActivityProjection:
    schema_version: int
    last_change_cursor: int
    items: tuple[TaskConsoleActivityItemProjection, ...]
    next_cursor: str | None


class TaskConsoleProjectionService:
    """Project a root Task around Subtasks and WorkerItem aggregate facts."""

    def __init__(self) -> None:
        self._convergence_service = PlanConvergenceProjectionService()

    def compose(
        self,
        plan: CanonicalTaskPlan,
        *,
        tasks: Mapping[UUID, TaskConsoleTaskFact],
        worker_items: Mapping[UUID, TaskConsoleWorkerItemFact],
        placements: Iterable[TaskConsolePlacementFact],
        executions: Iterable[TaskConsoleExecutionFact],
        evidence: Iterable[TaskConsoleEvidenceFact],
        activity: Iterable[TaskConsoleActivityFact],
        last_change_cursor: int,
        as_of: datetime,
        acknowledgements: Iterable[AttentionAcknowledgement] = (),
        convergence_facts: PlanConvergenceFacts | None = None,
    ) -> TaskConsoleGraphProjection:
        placement_values = tuple(
            placement
            for placement in placements
            if placement.plan_snapshot_id == plan.snapshot_id
        )
        execution_values = tuple(executions)
        evidence_values = tuple(evidence)
        activity_values = tuple(activity)
        placements_by_subtask = _group_placements(placement_values)
        executions_by_worker_item = _group_executions(execution_values)
        executions_by_task = _group_executions_by_task(execution_values)
        evidence_by_task = _group_evidence_by_task(evidence_values)
        activity_by_task = _group_activity_by_task(activity_values)

        nodes: list[SubtaskNodeProjection] = []
        for subtask in plan.subtasks:
            task_id = subtask.task_id
            task = tasks.get(task_id) if task_id else None
            item_placements = placements_by_subtask.get(subtask.key, ())
            item_states = tuple(
                _worker_item_state(
                    worker_items.get(placement.worker_item_id),
                    _latest(executions_by_worker_item.get(placement.worker_item_id, ())),
                    task,
                )
                for placement in item_placements
            )
            evidence_task_values = evidence_by_task.get(task_id, ()) if task_id else ()
            evidence_satisfied = _evidence_satisfied(
                subtask, evidence_task_values
            )
            state = _subtask_state(
                subtask,
                task,
                item_states,
                evidence_satisfied=evidence_satisfied,
                evidence_required=len(subtask.acceptance_refs),
                task_executions=(
                    executions_by_task.get(task_id, ()) if task_id else ()
                ),
            )
            latest_activity = _latest(
                activity_by_task.get(task_id, ()) if task_id else ()
            )
            task_executions = (
                executions_by_task.get(task_id, ()) if task_id else ()
            )
            latest_block = _latest(
                execution
                for execution in task_executions
                if execution.status == "queued" and execution.active_block_id is not None
            )
            nodes.append(
                SubtaskNodeProjection(
                    subtask_key=subtask.key,
                    task_id=subtask.task_id,
                    kind=subtask.kind.value,
                    title=subtask.title,
                    goal_summary=subtask.goal_summary,
                    state=state,
                    milestone_key=subtask.milestone_key,
                    worker_item_count=len(item_placements),
                    running_worker_item_count=sum(
                        _is_running_state(item_state) for item_state in item_states
                    ),
                    attention_worker_item_count=sum(
                        _is_attention_state(item_state) for item_state in item_states
                    ),
                    evidence_required=len(subtask.acceptance_refs),
                    evidence_satisfied=evidence_satisfied,
                    current_action=(
                        latest_block.active_block_summary
                        if latest_block is not None
                        and latest_block.active_block_summary is not None
                        else latest_activity.summary
                        if latest_activity
                        else None
                    ),
                    waiting_reason=(
                        latest_block.active_block_reason
                        if latest_block is not None
                        else None
                    ),
                    waiting_details_summary=(
                        latest_block.active_block_summary
                        if latest_block is not None
                        else None
                    ),
                    requires_operator=(
                        latest_block.active_block_requires_operator
                        if latest_block is not None
                        else False
                    ),
                    blocked_since=(
                        latest_block.active_block_first_seen_at
                        if latest_block is not None
                        else None
                    ),
                )
            )

        nodes = self._apply_dependency_state(plan, nodes)
        node_by_key = {node.subtask_key: node for node in nodes}
        edges = tuple(
            SubtaskEdgeProjection(
                upstream_subtask_key=dependency.upstream_subtask_key,
                downstream_subtask_key=dependency.downstream_subtask_key,
                kind=dependency.kind.value,
            )
            for dependency in plan.subtask_dependencies
        )
        milestones = self._milestones(plan, node_by_key)
        convergence = (
            self._convergence_service.compose_root(plan, convergence_facts)
            if convergence_facts is not None
            else None
        )
        overview = self._overview(
            plan,
            nodes,
            milestones,
            tasks=tasks,
            activity=activity_values,
            executions=execution_values,
            last_change_cursor=last_change_cursor,
            as_of=as_of,
            acknowledgements=tuple(acknowledgements),
            convergence=convergence,
        )
        return TaskConsoleGraphProjection(
            schema_version=1,
            overview=overview,
            nodes=tuple(nodes),
            edges=edges,
            milestones=milestones,
        )

    def compose_activity(
        self,
        plan: CanonicalTaskPlan,
        activity: Iterable[TaskConsoleActivityFact],
        *,
        last_change_cursor: int,
        after: str | None,
        limit: int,
    ) -> TaskConsoleActivityProjection:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        subtask_titles = {item.key: item.title for item in plan.subtasks}
        subtask_by_task = {
            item.task_id: item.key for item in plan.subtasks if item.task_id
        }
        ordered = sorted(activity, key=_activity_sort_key)
        if after:
            ordered = [item for item in ordered if _activity_cursor(item) > after]
        page = ordered[:limit]
        return TaskConsoleActivityProjection(
            schema_version=1,
            last_change_cursor=last_change_cursor,
            items=tuple(
                TaskConsoleActivityItemProjection(
                    source_type=item.source_type,
                    source_id=item.id,
                    occurred_at=item.occurred_at,
                    kind=item.kind,
                    title=item.kind,
                    summary=item.summary,
                    correlation_id=item.correlation_id,
                    subtask_key=subtask_by_task.get(item.task_id),
                    subtask_title=(
                        subtask_titles.get(subtask_by_task[item.task_id])
                        if item.task_id in subtask_by_task
                        else None
                    ),
                )
                for item in page
            ),
            next_cursor=(
                _activity_cursor(page[-1])
                if len(ordered) > limit and page
                else None
            ),
        )

    @staticmethod
    def _apply_dependency_state(
        plan: CanonicalTaskPlan, nodes: list[SubtaskNodeProjection]
    ) -> list[SubtaskNodeProjection]:
        by_key = {node.subtask_key: node for node in nodes}
        waiting: set[str] = set()
        for dependency in plan.subtask_dependencies:
            if dependency.enforcement.value != "control_plane":
                continue
            upstream = by_key[dependency.upstream_subtask_key]
            if upstream.state != "succeeded":
                waiting.add(dependency.downstream_subtask_key)
        result: list[SubtaskNodeProjection] = []
        for node in nodes:
            if (
                node.subtask_key in waiting
                and node.state in {"planned", "succeeded"}
            ):
                payload = {
                    field: getattr(node, field)
                    for field in node.__dataclass_fields__
                }
                payload["state"] = "waiting_dependency"
                result.append(SubtaskNodeProjection(**payload))
            else:
                result.append(node)
        return result

    @staticmethod
    def _milestones(
        plan: CanonicalTaskPlan,
        nodes: Mapping[str, SubtaskNodeProjection],
    ) -> tuple[SubtaskMilestoneProjection, ...]:
        result: list[SubtaskMilestoneProjection] = []
        for milestone in sorted(plan.milestones, key=lambda value: (value.order_hint, value.key)):
            milestone_nodes = [nodes[key] for key in milestone.subtask_keys]
            completed = sum(node.state == "succeeded" for node in milestone_nodes)
            planned = sum(node.state == "planned" for node in milestone_nodes)
            if completed == len(milestone_nodes) and milestone_nodes:
                state = "completed"
            elif any(node.state == "running" for node in milestone_nodes):
                state = "running"
            elif any(
                node.state in {"waiting_user", "blocked", "failed", "waiting_dependency"}
                for node in milestone_nodes
            ):
                state = "waiting"
            else:
                state = "planned" if planned == len(milestone_nodes) else "ready"
            result.append(
                SubtaskMilestoneProjection(
                    key=milestone.key,
                    title=milestone.title,
                    state=state,
                    subtask_count=len(milestone_nodes),
                    completed_count=completed,
                    planned_count=planned,
                    order_hint=milestone.order_hint,
                )
            )
        return tuple(result)

    @staticmethod
    def _overview(
        plan: CanonicalTaskPlan,
        nodes: list[SubtaskNodeProjection],
        milestones: tuple[SubtaskMilestoneProjection, ...],
        *,
        tasks: Mapping[UUID, TaskConsoleTaskFact],
        activity: tuple[TaskConsoleActivityFact, ...],
        executions: tuple[TaskConsoleExecutionFact, ...],
        last_change_cursor: int,
        as_of: datetime,
        acknowledgements: tuple[AttentionAcknowledgement, ...],
        convergence: PlanRootProjection | None,
    ) -> TaskConsoleOverviewProjection:
        active_milestone = next(
            (milestone for milestone in milestones if milestone.state != "completed"),
            None,
        )
        activity_by_task = _group_activity_by_task(activity)
        executions_by_task = _group_executions_by_task(executions)
        attention: list[TaskConsoleAttentionProjection] = []
        for node in nodes:
            if node.state not in {"waiting_user", "blocked", "failed"}:
                continue
            task_id = node.task_id
            latest_activity = _latest(
                activity_by_task.get(task_id, ()) if task_id else ()
            )
            task = tasks.get(task_id) if task_id else None
            task_executions = (
                executions_by_task.get(task_id, ()) if task_id is not None else ()
            )
            latest_block = _latest(
                execution
                for execution in task_executions
                if execution.status == "queued" and execution.active_block_id is not None
            )
            if latest_block is not None and latest_block.active_block_id is not None:
                attention_key = (
                    f"scheduling_block:{plan.root_task_id}:"
                    f"{node.subtask_key}:{latest_block.active_block_id}"
                )
                resource_id = latest_block.active_block_id
                resource_version = latest_block.active_block_version or 1
                correlation_id = latest_block.active_block_id
                reason = (
                    latest_block.active_block_summary
                    or _default_attention_reason(node.state)
                )
                occurred_at = latest_block.active_block_first_seen_at or as_of
            else:
                attention_key = (
                    f"subtask:{plan.root_task_id}:{node.subtask_key}:{node.state}"
                )
                resource_id = node.task_id or plan.root_task_id
                resource_version = task.version if task else 1
                correlation_id = (
                    latest_activity.correlation_id
                    if latest_activity
                    else UUID(int=0)
                )
                reason = node.current_action or _default_attention_reason(node.state)
                occurred_at = latest_activity.occurred_at if latest_activity else as_of
            item = TaskConsoleAttentionProjection(
                key=attention_key,
                kind=node.state,
                task_title=plan.title,
                subtask_key=node.subtask_key,
                subtask_title=node.title,
                reason=reason,
                resource_id=resource_id,
                resource_version=resource_version,
                correlation_id=correlation_id,
                occurred_at=occurred_at,
                waiting_reason=(
                    latest_block.active_block_reason
                    if latest_block is not None
                    else None
                ),
                waiting_details_summary=(
                    latest_block.active_block_summary
                    if latest_block is not None
                    else None
                ),
                requires_operator=(
                    latest_block.active_block_requires_operator
                    if latest_block is not None
                    else False
                ),
                blocked_since=(
                    latest_block.active_block_first_seen_at
                    if latest_block is not None
                    else None
                ),
            )
            source = AttentionSource(
                attention_key=item.key,
                root_task_id=plan.root_task_id,
                resource_id=item.resource_id,
                resource_version=item.resource_version,
                correlation_id=item.correlation_id,
            )
            if not any(
                acknowledgement.applies_to(source)
                for acknowledgement in acknowledgements
            ):
                attention.append(item)

        next_keys = tuple(
            node.subtask_key
            for node in nodes
            if node.state not in {"succeeded", "canceled"}
        )
        return TaskConsoleOverviewProjection(
            schema_version=1,
            workspace_id=plan.workspace_id,
            root_task_id=plan.root_task_id,
            plan_snapshot_id=plan.snapshot_id,
            generation=plan.generation,
            title=plan.title,
            goal_summary=plan.goal_summary,
            acceptance_refs=plan.acceptance_refs,
            current_milestone_key=active_milestone.key if active_milestone else None,
            subtask_count=len(nodes),
            planned_subtask_count=sum(node.state == "planned" for node in nodes),
            running_subtask_count=sum(node.state == "running" for node in nodes),
            completed_subtask_count=sum(node.state == "succeeded" for node in nodes),
            failed_subtask_count=sum(node.state in {"failed", "blocked"} for node in nodes),
            next_subtask_keys=next_keys,
            attention_items=tuple(attention),
            last_change_cursor=last_change_cursor,
            as_of=as_of,
            plan_head=convergence.plan_head if convergence is not None else None,
            active_revision=(
                convergence.active_revision if convergence is not None else None
            ),
            convergence=convergence.convergence if convergence is not None else None,
            revision_attention=(
                convergence.revision_attention if convergence is not None else None
            ),
        )


def _group_placements(
    values: Iterable[TaskConsolePlacementFact],
) -> dict[str, tuple[TaskConsolePlacementFact, ...]]:
    grouped: dict[str, list[TaskConsolePlacementFact]] = defaultdict(list)
    for value in values:
        grouped[value.subtask_key].append(value)
    return {key: tuple(value) for key, value in grouped.items()}


def _group_executions(
    values: Iterable[TaskConsoleExecutionFact],
) -> dict[UUID, tuple[TaskConsoleExecutionFact, ...]]:
    grouped: dict[UUID, list[TaskConsoleExecutionFact]] = defaultdict(list)
    for value in values:
        if value.worker_item_id is not None:
            grouped[value.worker_item_id].append(value)
    return {key: tuple(value) for key, value in grouped.items()}


def _group_executions_by_task(
    values: Iterable[TaskConsoleExecutionFact],
) -> dict[UUID, tuple[TaskConsoleExecutionFact, ...]]:
    grouped: dict[UUID, list[TaskConsoleExecutionFact]] = defaultdict(list)
    for value in values:
        grouped[value.task_id].append(value)
    return {key: tuple(value) for key, value in grouped.items()}


def _group_evidence_by_task(
    values: Iterable[TaskConsoleEvidenceFact],
) -> dict[UUID, tuple[TaskConsoleEvidenceFact, ...]]:
    grouped: dict[UUID, list[TaskConsoleEvidenceFact]] = defaultdict(list)
    for value in values:
        grouped[value.task_id].append(value)
    return {key: tuple(value) for key, value in grouped.items()}


def _group_activity_by_task(
    values: Iterable[TaskConsoleActivityFact],
) -> dict[UUID, tuple[TaskConsoleActivityFact, ...]]:
    grouped: dict[UUID, list[TaskConsoleActivityFact]] = defaultdict(list)
    for value in values:
        grouped[value.task_id].append(value)
    return {key: tuple(value) for key, value in grouped.items()}


def _latest(values: Iterable[Any]) -> Any:
    return max(values, key=_fact_sort_key, default=None)


def _worker_item_state(
    worker_item: TaskConsoleWorkerItemFact | None,
    execution: TaskConsoleExecutionFact | None,
    task: TaskConsoleTaskFact | None,
) -> str:
    if worker_item is not None:
        if worker_item.status == "retired":
            return "retired"
        if worker_item.status == "paused":
            return "paused"
    if execution is not None:
        if execution.active_block_id is not None and execution.status == "queued":
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
    task: TaskConsoleTaskFact | None,
    item_states: tuple[str, ...],
    *,
    evidence_satisfied: int,
    evidence_required: int,
    task_executions: tuple[TaskConsoleExecutionFact, ...],
) -> str:
    del subtask
    if "waiting_user" in item_states or (task and task.status in {"waiting", "waiting_user"}):
        return "waiting_user"
    if "blocked" in item_states:
        return "blocked"
    if "failed" in item_states:
        return "failed"
    if any(_is_running_state(value) for value in item_states):
        return "running"
    if not item_states and task_executions:
        execution_state = _worker_item_state(None, _latest(task_executions), task)
        if execution_state == "waiting_user":
            return "waiting_user"
        if execution_state == "blocked":
            return "blocked"
        if execution_state == "failed":
            return "failed"
        if _is_running_state(execution_state):
            return "running"
    if task and task.status in {"running", "ready"} and not task_executions:
        return "running"
    if (
        (not item_states or all(value in {"succeeded", "canceled"} for value in item_states))
        and evidence_satisfied >= evidence_required
        and task
        and task.status == "succeeded"
    ):
        return "succeeded"
    if task and task.status == "failed":
        return "failed"
    if task and task.status == "blocked":
        return "blocked"
    return "planned"


def _evidence_satisfied(
    subtask: SubtaskPlanItem,
    evidence: Iterable[TaskConsoleEvidenceFact],
) -> int:
    refs = set(subtask.acceptance_refs)
    return len(
        {
            item.acceptance_ref
            for item in evidence
            if item.acceptance_ref in refs
        }
    )


def _is_running_state(state: str) -> bool:
    return state in {"queued", "assigned", "running", "retrying"}


def _is_attention_state(state: str) -> bool:
    return state in {"waiting_user", "blocked", "failed"}


def _default_attention_reason(state: str) -> str:
    return {
        "waiting_user": "等待用户确认",
        "blocked": "子任务已阻塞",
        "failed": "子任务执行失败",
    }.get(state, f"子任务状态为 {state}")


def _fact_sort_key(value: object) -> tuple[datetime, UUID | str]:
    timestamp = (
        getattr(value, "updated_at", None)
        or getattr(value, "occurred_at", None)
        or getattr(value, "created_at", None)
    )
    identifier = getattr(value, "id", "")
    return timestamp or datetime.min.replace(tzinfo=UTC), identifier


def _activity_sort_key(value: TaskConsoleActivityFact) -> tuple[datetime, str, UUID]:
    return value.occurred_at, value.source_type, value.id


def _activity_cursor(value: TaskConsoleActivityFact) -> str:
    return f"{value.occurred_at.isoformat()}|{value.source_type}|{value.id}"
