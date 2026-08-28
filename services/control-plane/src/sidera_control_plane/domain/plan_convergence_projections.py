from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sidera_control_plane.domain.plan_normalization import CanonicalTaskPlan

_IN_FLIGHT_STATUSES = frozenset(
    {
        "pending",
        "waiting_safe_boundary",
        "applying",
        "waiting_activation",
    }
)
_UNKNOWN_STATUSES = frozenset({"blocked_unknown"})
_TERMINAL_STATUSES = frozenset({"converged", "failed", "blocked_unknown"})
_ATTENTION_STATUSES = frozenset(
    {"proposal_ready", "conflicted", "waiting_for_user", "failed"}
)


@dataclass(frozen=True, slots=True)
class PlanHeadProjection:
    active_snapshot_id: UUID
    active_generation: int
    active_snapshot_hash: str
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PlanRevisionProjection:
    planning_request_id: UUID
    status: str
    proposal_revision: int | None
    proposal_content_hash: str | None
    base_generation: int | None
    target_generation: int | None
    version: int
    correlation_id: UUID
    updated_at: datetime
    subtask_path: tuple[str, ...] = ()
    summary: str | None = None
    action: str | None = None


@dataclass(frozen=True, slots=True)
class PlanTransitionProjectionFact:
    id: UUID
    from_generation: int
    to_generation: int
    status: str
    started_at: datetime
    completed_at: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class WorkerTransitionProjectionFact:
    id: UUID
    plan_transition_id: UUID
    worker_item_id: UUID
    subtask_key: str | None
    action: str
    status: str
    writer_state: str
    target_context_version: int
    current_context_version: int
    acknowledged_context_version: int
    patch_id: UUID | None
    patch_acknowledged: bool
    target_execution_id: UUID | None
    target_session_id: UUID | None
    queued_input_ids: tuple[UUID, ...]
    superseded_input_ids: tuple[UUID, ...]
    failure_code: str | None
    started_at: datetime | None
    completed_at: datetime | None
    version: int
    source_execution_id: UUID | None = None
    source_session_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PlanConvergenceFacts:
    head: PlanHeadProjection | None
    revision: PlanRevisionProjection | None
    transition: PlanTransitionProjectionFact | None
    workers: tuple[WorkerTransitionProjectionFact, ...]


@dataclass(frozen=True, slots=True)
class PlanRevisionAttentionProjection:
    planning_request_id: UUID
    status: str
    subtask_path: tuple[str, ...]
    summary: str
    action: str
    deep_link: str
    correlation_id: UUID
    resource_version: int
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class PlanConvergenceProjection:
    plan_transition_id: UUID | None
    from_generation: int | None
    target_generation: int | None
    status: str
    total_count: int
    converged_count: int
    blocked_count: int
    unknown_count: int
    in_flight_count: int
    has_in_flight: bool
    has_unknown: bool
    has_active_fence: bool
    confirmation_blocked: bool
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class PlanRootProjection:
    plan_head: PlanHeadProjection | None
    active_revision: PlanRevisionProjection | None
    convergence: PlanConvergenceProjection
    revision_attention: PlanRevisionAttentionProjection | None


@dataclass(frozen=True, slots=True)
class SubtaskConvergenceProjection:
    subtask_key: str
    status: str
    plan_transition_id: UUID | None
    source_generation: int | None
    target_generation: int | None
    total_count: int
    converged_count: int
    blocked_count: int
    unknown_count: int
    blocked_predecessor_paths: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class WorkerPlanSyncProjection:
    worker_transition_id: UUID
    plan_transition_id: UUID
    action: str
    status: str
    writer_state: str
    source_generation: int
    target_generation: int
    current_context_version: int
    acknowledged_context_version: int
    target_context_version: int
    patch_id: UUID | None
    patch_acknowledged: bool
    source_execution_id: UUID | None
    source_session_id: UUID | None
    target_execution_id: UUID | None
    target_session_id: UUID | None
    continuation_execution_id: UUID | None
    continuation_session_id: UUID | None
    queued_input_ids: tuple[UUID, ...]
    superseded_input_ids: tuple[UUID, ...]
    failure_code: str | None
    started_at: datetime | None
    completed_at: datetime | None
    trace_href: None = None


class PlanConvergenceProjectionService:
    def compose_root(
        self, plan: CanonicalTaskPlan, facts: PlanConvergenceFacts
    ) -> PlanRootProjection:
        return PlanRootProjection(
            plan_head=facts.head,
            active_revision=facts.revision,
            convergence=self._convergence(facts.transition, facts.workers, facts.head),
            revision_attention=self._attention(plan, facts.revision),
        )

    def compose_subtask(
        self,
        plan: CanonicalTaskPlan,
        subtask_key: str,
        facts: PlanConvergenceFacts,
    ) -> SubtaskConvergenceProjection:
        branch_workers = tuple(
            worker
            for worker in facts.workers
            if worker.subtask_key == subtask_key
        )
        branch_status = _status_for_workers(branch_workers)
        branch_ids = {worker.worker_item_id for worker in branch_workers}
        predecessor_paths: list[tuple[str, ...]] = []
        subtask_titles = {item.key: item.title for item in plan.subtasks}
        status_by_subtask = self._status_by_subtask(plan, facts)

        for dependency in plan.subtask_dependencies:
            if dependency.downstream_subtask_key != subtask_key:
                continue
            upstream_status = status_by_subtask.get(dependency.upstream_subtask_key)
            if upstream_status in {"blocked_unknown", "degraded"}:
                path = (subtask_titles.get(dependency.upstream_subtask_key, dependency.upstream_subtask_key),)
                if path not in predecessor_paths:
                    predecessor_paths.append(path)

        worker_by_key = {item.key: item for item in plan.worker_items}
        status_by_worker_key = {
            item.key: _status_for_workers(
                worker
                for worker in facts.workers
                if worker.worker_item_id == item.worker_item_id
            )
            for item in plan.worker_items
        }
        for dependency in plan.worker_item_dependencies:
            downstream = worker_by_key.get(dependency.downstream_worker_item_key)
            if downstream is None or downstream.worker_item_id not in branch_ids:
                continue
            upstream = worker_by_key.get(dependency.upstream_worker_item_key)
            if upstream is None:
                continue
            if status_by_worker_key.get(upstream.key) not in {"blocked_unknown", "degraded"}:
                continue
            subtask_title = subtask_titles.get(upstream.subtask_key, upstream.subtask_key)
            if (subtask_title,) in predecessor_paths:
                continue
            path = (subtask_title, upstream.title)
            if path not in predecessor_paths:
                predecessor_paths.append(path)

        return SubtaskConvergenceProjection(
            subtask_key=subtask_key,
            status=(
                "waiting_dependency"
                if branch_status == "idle" and predecessor_paths
                else branch_status
            ),
            plan_transition_id=(
                facts.transition.id if facts.transition is not None else None
            ),
            source_generation=(
                facts.transition.from_generation if facts.transition is not None else None
            ),
            target_generation=(
                facts.transition.to_generation if facts.transition is not None else None
            ),
            total_count=len(branch_workers),
            converged_count=sum(item.status == "converged" for item in branch_workers),
            blocked_count=sum(
                item.status in {"failed", "blocked_unknown"} for item in branch_workers
            ),
            unknown_count=sum(
                item.status in _UNKNOWN_STATUSES or item.writer_state == "unknown"
                for item in branch_workers
            ),
            blocked_predecessor_paths=tuple(predecessor_paths),
        )

    def compose_worker(
        self,
        plan: CanonicalTaskPlan | None,
        worker_item_id: UUID,
        facts: PlanConvergenceFacts,
    ) -> WorkerPlanSyncProjection | None:
        del plan
        if facts.transition is None:
            return None
        worker = next(
            (item for item in facts.workers if item.worker_item_id == worker_item_id),
            None,
        )
        if worker is None:
            return None
        return WorkerPlanSyncProjection(
            worker_transition_id=worker.id,
            plan_transition_id=worker.plan_transition_id,
            action=worker.action,
            status=worker.status,
            writer_state=worker.writer_state,
            source_generation=facts.transition.from_generation,
            target_generation=facts.transition.to_generation,
            current_context_version=worker.current_context_version,
            acknowledged_context_version=worker.acknowledged_context_version,
            target_context_version=worker.target_context_version,
            patch_id=worker.patch_id,
            patch_acknowledged=worker.patch_acknowledged,
            source_execution_id=worker.source_execution_id,
            source_session_id=worker.source_session_id,
            target_execution_id=worker.target_execution_id,
            target_session_id=worker.target_session_id,
            continuation_execution_id=(
                worker.target_execution_id if worker.action == "continue" else None
            ),
            continuation_session_id=(
                worker.target_session_id if worker.action == "continue" else None
            ),
            queued_input_ids=worker.queued_input_ids,
            superseded_input_ids=worker.superseded_input_ids,
            failure_code=worker.failure_code,
            started_at=worker.started_at,
            completed_at=worker.completed_at,
        )

    def _status_by_subtask(
        self, plan: CanonicalTaskPlan, facts: PlanConvergenceFacts
    ) -> dict[str, str]:
        return {
            subtask.key: _status_for_workers(
                worker
                for worker in facts.workers
                if worker.subtask_key == subtask.key
            )
            for subtask in plan.subtasks
        }

    @staticmethod
    def _convergence(
        transition: PlanTransitionProjectionFact | None,
        workers: Iterable[WorkerTransitionProjectionFact],
        head: PlanHeadProjection | None,
    ) -> PlanConvergenceProjection:
        worker_values = tuple(workers)
        has_in_flight = any(item.status in _IN_FLIGHT_STATUSES for item in worker_values)
        has_unknown = any(
            item.status in _UNKNOWN_STATUSES or item.writer_state == "unknown"
            for item in worker_values
        )
        has_active_fence = any(
            item.writer_state in {"active", "fenced"}
            and item.status not in {"converged", "failed"}
            for item in worker_values
        )
        return PlanConvergenceProjection(
            plan_transition_id=transition.id if transition is not None else None,
            from_generation=(
                transition.from_generation if transition is not None else None
            ),
            target_generation=(
                transition.to_generation
                if transition is not None
                else head.active_generation if head is not None else None
            ),
            status=transition.status if transition is not None else "idle",
            total_count=len(worker_values),
            converged_count=sum(item.status == "converged" for item in worker_values),
            blocked_count=sum(
                item.status in {"failed", "blocked_unknown"} for item in worker_values
            ),
            unknown_count=sum(
                item.status in _UNKNOWN_STATUSES or item.writer_state == "unknown"
                for item in worker_values
            ),
            in_flight_count=sum(item.status in _IN_FLIGHT_STATUSES for item in worker_values),
            has_in_flight=has_in_flight,
            has_unknown=has_unknown,
            has_active_fence=has_active_fence,
            confirmation_blocked=has_in_flight or has_unknown or has_active_fence,
            started_at=transition.started_at if transition is not None else None,
            completed_at=transition.completed_at if transition is not None else None,
        )

    @staticmethod
    def _attention(
        plan: CanonicalTaskPlan, revision: PlanRevisionProjection | None
    ) -> PlanRevisionAttentionProjection | None:
        if revision is None or revision.status not in _ATTENTION_STATUSES:
            return None
        summary = revision.summary or _revision_summary(revision.status)
        action = revision.action or _revision_action(revision.status)
        return PlanRevisionAttentionProjection(
            planning_request_id=revision.planning_request_id,
            status=revision.status,
            subtask_path=revision.subtask_path,
            summary=summary,
            action=action,
            deep_link=(
                f"/workspaces/{plan.workspace_id}/tasks/{plan.root_task_id}/"
                f"revisions/{revision.planning_request_id}"
            ),
            correlation_id=revision.correlation_id,
            resource_version=revision.version,
            occurred_at=revision.updated_at,
        )


def _status_for_workers(
    workers: Iterable[WorkerTransitionProjectionFact],
) -> str:
    values = tuple(workers)
    if not values:
        return "idle"
    if any(item.status in _UNKNOWN_STATUSES or item.writer_state == "unknown" for item in values):
        return "blocked_unknown"
    if any(item.status == "failed" for item in values):
        return "degraded"
    if all(item.status == "converged" for item in values):
        return "converged"
    if any(item.status in _IN_FLIGHT_STATUSES for item in values):
        return "converging"
    return "waiting"


def _revision_summary(status: str) -> str:
    return {
        "proposal_ready": "新的计划方案已就绪",
        "conflicted": "当前计划已变化，需要重新审阅",
        "waiting_for_user": "规划需要用户决定",
        "failed": "规划失败，需要处理",
    }.get(status, "计划方案需要关注")


def _revision_action(status: str) -> str:
    return {
        "proposal_ready": "review_revision",
        "conflicted": "refresh_revision",
        "waiting_for_user": "answer_clarification",
        "failed": "retry_revision",
    }.get(status, "review_revision")
