from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sidera_control_plane.application.errors import NoCurrentPlan, NotFound
from sidera_control_plane.domain.plans import (
    TaskPlanSnapshot,
    WorkItem,
)
from sidera_control_plane.domain.projections import (
    ActivityItemProjection,
    ArtifactSummaryProjection,
    AttentionItemProjection,
    CheckpointSummaryProjection,
    EvidenceSummaryProjection,
    ExecutionSummaryProjection,
    MainAgentSlotProjection,
    MilestoneProjection,
    PlanDiffEntryProjection,
    PlanDiffProjection,
    TaskActivityProjection,
    TaskDetailProjection,
    TaskEdgeProjection,
    TaskGraphProjection,
    TaskNodeProjection,
    TaskNodeState,
    TaskOverviewProjection,
    WorkspaceCapabilitiesProjection,
)


@dataclass(frozen=True, slots=True)
class TaskFact:
    id: UUID
    workspace_id: UUID
    title: str
    acceptance_criteria: tuple[str, ...]
    status: str
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionFact:
    id: UUID
    task_id: UUID
    status: str
    attempt: int
    session_id: UUID | None
    assigned_worker_id: UUID | None
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime
    capability_requirements: tuple[str, ...]
    lineage_id: UUID | None = None
    source_execution_id: UUID | None = None
    source_session_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class LeaseFact:
    id: UUID
    execution_id: UUID
    status: str
    worker_id: UUID
    version: int
    fencing_token: int | None = None


@dataclass(frozen=True, slots=True)
class BindingFact:
    execution_id: UUID
    worker_id: UUID
    version: int
    lease_id: UUID | None = None
    fencing_token: int | None = None


@dataclass(frozen=True, slots=True)
class SessionFact:
    id: UUID
    execution_id: UUID
    worker_id: UUID
    status: str
    last_activity_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class WorkerFact:
    id: UUID
    display_name: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvidenceFact:
    id: UUID
    task_id: UUID
    acceptance_ref: str
    created_at: datetime
    conclusion: str = ""
    artifact_ids: tuple[UUID, ...] = ()
    execution_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ArtifactFact:
    id: UUID
    task_id: UUID
    execution_id: UUID
    artifact_type: str
    media_type: str
    size_bytes: int
    content_available: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CheckpointFact:
    execution_id: UUID
    sequence: int
    title: str
    artifact_ids: tuple[UUID, ...]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ActivityFact:
    id: UUID
    task_id: UUID
    source_type: str
    occurred_at: datetime
    kind: str
    title: str
    summary: str
    correlation_id: UUID
    causal_parent_id: UUID | None = None
    scheduler_enforced: bool = False


FactValue = Any
FactMap = Mapping[UUID, FactValue]


class TaskFirstProjectionService:
    """Compose Task First views from immutable plan and runtime facts.

    This service deliberately has no scheduling side effects. In particular,
    planned nodes and informational dependencies are display facts only.
    """

    def compose(
        self,
        snapshot: TaskPlanSnapshot,
        *,
        tasks: Mapping[UUID, TaskFact],
        executions: FactMap,
        leases: FactMap,
        bindings: FactMap,
        sessions: FactMap,
        workers: Mapping[UUID, WorkerFact],
        evidence: FactMap,
        activity: FactMap,
        last_change_cursor: int,
        as_of: datetime,
    ) -> TaskGraphProjection:
        nodes = [
            self._node_for_item(
                item,
                snapshot,
                tasks=tasks,
                executions=executions,
                leases=leases,
                bindings=bindings,
                sessions=sessions,
                workers=workers,
                evidence=evidence,
                activity=activity,
            )
            for item in snapshot.work_items
        ]
        nodes = self._apply_dependency_overlay(snapshot, nodes)
        edges = tuple(
            self._edge_projection(dependency) for dependency in snapshot.dependencies
        )
        nodes = self._apply_horizon(nodes, edges)
        milestones = self._milestones(snapshot, nodes)
        overview = self._overview(
            snapshot,
            nodes,
            milestones,
            activity=activity,
            last_change_cursor=last_change_cursor,
            as_of=as_of,
        )
        return TaskGraphProjection(
            schema_version=1,
            overview=overview,
            nodes=tuple(nodes),
            edges=edges,
            milestones=milestones,
        )

    def compose_detail(
        self,
        graph: TaskGraphProjection,
        snapshot: TaskPlanSnapshot,
        work_item_key: str,
        *,
        executions: FactMap,
        artifacts: FactMap,
        evidence: FactMap,
        checkpoints: FactMap,
        last_change_cursor: int,
    ) -> TaskDetailProjection:
        item = _find_work_item(snapshot, work_item_key)
        node = next(node for node in graph.nodes if node.work_item_key == work_item_key)
        execution_values = _values_for_task(executions, item.task_id)
        artifact_values = _values_for_task(artifacts, item.task_id)
        evidence_values = _values_for_task(evidence, item.task_id)
        checkpoint_values = tuple(
            checkpoint
            for execution in execution_values
            for checkpoint in _values_for_task(checkpoints, execution.id)
        )
        return TaskDetailProjection(
            schema_version=1,
            last_change_cursor=last_change_cursor,
            node=node,
            acceptance_refs=item.acceptance_refs,
            expected_outputs=item.expected_outputs,
            dependencies=tuple(
                edge
                for edge in graph.edges
                if edge.upstream_work_item_key == work_item_key
                or edge.downstream_work_item_key == work_item_key
            ),
            executions=tuple(
                ExecutionSummaryProjection(
                    execution_id=execution.id,
                    session_id=execution.session_id,
                    worker_id=execution.assigned_worker_id,
                    status=execution.status,
                    attempt=execution.attempt,
                    started_at=execution.started_at,
                    finished_at=execution.finished_at,
                )
                for execution in execution_values
            ),
            artifacts=tuple(
                ArtifactSummaryProjection(
                    artifact_id=artifact.id,
                    artifact_type=artifact.artifact_type,
                    media_type=artifact.media_type,
                    size_bytes=artifact.size_bytes,
                    content_available=artifact.content_available,
                )
                for artifact in artifact_values
            ),
            evidence=tuple(
                EvidenceSummaryProjection(
                    evidence_id=claim.id,
                    acceptance_ref=claim.acceptance_ref,
                    conclusion=claim.conclusion,
                    artifact_ids=claim.artifact_ids,
                )
                for claim in evidence_values
            ),
            checkpoints=tuple(
                CheckpointSummaryProjection(
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
        )

    def compose_activity(
        self,
        activity: Iterable[ActivityFact],
        *,
        last_change_cursor: int,
        after: str | None,
        limit: int,
    ) -> TaskActivityProjection:
        ordered = sorted(activity, key=_activity_sort_key)
        if after:
            ordered = [fact for fact in ordered if _activity_cursor(fact) > after]
        page = ordered[:limit]
        next_cursor = (
            _activity_cursor(page[-1]) if len(ordered) > limit and page else None
        )
        return TaskActivityProjection(
            schema_version=1,
            last_change_cursor=last_change_cursor,
            items=tuple(
                ActivityItemProjection(
                    source_type=fact.source_type,
                    source_id=fact.id,
                    occurred_at=fact.occurred_at,
                    kind=fact.kind,
                    title=fact.title,
                    summary=fact.summary,
                    correlation_id=fact.correlation_id,
                    causal_parent_id=fact.causal_parent_id,
                    scheduler_enforced=fact.scheduler_enforced,
                )
                for fact in page
            ),
            next_cursor=next_cursor,
        )

    def compose_diff(
        self,
        left: TaskPlanSnapshot,
        right: TaskPlanSnapshot,
        *,
        runtime_executions: FactMap,
        last_change_cursor: int,
    ) -> PlanDiffProjection:
        left_items = {item.key: item for item in left.work_items}
        right_items = {item.key: item for item in right.work_items}
        entries: list[PlanDiffEntryProjection] = []
        for key in tuple(left_items) + tuple(
            item_key for item_key in right_items if item_key not in left_items
        ):
            left_item = left_items.get(key)
            right_item = right_items.get(key)
            candidate = right_item or left_item
            if candidate is None:
                continue
            runtime_execution_id = _runtime_execution_id(
                runtime_executions,
                candidate.task_id,
            )
            if left_item is None:
                entries.append(
                    PlanDiffEntryProjection(
                        work_item_key=key,
                        change="added",
                        changed_fields=(),
                        reuse_candidate=False,
                        context_patch_candidate=False,
                        portable_replay_candidate=False,
                        runtime_execution_id=runtime_execution_id,
                    )
                )
                continue
            if right_item is None:
                entries.append(
                    PlanDiffEntryProjection(
                        work_item_key=key,
                        change="removed",
                        changed_fields=(),
                        reuse_candidate=False,
                        context_patch_candidate=False,
                        portable_replay_candidate=runtime_execution_id is not None,
                        runtime_execution_id=runtime_execution_id,
                    )
                )
                continue
            changed_fields = _changed_work_item_fields(left_item, right_item)
            changed = "unchanged" if not changed_fields else "patched"
            entries.append(
                PlanDiffEntryProjection(
                    work_item_key=key,
                    change=changed,
                    changed_fields=changed_fields,
                    reuse_candidate=not changed_fields,
                    context_patch_candidate=bool(changed_fields),
                    portable_replay_candidate=False,
                    runtime_execution_id=runtime_execution_id,
                )
            )
        return PlanDiffProjection(
            schema_version=1,
            last_change_cursor=last_change_cursor,
            root_task_id=right.root_task_id,
            left_generation=left.generation,
            right_generation=right.generation,
            entries=tuple(entries),
        )

    @staticmethod
    def capabilities(entry_uri: str | None) -> WorkspaceCapabilitiesProjection:
        return WorkspaceCapabilitiesProjection(
            schema_version=1,
            main_agent=MainAgentSlotProjection(
                available=False,
                request_revision_supported=False,
                entry_uri=entry_uri,
            ),
        )

    def _node_for_item(
        self,
        item: WorkItem,
        snapshot: TaskPlanSnapshot,
        *,
        tasks: Mapping[UUID, TaskFact],
        executions: FactMap,
        leases: FactMap,
        bindings: FactMap,
        sessions: FactMap,
        workers: Mapping[UUID, WorkerFact],
        evidence: FactMap,
        activity: FactMap,
    ) -> TaskNodeProjection:
        task = tasks.get(item.task_id) if item.task_id else None
        execution = _latest_fact(executions, item.task_id)
        lease = _latest_fact(leases, execution.id) if execution else None
        binding = _latest_fact(bindings, execution.id) if execution else None
        assigned = _valid_assignment(execution, lease, binding)
        session = _session_for_execution(sessions, execution)
        worker_id = binding.worker_id if assigned and binding else None
        worker = workers.get(worker_id) if worker_id else None
        evidence_values = _values_for_task(evidence, item.task_id)
        activity_value = _latest_fact(activity, item.task_id)
        state = _state_for(task, execution, assigned)
        required_capabilities = _required_capabilities(snapshot, item, execution)
        evidence_refs = set(item.acceptance_refs)
        satisfied_refs = {
            claim.acceptance_ref
            for claim in evidence_values
            if claim.acceptance_ref in evidence_refs
        }
        updated_values = [
            value
            for value in (
                task.updated_at if task else None,
                execution.updated_at if execution else None,
                session.last_activity_at if session else None,
                activity_value.occurred_at if activity_value else None,
            )
            if value is not None
        ]
        return TaskNodeProjection(
            work_item_key=item.key,
            task_id=item.task_id,
            kind=item.kind,
            title=item.title,
            goal_summary=item.goal_summary,
            milestone_key=item.milestone_key,
            worker_slot_key=item.worker_slot_key,
            required_capabilities=required_capabilities,
            state=state,
            execution_id=execution.id if execution else None,
            session_id=session.id
            if session
            else execution.session_id
            if execution
            else None,
            worker_id=worker_id,
            worker_display_name=worker.display_name if worker else None,
            execution_horizon=False,
            critical_path=state
            in {
                TaskNodeState.RUNNING,
                TaskNodeState.WAITING_DEPENDENCY,
                TaskNodeState.FAILED,
            },
            waiting_on_plan_dependency=False,
            scheduler_enforced=assigned
            or state
            in {
                TaskNodeState.SUCCEEDED,
                TaskNodeState.FAILED,
                TaskNodeState.CANCELED,
            },
            current_action=(activity_value.summary if activity_value else None),
            evidence_satisfied=len(satisfied_refs),
            evidence_required=len(item.acceptance_refs),
            updated_at=max(updated_values) if updated_values else None,
        )

    @staticmethod
    def _apply_dependency_overlay(
        snapshot: TaskPlanSnapshot, nodes: list[TaskNodeProjection]
    ) -> list[TaskNodeProjection]:
        by_key = {node.work_item_key: node for node in nodes}
        incomplete_upstreams: dict[str, bool] = {
            node.work_item_key: False for node in nodes
        }
        for dependency in snapshot.dependencies:
            upstream = by_key[dependency.upstream_work_item_key]
            if upstream.state not in {
                TaskNodeState.SUCCEEDED,
                TaskNodeState.CANCELED,
            }:
                incomplete_upstreams[dependency.downstream_work_item_key] = True
        result: list[TaskNodeProjection] = []
        for node in nodes:
            waiting = (
                node.task_id is not None
                and incomplete_upstreams[node.work_item_key]
                and node.state
                not in {
                    TaskNodeState.RUNNING,
                    TaskNodeState.ASSIGNED,
                    TaskNodeState.QUEUED,
                    TaskNodeState.SUCCEEDED,
                    TaskNodeState.FAILED,
                    TaskNodeState.CANCELED,
                }
            )
            if not waiting:
                result.append(node)
                continue
            payload = {
                field: getattr(node, field) for field in node.__dataclass_fields__
            }
            payload.update(
                state=TaskNodeState.WAITING_DEPENDENCY,
                waiting_on_plan_dependency=True,
                scheduler_enforced=False,
            )
            result.append(TaskNodeProjection(**payload))
        return result

    @staticmethod
    def _apply_horizon(
        nodes: list[TaskNodeProjection], edges: tuple[TaskEdgeProjection, ...]
    ) -> list[TaskNodeProjection]:
        active = {
            node.work_item_key
            for node in nodes
            if node.state
            in {
                TaskNodeState.RUNNING,
                TaskNodeState.ASSIGNED,
                TaskNodeState.WAITING_DEPENDENCY,
                TaskNodeState.WAITING_USER,
                TaskNodeState.BLOCKED,
                TaskNodeState.FAILED,
            }
        }
        horizon = set(active)
        for edge in edges:
            if (
                edge.upstream_work_item_key in active
                or edge.downstream_work_item_key in active
            ):
                horizon.update(
                    (edge.upstream_work_item_key, edge.downstream_work_item_key)
                )
        result: list[TaskNodeProjection] = []
        for node in nodes:
            payload = {
                field: getattr(node, field) for field in node.__dataclass_fields__
            }
            payload["execution_horizon"] = node.work_item_key in horizon
            result.append(TaskNodeProjection(**payload))
        return result

    @staticmethod
    def _edge_projection(dependency: Any) -> TaskEdgeProjection:
        return TaskEdgeProjection(
            upstream_work_item_key=dependency.upstream_work_item_key,
            downstream_work_item_key=dependency.downstream_work_item_key,
            kind=dependency.kind,
            enforcement=dependency.enforcement,
            scheduler_enforced=False,
            required_evidence_refs=dependency.required_evidence_refs,
        )

    @staticmethod
    def _milestones(
        snapshot: TaskPlanSnapshot, nodes: list[TaskNodeProjection]
    ) -> tuple[MilestoneProjection, ...]:
        by_key = {node.work_item_key: node for node in nodes}
        result: list[MilestoneProjection] = []
        for milestone in sorted(
            snapshot.milestones, key=lambda value: (value.order_hint, value.key)
        ):
            milestone_nodes = [by_key[key] for key in milestone.work_item_keys]
            materialized = sum(node.task_id is not None for node in milestone_nodes)
            planned = len(milestone_nodes) - materialized
            completed = sum(
                node.state is TaskNodeState.SUCCEEDED for node in milestone_nodes
            )
            if materialized == 0:
                state = "planned"
            elif completed == materialized and planned == 0:
                state = "completed"
            elif any(
                node.state in {TaskNodeState.RUNNING, TaskNodeState.ASSIGNED}
                for node in milestone_nodes
            ):
                state = "running"
            elif any(
                node.state in {TaskNodeState.WAITING_DEPENDENCY, TaskNodeState.BLOCKED}
                for node in milestone_nodes
            ):
                state = "waiting"
            else:
                state = "ready"
            result.append(
                MilestoneProjection(
                    key=milestone.key,
                    title=milestone.title,
                    state=state,
                    materialized_count=materialized,
                    completed_count=completed,
                    planned_count=planned,
                    order_hint=milestone.order_hint,
                )
            )
        return tuple(result)

    def _overview(
        self,
        snapshot: TaskPlanSnapshot,
        nodes: list[TaskNodeProjection],
        milestones: tuple[MilestoneProjection, ...],
        *,
        activity: FactMap,
        last_change_cursor: int,
        as_of: datetime,
    ) -> TaskOverviewProjection:
        active_milestone = next(
            (milestone for milestone in milestones if milestone.state != "completed"),
            None,
        )
        attention = tuple(
            self._attention(node, activity)
            for node in nodes
            if node.state
            in {
                TaskNodeState.WAITING_DEPENDENCY,
                TaskNodeState.WAITING_USER,
                TaskNodeState.BLOCKED,
                TaskNodeState.FAILED,
            }
        )
        next_keys = tuple(
            node.work_item_key
            for node in nodes
            if node.state
            in {
                TaskNodeState.PLANNED,
                TaskNodeState.MATERIALIZED,
                TaskNodeState.READY,
                TaskNodeState.WAITING_DEPENDENCY,
                TaskNodeState.WAITING_USER,
                TaskNodeState.BLOCKED,
                TaskNodeState.RETRYING,
                TaskNodeState.PAUSED,
            }
        )
        return TaskOverviewProjection(
            schema_version=1,
            workspace_id=snapshot.workspace_id,
            root_task_id=snapshot.root_task_id,
            plan_snapshot_id=snapshot.id,
            generation=snapshot.generation,
            title=snapshot.title,
            goal_summary=snapshot.goal_summary,
            acceptance_refs=snapshot.acceptance_refs,
            current_milestone_key=active_milestone.key if active_milestone else None,
            materialized_count=sum(node.task_id is not None for node in nodes),
            planned_count=sum(node.state is TaskNodeState.PLANNED for node in nodes),
            running_count=sum(
                node.state in {TaskNodeState.RUNNING, TaskNodeState.ASSIGNED}
                for node in nodes
            ),
            completed_count=sum(
                node.state is TaskNodeState.SUCCEEDED for node in nodes
            ),
            failed_count=sum(node.state is TaskNodeState.FAILED for node in nodes),
            next_work_item_keys=next_keys,
            attention_items=attention,
            last_change_cursor=last_change_cursor,
            as_of=as_of,
        )

    @staticmethod
    def _attention(
        node: TaskNodeProjection, activity: FactMap
    ) -> AttentionItemProjection:
        fact = _latest_fact(activity, node.task_id)
        return AttentionItemProjection(
            key=f"{node.work_item_key}:{node.state.value}",
            kind=node.state.value,
            title=node.title,
            summary=node.current_action or f"Task is {node.state.value}",
            resource_id=node.task_id or UUID(int=0),
            resource_version=1,
            correlation_id=fact.correlation_id if fact else UUID(int=0),
            occurred_at=(
                fact.occurred_at
                if fact
                else node.updated_at or datetime.min.replace(tzinfo=UTC)
            ),
        )


def _state_for(
    task: TaskFact | None,
    execution: ExecutionFact | None,
    assigned: bool,
) -> TaskNodeState:
    if task is None:
        return TaskNodeState.PLANNED
    if execution is None:
        return {
            "pending": TaskNodeState.MATERIALIZED,
            "ready": TaskNodeState.READY,
            "running": TaskNodeState.RUNNING,
            "waiting": TaskNodeState.WAITING_USER,
            "blocked": TaskNodeState.BLOCKED,
            "paused": TaskNodeState.PAUSED,
            "succeeded": TaskNodeState.SUCCEEDED,
            "failed": TaskNodeState.FAILED,
            "canceled": TaskNodeState.CANCELED,
        }.get(task.status, TaskNodeState.MATERIALIZED)
    if execution.status == "queued":
        return TaskNodeState.QUEUED
    if execution.status == "leased":
        return TaskNodeState.ASSIGNED if assigned else TaskNodeState.QUEUED
    return {
        "running": TaskNodeState.RUNNING,
        "succeeded": TaskNodeState.SUCCEEDED,
        "failed": TaskNodeState.FAILED,
        "canceled": TaskNodeState.CANCELED,
        "lost": TaskNodeState.RETRYING,
    }.get(execution.status, TaskNodeState.QUEUED)


def _valid_assignment(
    execution: ExecutionFact | None,
    lease: LeaseFact | None,
    binding: BindingFact | None,
) -> bool:
    return bool(
        execution
        and lease
        and binding
        and lease.status == "active"
        and lease.execution_id == execution.id
        and binding.execution_id == execution.id
        and binding.worker_id == lease.worker_id
        and (
            lease.fencing_token is None
            or binding.fencing_token is None
            or lease.fencing_token == binding.fencing_token
        )
    )


def _session_for_execution(
    sessions: FactMap, execution: ExecutionFact | None
) -> SessionFact | None:
    if execution is None:
        return None
    if execution.session_id:
        value = sessions.get(execution.session_id)
        if isinstance(value, SessionFact):
            return value
    candidates = [
        value for value in _all_facts(sessions) if isinstance(value, SessionFact)
    ]
    matching = [value for value in candidates if value.execution_id == execution.id]
    return max(
        matching, key=lambda value: (value.last_activity_at, value.id), default=None
    )


def _required_capabilities(
    snapshot: TaskPlanSnapshot, item: WorkItem, execution: ExecutionFact | None
) -> tuple[str, ...]:
    if execution and execution.capability_requirements:
        return tuple(sorted(execution.capability_requirements))
    for slot in snapshot.worker_slots:
        if slot.key == item.worker_slot_key:
            return tuple(sorted(slot.capability_requirements))
    return ()


def _find_work_item(snapshot: TaskPlanSnapshot, key: str) -> WorkItem:
    for item in snapshot.work_items:
        if item.key == key:
            return item
    raise KeyError(f"unknown work item: {key}")


def _all_facts(values: FactMap) -> tuple[FactValue, ...]:
    result: list[FactValue] = []
    for value in values.values():
        result.extend(value if isinstance(value, tuple | list) else (value,))
    return tuple(result)


def _latest_fact(values: FactMap, key: UUID | None) -> FactValue | None:
    if key is None:
        return None
    value = values.get(key)
    if value is not None:
        if isinstance(value, tuple | list):
            return max(value, key=_fact_sort_key, default=None)
        return value
    candidates = [
        candidate for candidate in _all_facts(values) if _fact_key(candidate) == key
    ]
    return max(candidates, key=_fact_sort_key, default=None)


def _values_for_task(values: FactMap, key: UUID | None) -> tuple[Any, ...]:
    if key is None:
        return ()
    value = values.get(key)
    if value is not None:
        return tuple(value) if isinstance(value, tuple | list) else (value,)
    return tuple(
        candidate for candidate in _all_facts(values) if _fact_key(candidate) == key
    )


def _fact_key(value: Any) -> UUID | None:
    for field in ("task_id", "execution_id", "id"):
        candidate = getattr(value, field, None)
        if isinstance(candidate, UUID):
            return candidate
    return None


def _fact_sort_key(value: Any) -> tuple[Any, UUID | str]:
    timestamp = getattr(value, "updated_at", None) or getattr(value, "created_at", None)
    timestamp = timestamp or getattr(value, "last_activity_at", None)
    timestamp = timestamp or getattr(value, "occurred_at", None)
    identifier = getattr(value, "id", "")
    return timestamp or datetime.min.replace(tzinfo=UTC), identifier


def _runtime_execution_id(values: FactMap, task_id: UUID | None) -> UUID | None:
    value = _latest_fact(values, task_id)
    return value.id if isinstance(value, ExecutionFact) else None


def _changed_work_item_fields(left: WorkItem, right: WorkItem) -> tuple[str, ...]:
    left_payload = asdict(left)
    right_payload = asdict(right)
    left_payload.pop("task_id", None)
    right_payload.pop("task_id", None)
    changed = [
        key for key in left_payload if left_payload[key] != right_payload.get(key)
    ]
    return tuple(changed)


def _activity_sort_key(value: ActivityFact) -> tuple[datetime, str, UUID]:
    return value.occurred_at, value.source_type, value.id


def _activity_cursor(value: ActivityFact) -> str:
    return f"{value.occurred_at.isoformat()}|{value.source_type}|{value.id}"


class TaskFirstQueryService:
    def __init__(self, store: Any, *, main_agent_entry_uri: str | None = None) -> None:
        self._store = store
        self._composer = TaskFirstProjectionService()
        self._main_agent_entry_uri = main_agent_entry_uri

    def list_overviews(self, workspace_id: UUID) -> tuple[TaskOverviewProjection, ...]:
        overviews: list[TaskOverviewProjection] = []
        for root_task_id in self._store.list_current_root_ids(workspace_id):
            loaded = self._store.load(workspace_id, root_task_id)
            if loaded is not None:
                overviews.append(self._graph(loaded).overview)
        return tuple(overviews)

    def get_overview(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> TaskOverviewProjection:
        return self._graph(self._require_loaded(workspace_id, root_task_id)).overview

    def get_graph(self, workspace_id: UUID, root_task_id: UUID) -> TaskGraphProjection:
        return self._graph(self._require_loaded(workspace_id, root_task_id))

    def get_detail(
        self, workspace_id: UUID, root_task_id: UUID, work_item_key: str
    ) -> TaskDetailProjection:
        loaded = self._require_loaded(workspace_id, root_task_id)
        graph = self._graph(loaded)
        try:
            return self._composer.compose_detail(
                graph,
                loaded.plan,
                work_item_key,
                executions=loaded.executions,
                artifacts=loaded.artifacts,
                evidence=loaded.evidence,
                checkpoints=loaded.checkpoints,
                last_change_cursor=loaded.last_change_cursor,
            )
        except KeyError as exc:
            raise NotFound("work item not found") from exc

    def get_activity(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        after: str | None,
        limit: int,
    ) -> TaskActivityProjection:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        loaded = self._require_loaded(workspace_id, root_task_id)
        return self._composer.compose_activity(
            loaded.activity,
            last_change_cursor=loaded.last_change_cursor,
            after=after,
            limit=limit,
        )

    def get_plan_diff(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        left_generation: int,
        right_generation: int,
    ) -> PlanDiffProjection:
        loaded = self._require_loaded(workspace_id, root_task_id)
        left = self._store.get_plan(workspace_id, root_task_id, left_generation)
        right = self._store.get_plan(workspace_id, root_task_id, right_generation)
        if left is None or right is None:
            raise NotFound("task plan generation not found")
        task_ids = {
            item.task_id
            for snapshot in (left, right)
            for item in snapshot.work_items
            if item.task_id
        }
        runtime_executions = dict(loaded.executions)
        runtime_executions.update(
            self._store.list_executions_for_tasks(workspace_id, task_ids)
        )
        return self._composer.compose_diff(
            left,
            right,
            runtime_executions=runtime_executions,
            last_change_cursor=loaded.last_change_cursor,
        )

    def get_capabilities(self) -> WorkspaceCapabilitiesProjection:
        return self._composer.capabilities(self._main_agent_entry_uri)

    def _require_loaded(self, workspace_id: UUID, root_task_id: UUID) -> Any:
        loaded = self._store.load(workspace_id, root_task_id)
        if loaded is None:
            raise NoCurrentPlan("current task plan is unavailable")
        return loaded

    def _graph(self, loaded: Any) -> TaskGraphProjection:
        activity_by_task: dict[UUID, tuple[ActivityFact, ...]] = defaultdict(tuple)
        grouped: dict[UUID, list[ActivityFact]] = defaultdict(list)
        for fact in loaded.activity:
            grouped[fact.task_id].append(fact)
        activity_by_task = {key: tuple(value) for key, value in grouped.items()}
        return self._composer.compose(
            loaded.plan,
            tasks=loaded.tasks,
            executions=loaded.executions,
            leases=loaded.leases,
            bindings=loaded.bindings,
            sessions=loaded.sessions,
            workers=loaded.workers,
            evidence=loaded.evidence,
            activity=activity_by_task,
            last_change_cursor=loaded.last_change_cursor,
            as_of=loaded.as_of,
        )
