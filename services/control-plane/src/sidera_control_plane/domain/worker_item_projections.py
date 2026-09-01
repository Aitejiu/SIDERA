from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.domain.plan_convergence_projections import (
    PlanConvergenceFacts,
    PlanConvergenceProjectionService,
    WorkerPlanSyncProjection,
)


class InvalidActivityCursor(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WorkerItemFact:
    id: UUID
    workspace_id: UUID
    root_task_id: UUID
    stable_key: str
    title: str
    responsibility: str
    status: str
    context_version: int
    acknowledged_context_version: int
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerItemPlacementFact:
    plan_snapshot_id: UUID
    plan_generation: int
    worker_item_id: UUID
    worker_item_key: str
    subtask_key: str
    subtask_task_id: UUID | None
    role: str
    capability_requirements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExecutionFact:
    id: UUID
    task_id: UUID
    worker_item_id: UUID
    lineage_id: UUID
    attempt: int
    status: str
    failure_code: str | None
    assigned_worker_id: UUID | None
    pinned_worker_id: UUID | None
    active_lease_id: UUID | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    version: int = 1
    capability_requirements: tuple[str, ...] = ()
    adapter_preferences: tuple[str, ...] = ()
    adapter_forbidden: tuple[str, ...] = ()
    scheduling_status: str = ""
    waiting_reason: str | None = None
    waiting_details_summary: str | None = None
    requires_operator: bool = False
    blocked_since: datetime | None = None


@dataclass(frozen=True, slots=True)
class SessionFact:
    id: UUID
    execution_id: UUID
    worker_id: UUID
    adapter_session_id: str
    status: str
    input_mode: str
    version: int
    created_at: datetime
    last_activity_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerFact:
    id: UUID
    display_name: str
    adapter_type: str
    status: str = "offline"
    desired_state: str = "active"
    capabilities: tuple[str, ...] = ()
    current_load: int = 0
    capacity: int = 0


@dataclass(frozen=True, slots=True)
class BindingFact:
    execution_id: UUID
    worker_id: UUID
    lease_id: UUID
    fencing_token: int
    updated_at: datetime
    id: UUID | None = None
    adapter_module_id: UUID | None = None
    workspace_module_id: UUID | None = None
    adapter_config_version: int | None = None
    workspace_config_version: int | None = None
    version: int = 1


@dataclass(frozen=True, slots=True)
class WorkerSummaryProjection:
    id: UUID
    display_name: str
    adapter_type: str
    status: str
    desired_state: str
    current_load: int
    capacity: int


@dataclass(frozen=True, slots=True)
class BindingSummaryProjection:
    id: UUID
    worker_id: UUID
    adapter_module_id: UUID
    workspace_module_id: UUID
    adapter_config_version: int
    workspace_config_version: int
    fencing_token: int
    version: int


@dataclass(frozen=True, slots=True)
class LeaseFact:
    id: UUID
    execution_id: UUID
    worker_id: UUID
    status: str
    fencing_token: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerEventFact:
    id: UUID
    execution_id: UUID
    session_id: UUID
    sequence: int
    event_type: str
    payload: Mapping[str, Any]
    occurred_at: datetime
    raw_ref: str | None


@dataclass(frozen=True, slots=True)
class InputFact:
    id: UUID
    execution_id: UUID
    session_id: UUID
    text: str
    status: str
    operation_id: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactSummaryProjection:
    artifact_id: UUID
    task_id: UUID
    execution_id: UUID
    artifact_type: str
    media_type: str
    size_bytes: int
    content_available: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceSummaryProjection:
    evidence_id: UUID
    task_id: UUID
    execution_id: UUID
    acceptance_ref: str
    conclusion: str
    artifact_ids: tuple[UUID, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TraceDescriptorProjection:
    root_task_id: UUID
    execution_id: UUID
    session_id: UUID
    route: str
    raw_ref: str | None


@dataclass(frozen=True, slots=True)
class ActiveHandoffProjection:
    transition_id: UUID
    proposal_id: UUID
    status: str
    phase: str
    writer_state: str
    source_execution_id: UUID
    source_session_id: UUID
    source_worker_id: UUID
    target_worker_id: UUID
    target_execution_id: UUID | None
    target_session_id: UUID | None
    attention_required: bool
    failure_code: str | None
    blocked_operation_id: UUID | None
    available_recovery_strategies: tuple[str, ...]
    source_trace_descriptor: TraceDescriptorProjection | None
    target_trace_descriptor: TraceDescriptorProjection | None
    started_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerActivityFact:
    id: UUID
    source_type: str
    source_id: UUID
    execution_id: UUID | None
    session_id: UUID | None
    occurred_at: datetime
    kind: str
    title: str
    summary: str
    correlation_id: UUID
    source_priority: int
    body: str | None
    delivery_status: str | None
    trace_descriptor: TraceDescriptorProjection | None
    artifact_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkerItemIdentityProjection:
    id: UUID
    root_task_id: UUID
    stable_key: str
    title: str
    responsibility: str
    status: str
    context_version: int
    acknowledged_context_version: int
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerItemPlacementProjection:
    plan_snapshot_id: UUID
    plan_generation: int
    worker_item_key: str
    subtask_key: str
    subtask_task_id: UUID | None
    role: str
    capability_requirements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkerRuntimeProjection:
    execution_id: UUID
    task_id: UUID
    execution_status: str
    attempt: int
    session_id: UUID | None
    session_status: str | None
    session_version: int | None
    input_mode: str | None
    worker_id: UUID | None
    worker_display_name: str | None
    adapter_type: str | None
    lease_id: UUID | None
    fencing_token: int | None


@dataclass(frozen=True, slots=True)
class ExecutionSummaryProjection:
    execution_id: UUID
    task_id: UUID
    status: str
    attempt: int
    worker_id: UUID | None
    session_id: UUID | None
    failure_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int
    capability_requirements: tuple[str, ...]
    adapter_preferences: tuple[str, ...]
    adapter_forbidden: tuple[str, ...]
    scheduling_status: str
    waiting_reason: str | None
    waiting_details_summary: str | None
    requires_operator: bool
    blocked_since: datetime | None
    pinned_worker_summary: WorkerSummaryProjection | None
    assigned_worker_summary: WorkerSummaryProjection | None
    binding_summary: BindingSummaryProjection | None
    pinned_worker_id: UUID | None = None
    assigned_worker_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CheckpointSummaryProjection:
    execution_id: UUID
    sequence: int
    title: str
    artifact_ids: tuple[UUID, ...]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class UsageSummaryProjection:
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_microunits: int


@dataclass(frozen=True, slots=True)
class WorkerInterventionContextProjection:
    worker_item_id: UUID
    plan_generation: int
    context_version: int
    execution_id: UUID
    session_id: UUID
    session_version: int
    worker_id: UUID


@dataclass(frozen=True, slots=True)
class WorkerActivityProjection:
    activity_id: UUID
    source_type: str
    source_id: UUID
    execution_id: UUID | None
    session_id: UUID | None
    occurred_at: datetime
    kind: str
    title: str
    summary: str
    correlation_id: UUID
    body: str | None
    delivery_status: str | None
    artifact_ids: tuple[UUID, ...]
    trace_descriptor: TraceDescriptorProjection | None


@dataclass(frozen=True, slots=True)
class WorkerActivityPageProjection:
    last_change_cursor: int
    items: tuple[WorkerActivityProjection, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class WorkerWorkspaceProjection:
    workspace_id: UUID
    worker_item: WorkerItemIdentityProjection
    current_placement: WorkerItemPlacementProjection
    placement_history: tuple[WorkerItemPlacementProjection, ...]
    current_runtime: WorkerRuntimeProjection | None
    execution_history: tuple[ExecutionSummaryProjection, ...]
    current_step: str | None
    next_action: str | None
    latest_checkpoint: CheckpointSummaryProjection | None
    usage: UsageSummaryProjection
    artifacts: tuple[ArtifactSummaryProjection, ...]
    evidence: tuple[EvidenceSummaryProjection, ...]
    can_intervene: bool
    read_only_reason: str | None
    intervention_context: WorkerInterventionContextProjection | None
    trace_descriptor: TraceDescriptorProjection | None
    last_change_cursor: int
    as_of: datetime
    plan_sync: WorkerPlanSyncProjection | None = None
    active_handoff: ActiveHandoffProjection | None = None


class WorkerItemProjectionService:
    def __init__(
        self,
        *,
        worker_item: WorkerItemFact,
        placements: Iterable[WorkerItemPlacementFact],
        executions: Iterable[ExecutionFact],
        sessions: Iterable[SessionFact],
        workers: Iterable[WorkerFact],
        bindings: Iterable[BindingFact],
        leases: Iterable[LeaseFact],
        events: Iterable[WorkerEventFact],
        inputs: Iterable[InputFact],
        artifacts: Iterable[ArtifactSummaryProjection],
        evidence: Iterable[EvidenceSummaryProjection],
        last_change_cursor: int,
        as_of: datetime,
        projection_stale: bool = False,
        convergence_facts: PlanConvergenceFacts | None = None,
        active_handoff: ActiveHandoffProjection | None = None,
    ) -> None:
        self.worker_item = worker_item
        self.placements = tuple(placements)
        self.executions = tuple(executions)
        self.sessions = tuple(sessions)
        self.workers = tuple(workers)
        self.bindings = tuple(bindings)
        self.leases = tuple(leases)
        self.events = tuple(events)
        self.inputs = tuple(inputs)
        self.artifacts = tuple(artifacts)
        self.evidence = tuple(evidence)
        self.last_change_cursor = last_change_cursor
        self.as_of = as_of
        self.projection_stale = projection_stale
        self.convergence_facts = convergence_facts
        self.active_handoff = active_handoff
        self._convergence_service = PlanConvergenceProjectionService()

    def compose_workspace(self) -> WorkerWorkspaceProjection:
        placement_values = tuple(
            sorted(
                self.placements,
                key=lambda item: (item.plan_generation, item.plan_snapshot_id),
                reverse=True,
            )
        )
        if not placement_values:
            raise ValueError("WorkerItem has no placement history")
        execution_values = tuple(
            sorted(self.executions, key=_execution_sort_key, reverse=True)
        )
        session_by_execution = _latest_sessions(self.sessions)
        worker_by_id = {worker.id: worker for worker in self.workers}
        binding_by_execution = {item.execution_id: item for item in self.bindings}
        active_lease_by_execution = {
            item.execution_id: item for item in self.leases if item.status == "active"
        }
        current_execution = execution_values[0] if execution_values else None
        current_session = (
            session_by_execution.get(current_execution.id)
            if current_execution is not None
            else None
        )
        current_runtime = _runtime_projection(
            current_execution,
            current_session,
            worker_by_id,
            binding_by_execution,
            active_lease_by_execution,
        )
        current_placement = _placement_projection(placement_values[0])
        placement_history = tuple(
            _placement_projection(item) for item in placement_values
        )
        latest_event = _latest(self.events, key=_event_sort_key)
        latest_checkpoint = _latest_checkpoint(self.events)
        read_only_reason = _read_only_reason(
            self.worker_item,
            current_runtime,
            projection_stale=self.projection_stale,
        )
        worker_id = current_runtime.worker_id if current_runtime else None
        intervention_context = (
            WorkerInterventionContextProjection(
                worker_item_id=self.worker_item.id,
                plan_generation=current_placement.plan_generation,
                context_version=self.worker_item.context_version,
                execution_id=current_runtime.execution_id,
                session_id=current_runtime.session_id,
                session_version=current_runtime.session_version,
                worker_id=worker_id,
            )
            if current_runtime is not None
            and current_runtime.session_id is not None
            and current_runtime.session_version is not None
            and worker_id is not None
            else None
        )
        trace_descriptor = _trace_descriptor(
            self.worker_item.root_task_id, current_runtime, raw_ref=None
        )
        plan_sync = (
            self._convergence_service.compose_worker(
                None, self.worker_item.id, self.convergence_facts
            )
            if self.convergence_facts is not None
            else None
        )
        return WorkerWorkspaceProjection(
            workspace_id=self.worker_item.workspace_id,
            worker_item=_worker_item_projection(self.worker_item),
            current_placement=current_placement,
            placement_history=placement_history,
            current_runtime=current_runtime,
            execution_history=tuple(
                _execution_projection(
                    execution,
                    session_by_execution.get(execution.id),
                    worker_by_id,
                    binding_by_execution,
                    active_lease_by_execution,
                )
                for execution in execution_values
            ),
            current_step=_event_summary(latest_event) if latest_event else None,
            next_action=_next_action(current_execution, current_session),
            latest_checkpoint=latest_checkpoint,
            usage=_usage_summary(self.events),
            artifacts=tuple(
                sorted(self.artifacts, key=lambda item: (item.created_at, item.artifact_id))
            ),
            evidence=tuple(
                sorted(self.evidence, key=lambda item: (item.created_at, item.evidence_id))
            ),
            can_intervene=read_only_reason is None,
            read_only_reason=read_only_reason,
            intervention_context=intervention_context,
            trace_descriptor=trace_descriptor,
            last_change_cursor=self.last_change_cursor,
            as_of=self.as_of,
            plan_sync=plan_sync,
            active_handoff=self.active_handoff,
        )

    def activity_facts(self) -> tuple[WorkerActivityFact, ...]:
        return _activity_facts(
            self.worker_item.root_task_id,
            executions=self.executions,
            sessions=self.sessions,
            events=self.events,
            inputs=self.inputs,
            artifacts=self.artifacts,
            evidence=self.evidence,
        )

    def compose_activity(
        self,
        activities: Iterable[WorkerActivityFact],
        *,
        limit: int,
        after: str | None = None,
    ) -> WorkerActivityPageProjection:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        ordered = tuple(sorted(activities, key=_activity_sort_key))
        after_key = _decode_cursor(after) if after else None
        if after_key is not None:
            ordered = tuple(
                item for item in ordered if _activity_sort_key(item) > after_key
            )
        page = ordered[:limit]
        return WorkerActivityPageProjection(
            last_change_cursor=self.last_change_cursor,
            items=tuple(_activity_projection(item) for item in page),
            next_cursor=(
                _encode_cursor(page[-1])
                if page and len(ordered) > limit
                else None
            ),
        )


def _worker_item_projection(item: WorkerItemFact) -> WorkerItemIdentityProjection:
    return WorkerItemIdentityProjection(
        id=item.id,
        root_task_id=item.root_task_id,
        stable_key=item.stable_key,
        title=item.title,
        responsibility=item.responsibility,
        status=item.status,
        context_version=item.context_version,
        acknowledged_context_version=item.acknowledged_context_version,
        version=item.version,
        updated_at=item.updated_at,
    )


def _placement_projection(
    placement: WorkerItemPlacementFact,
) -> WorkerItemPlacementProjection:
    return WorkerItemPlacementProjection(
        plan_snapshot_id=placement.plan_snapshot_id,
        plan_generation=placement.plan_generation,
        worker_item_key=placement.worker_item_key,
        subtask_key=placement.subtask_key,
        subtask_task_id=placement.subtask_task_id,
        role=placement.role,
        capability_requirements=placement.capability_requirements,
    )


def _runtime_projection(
    execution: ExecutionFact | None,
    session: SessionFact | None,
    workers: Mapping[UUID, WorkerFact],
    bindings: Mapping[UUID, BindingFact],
    leases: Mapping[UUID, LeaseFact],
) -> WorkerRuntimeProjection | None:
    if execution is None:
        return None
    binding = bindings.get(execution.id)
    lease = leases.get(execution.id)
    if binding is not None:
        worker_id = binding.worker_id
    elif lease is not None:
        worker_id = lease.worker_id
    elif execution.assigned_worker_id is not None:
        worker_id = execution.assigned_worker_id
    elif session is not None:
        worker_id = session.worker_id
    else:
        worker_id = execution.pinned_worker_id
    worker = workers.get(worker_id) if worker_id is not None else None
    if binding is not None:
        lease_id = binding.lease_id
        fencing_token = binding.fencing_token
    elif execution.active_lease_id is not None:
        lease_id = execution.active_lease_id
        fencing_token = lease.fencing_token if lease is not None else None
    elif lease is not None:
        lease_id = lease.id
        fencing_token = lease.fencing_token
    else:
        lease_id = None
        fencing_token = None
    return WorkerRuntimeProjection(
        execution_id=execution.id,
        task_id=execution.task_id,
        execution_status=execution.status,
        attempt=execution.attempt,
        session_id=session.id if session else None,
        session_status=session.status if session else None,
        session_version=session.version if session else None,
        input_mode=session.input_mode if session else None,
        worker_id=worker_id,
        worker_display_name=worker.display_name if worker else None,
        adapter_type=worker.adapter_type if worker else None,
        lease_id=lease_id,
        fencing_token=fencing_token,
    )


def _execution_projection(
    execution: ExecutionFact,
    session: SessionFact | None,
    workers: Mapping[UUID, WorkerFact],
    bindings: Mapping[UUID, BindingFact],
    leases: Mapping[UUID, LeaseFact],
) -> ExecutionSummaryProjection:
    runtime = _runtime_projection(execution, session, workers, bindings, leases)
    scheduling_status = _scheduling_status(execution)
    return ExecutionSummaryProjection(
        execution_id=execution.id,
        task_id=execution.task_id,
        status=execution.status,
        attempt=execution.attempt,
        worker_id=runtime.worker_id if runtime else None,
        session_id=runtime.session_id if runtime else None,
        failure_code=execution.failure_code,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        created_at=execution.created_at,
        updated_at=execution.updated_at,
        version=execution.version,
        capability_requirements=execution.capability_requirements,
        adapter_preferences=execution.adapter_preferences,
        adapter_forbidden=execution.adapter_forbidden,
        scheduling_status=scheduling_status,
        waiting_reason=execution.waiting_reason if scheduling_status == "waiting" else None,
        waiting_details_summary=(
            execution.waiting_details_summary
            if scheduling_status == "waiting"
            else None
        ),
        requires_operator=(
            execution.requires_operator if scheduling_status == "waiting" else False
        ),
        blocked_since=(
            execution.blocked_since if scheduling_status == "waiting" else None
        ),
        pinned_worker_summary=_worker_summary(
            workers.get(execution.pinned_worker_id)
            if execution.pinned_worker_id is not None
            else None
        ),
        assigned_worker_summary=_worker_summary(
            workers.get(execution.assigned_worker_id)
            if execution.assigned_worker_id is not None
            else None
        ),
        binding_summary=_binding_summary(bindings.get(execution.id)),
        pinned_worker_id=execution.pinned_worker_id,
        assigned_worker_id=execution.assigned_worker_id,
    )


def _scheduling_status(execution: ExecutionFact) -> str:
    if execution.status in {"succeeded", "failed", "canceled", "lost"}:
        return "terminal"
    if execution.status == "queued":
        return execution.scheduling_status or "ready"
    return "assigned"


def _worker_summary(worker: WorkerFact | None) -> WorkerSummaryProjection | None:
    if worker is None:
        return None
    return WorkerSummaryProjection(
        id=worker.id,
        display_name=worker.display_name,
        adapter_type=worker.adapter_type,
        status=worker.status,
        desired_state=worker.desired_state,
        current_load=worker.current_load,
        capacity=worker.capacity,
    )


def _binding_summary(binding: BindingFact | None) -> BindingSummaryProjection | None:
    if (
        binding is None
        or binding.id is None
        or binding.adapter_module_id is None
        or binding.workspace_module_id is None
        or binding.adapter_config_version is None
        or binding.workspace_config_version is None
    ):
        return None
    return BindingSummaryProjection(
        id=binding.id,
        worker_id=binding.worker_id,
        adapter_module_id=binding.adapter_module_id,
        workspace_module_id=binding.workspace_module_id,
        adapter_config_version=binding.adapter_config_version,
        workspace_config_version=binding.workspace_config_version,
        fencing_token=binding.fencing_token,
        version=binding.version,
    )


def _latest_sessions(values: Iterable[SessionFact]) -> dict[UUID, SessionFact]:
    result: dict[UUID, SessionFact] = {}
    for session in values:
        existing = result.get(session.execution_id)
        if existing is None or (session.last_activity_at, session.id) > (
            existing.last_activity_at,
            existing.id,
        ):
            result[session.execution_id] = session
    return result


def _read_only_reason(
    worker_item: WorkerItemFact,
    runtime: WorkerRuntimeProjection | None,
    *,
    projection_stale: bool,
) -> str | None:
    if worker_item.status == "retired":
        return "worker_item_retired"
    if worker_item.status == "paused":
        return "worker_item_paused"
    if worker_item.context_version != worker_item.acknowledged_context_version:
        return "context_not_acknowledged"
    if projection_stale:
        return "projection_stale"
    if (
        runtime is None
        or runtime.session_id is None
        or runtime.session_status != "active"
    ):
        return "no_active_session"
    if runtime.execution_status in {"succeeded", "failed", "canceled"}:
        return "execution_not_active"
    if runtime.input_mode == "unsupported":
        return "adapter_input_unsupported"
    return None


def _next_action(
    execution: ExecutionFact | None, session: SessionFact | None
) -> str | None:
    if execution is None:
        return "等待 WorkerItem 首次执行"
    if execution.status in {"queued", "leased"}:
        return "等待 Agent 开始执行"
    if execution.status == "running":
        return "等待当前 Agent 更新"
    if execution.status == "waiting_user":
        return "等待用户输入"
    if execution.status in {"failed", "lost"}:
        return "检查失败原因并决定是否重试"
    if execution.status == "succeeded":
        return "验收当前结果"
    if session is None:
        return "等待新的 Agent Session"
    return None


def _latest_checkpoint(
    events: Iterable[WorkerEventFact],
) -> CheckpointSummaryProjection | None:
    checkpoints = [event for event in events if event.event_type == "checkpoint.created"]
    event = max(
        checkpoints,
        key=lambda item: (item.occurred_at, item.sequence, item.id),
        default=None,
    )
    if event is None:
        return None
    return CheckpointSummaryProjection(
        execution_id=event.execution_id,
        sequence=event.sequence,
        title=_event_summary(event),
        artifact_ids=_uuid_values(event.payload.get("artifact_ids")),
        occurred_at=event.occurred_at,
    )


def _usage_summary(events: Iterable[WorkerEventFact]) -> UsageSummaryProjection:
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cost_microunits": 0}
    for event in events:
        usage = event.payload.get("trace_usage") or event.payload.get("usage")
        if not isinstance(usage, Mapping):
            continue
        for key in totals:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] += value
    return UsageSummaryProjection(**totals)


def _activity_facts(
    root_task_id: UUID,
    *,
    executions: Iterable[ExecutionFact],
    sessions: Iterable[SessionFact],
    events: Iterable[WorkerEventFact],
    inputs: Iterable[InputFact],
    artifacts: Iterable[ArtifactSummaryProjection],
    evidence: Iterable[EvidenceSummaryProjection],
) -> tuple[WorkerActivityFact, ...]:
    result: list[WorkerActivityFact] = []
    for session in sessions:
        result.append(
            WorkerActivityFact(
                id=uuid5(NAMESPACE_URL, f"sidera:session-start:{session.id}"),
                source_type="session",
                source_id=session.id,
                execution_id=session.execution_id,
                session_id=session.id,
                occurred_at=session.created_at,
                kind="session.started",
                title="Session started",
                summary=f"Agent Session {session.adapter_session_id} 已启动",
                correlation_id=session.id,
                source_priority=10,
                body=None,
                delivery_status=None,
                trace_descriptor=_trace_descriptor(
                    root_task_id,
                    _runtime_from_ids(session.execution_id, session.id),
                    raw_ref=None,
                ),
            )
        )
        if session.status != "active":
            result.append(
                WorkerActivityFact(
                    id=uuid5(NAMESPACE_URL, f"sidera:session-close:{session.id}"),
                    source_type="session",
                    source_id=session.id,
                    execution_id=session.execution_id,
                    session_id=session.id,
                    occurred_at=session.last_activity_at,
                    kind="session.closed",
                    title="Session closed",
                    summary=f"Agent Session {session.adapter_session_id} 已结束",
                    correlation_id=session.id,
                    source_priority=11,
                    body=None,
                    delivery_status=None,
                    trace_descriptor=_trace_descriptor(
                        root_task_id,
                        _runtime_from_ids(session.execution_id, session.id),
                        raw_ref=None,
                    ),
                )
            )
    for execution in executions:
        result.append(
            WorkerActivityFact(
                id=uuid5(NAMESPACE_URL, f"sidera:execution-created:{execution.id}"),
                source_type="execution",
                source_id=execution.id,
                execution_id=execution.id,
                session_id=None,
                occurred_at=execution.created_at,
                kind="execution.created",
                title="Execution created",
                summary=execution.status,
                correlation_id=execution.id,
                source_priority=20,
                body=None,
                delivery_status=None,
                trace_descriptor=None,
            )
        )
        if execution.status not in {"queued", "leased"}:
            result.append(
                WorkerActivityFact(
                    id=uuid5(NAMESPACE_URL, f"sidera:execution-status:{execution.id}"),
                    source_type="execution",
                    source_id=execution.id,
                    execution_id=execution.id,
                    session_id=None,
                    occurred_at=execution.updated_at,
                    kind=f"execution.{execution.status}",
                    title="Execution status",
                    summary=(
                        execution.failure_code
                        if execution.failure_code
                        else f"Execution 状态：{execution.status}"
                    ),
                    correlation_id=execution.id,
                    source_priority=21,
                    body=None,
                    delivery_status=None,
                    trace_descriptor=None,
                )
            )
    for item in inputs:
        result.append(
            WorkerActivityFact(
                id=item.id,
                source_type="execution_input",
                source_id=item.id,
                execution_id=item.execution_id,
                session_id=item.session_id,
                occurred_at=item.updated_at,
                kind="user_input",
                title="用户介入",
                summary=item.text,
                correlation_id=item.operation_id or item.id,
                source_priority=30,
                body=item.text,
                delivery_status=item.status,
                trace_descriptor=None,
            )
        )
    for event in events:
        result.append(
            WorkerActivityFact(
                id=event.id,
                source_type="execution_event",
                source_id=event.id,
                execution_id=event.execution_id,
                session_id=event.session_id,
                occurred_at=event.occurred_at,
                kind=_event_kind(event.event_type),
                title=str(event.payload.get("title") or event.event_type),
                summary=_event_summary(event),
                correlation_id=event.id,
                source_priority=40,
                body=None,
                delivery_status=None,
                trace_descriptor=_trace_descriptor(
                    root_task_id,
                    _runtime_from_ids(event.execution_id, event.session_id),
                    raw_ref=event.raw_ref,
                ),
                artifact_ids=_uuid_values(event.payload.get("artifact_ids")),
            )
        )
    for artifact in artifacts:
        result.append(
            WorkerActivityFact(
                id=artifact.artifact_id,
                source_type="artifact",
                source_id=artifact.artifact_id,
                execution_id=artifact.execution_id,
                session_id=None,
                occurred_at=artifact.created_at,
                kind="artifact",
                title=artifact.artifact_type,
                summary=f"生成 {artifact.artifact_type}",
                correlation_id=artifact.artifact_id,
                source_priority=50,
                body=None,
                delivery_status=None,
                trace_descriptor=None,
                artifact_ids=(artifact.artifact_id,),
            )
        )
    for claim in evidence:
        result.append(
            WorkerActivityFact(
                id=claim.evidence_id,
                source_type="evidence",
                source_id=claim.evidence_id,
                execution_id=claim.execution_id,
                session_id=None,
                occurred_at=claim.created_at,
                kind="evidence",
                title=claim.acceptance_ref,
                summary=claim.conclusion,
                correlation_id=claim.evidence_id,
                source_priority=60,
                body=None,
                delivery_status=None,
                trace_descriptor=None,
                artifact_ids=claim.artifact_ids,
            )
        )
    return tuple(result)


def _activity_projection(item: WorkerActivityFact) -> WorkerActivityProjection:
    return WorkerActivityProjection(
        activity_id=item.id,
        source_type=item.source_type,
        source_id=item.source_id,
        execution_id=item.execution_id,
        session_id=item.session_id,
        occurred_at=item.occurred_at,
        kind=item.kind,
        title=item.title,
        summary=item.summary,
        correlation_id=item.correlation_id,
        body=item.body,
        delivery_status=item.delivery_status,
        artifact_ids=item.artifact_ids,
        trace_descriptor=item.trace_descriptor,
    )


def _trace_descriptor(
    root_task_id: UUID,
    runtime: WorkerRuntimeProjection | None,
    *,
    raw_ref: str | None,
) -> TraceDescriptorProjection | None:
    if runtime is None or runtime.session_id is None:
        return None
    return TraceDescriptorProjection(
        root_task_id=root_task_id,
        execution_id=runtime.execution_id,
        session_id=runtime.session_id,
        route=(
            f"/api/v1/tasks/{root_task_id}/executions/{runtime.execution_id}"
            f"/sessions/{runtime.session_id}/trace"
        ),
        raw_ref=raw_ref,
    )


def _runtime_from_ids(execution_id: UUID, session_id: UUID) -> WorkerRuntimeProjection:
    return WorkerRuntimeProjection(
        execution_id=execution_id,
        task_id=UUID(int=0),
        execution_status="unknown",
        attempt=0,
        session_id=session_id,
        session_status=None,
        session_version=None,
        input_mode=None,
        worker_id=None,
        worker_display_name=None,
        adapter_type=None,
        lease_id=None,
        fencing_token=None,
    )


def _event_kind(event_type: str) -> str:
    normalized = event_type.lower()
    if normalized.startswith("checkpoint"):
        return "checkpoint"
    if normalized.startswith(("error", "failed")):
        return "error"
    if "tool" in normalized:
        return "tool"
    if normalized in {"assistant.summary", "progress.summary"}:
        return "assistant_summary"
    if normalized.startswith(("execution", "lease", "scheduler")):
        return "scheduler"
    return "event"


def _event_summary(event: WorkerEventFact) -> str:
    for key in ("summary", "title", "text", "message", "tool", "kind"):
        value = event.payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:4096]
    return event.event_type


def _execution_sort_key(value: ExecutionFact) -> tuple[datetime, datetime, UUID]:
    return value.updated_at, value.created_at, value.id


def _event_sort_key(value: WorkerEventFact) -> tuple[datetime, int, UUID]:
    return value.occurred_at, value.sequence, value.id


def _activity_sort_key(value: WorkerActivityFact) -> tuple[datetime, int, UUID]:
    return value.occurred_at, value.source_priority, value.id


def _latest(values: Iterable[Any], *, key: Any) -> Any:
    return max(values, key=key, default=None)


def _uuid_values(values: Any) -> tuple[UUID, ...]:
    if not isinstance(values, (list, tuple, set)):
        return ()
    result: list[UUID] = []
    for value in values:
        try:
            result.append(value if isinstance(value, UUID) else UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return tuple(result)


def _encode_cursor(value: WorkerActivityFact) -> str:
    payload = {
        "occurred_at": _as_utc(value.occurred_at).isoformat(),
        "source_priority": value.source_priority,
        "stable_id": str(value.id),
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, int, UUID]:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(payload, dict):
            raise TypeError
        occurred_at = datetime.fromisoformat(str(payload["occurred_at"]))
        source_priority = int(payload["source_priority"])
        stable_id = UUID(str(payload["stable_id"]))
        if source_priority < 0 or occurred_at.tzinfo is None:
            raise ValueError
        return _as_utc(occurred_at), source_priority, stable_id
    except (
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise InvalidActivityCursor("invalid activity cursor") from exc


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
