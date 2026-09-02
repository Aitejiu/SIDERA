from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sidera_control_plane.domain.plan_normalization import CanonicalTaskPlan

_MIN_DATETIME = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class CollaborationRunProjectionFact:
    id: UUID
    workspace_id: UUID
    root_task_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    status: str
    provider: str
    interface_version: str
    config_version: int
    strategy_label: str
    policy_enabled: bool
    blocked_reason: str | None
    updated_at: datetime | None
    strategy_by_subtask: Mapping[str, str] | None = None
    latest_decision_summary: str | None = None
    latest_decision_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CollaborationRequestProjectionFact:
    id: UUID
    subtask_key: str
    worker_item_key: str
    worker_item_id: UUID
    status: str
    collaboration_gate_mode: str
    gate_reason: str
    version: int
    expected_artifact_refs: tuple[str, ...]
    expected_evidence_refs: tuple[str, ...]
    updated_at: datetime | None
    execution_lineage_id: UUID | None = None
    first_execution_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CollaborationRequestDependencyProjectionFact:
    downstream_request_id: UUID
    upstream_request_id: UUID
    enforcement: str
    required_artifact_refs: tuple[str, ...]
    required_evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CollaborationExecutionProjectionFact:
    request_id: UUID
    execution_id: UUID
    lineage_id: UUID
    status: str
    attempt: int
    failure_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime | None
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CollaborationDecisionProjectionFact:
    id: UUID
    sequence: int
    outcome: str
    reason_code: str
    summary: str
    projection_id: UUID
    projection_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CollaborationIntentProjectionFact:
    id: UUID
    decision_id: UUID
    target_request_id: UUID
    ordinal: int
    summary: str


@dataclass(frozen=True, slots=True)
class CollaborationIntentApplicationProjectionFact:
    intent_id: UUID
    status: str
    result_code: str
    result_summary: str
    release_id: UUID | None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class CollaborationLinkProjection:
    subtask_key: str
    title: str
    href: str


@dataclass(frozen=True, slots=True)
class CollaborationDecisionApplicationProjection:
    intent_id: UUID
    status: str
    result_code: str
    result_summary: str
    release_id: UUID | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class CollaborationDecisionProjection:
    decision_id: UUID
    sequence: int
    outcome: str
    reason_code: str
    summary: str
    created_at: datetime
    projection_id: UUID
    projection_hash: str
    target_paths: tuple[CollaborationLinkProjection, ...]
    applications: tuple[CollaborationDecisionApplicationProjection, ...]


@dataclass(frozen=True, slots=True)
class CollaborationDecisionPageProjection:
    items: tuple[CollaborationDecisionProjection, ...]
    next_after: tuple[datetime, UUID] | None


@dataclass(frozen=True, slots=True)
class BlockedSubtaskProjection:
    subtask_key: str
    title: str
    reason: str
    last_failure_at: datetime | None
    href: str


@dataclass(frozen=True, slots=True)
class RootCollaborationSummaryProjection:
    enabled: bool
    status: str
    strategy_label: str
    run_id: UUID
    plan_generation: int
    released_count: int
    running_count: int
    waiting_count: int
    completed_count: int
    blocked_subtask_count: int
    blocked_subtasks: tuple[BlockedSubtaskProjection, ...]
    latest_decision_summary: str | None
    latest_decision_at: datetime | None
    attention_required: bool
    detail_href: str


@dataclass(frozen=True, slots=True)
class SubtaskCollaborationSummaryProjection:
    enabled: bool
    effective_strategy: str
    required_input_count: int
    satisfied_input_count: int
    waiting_upstream_subtasks: tuple[CollaborationLinkProjection, ...]
    missing_artifact_refs: tuple[str, ...]
    missing_evidence_refs: tuple[str, ...]
    released_worker_item_count: int
    waiting_worker_item_count: int


@dataclass(frozen=True, slots=True)
class WorkerItemCollaborationSummaryProjection:
    enabled: bool
    state: str
    waiting_summary: str | None
    upstream_links: tuple[CollaborationLinkProjection, ...]


@dataclass(frozen=True, slots=True)
class CollaborationProjectionFacts:
    plan: CanonicalTaskPlan
    run: CollaborationRunProjectionFact
    requests: tuple[CollaborationRequestProjectionFact, ...]
    dependencies: tuple[CollaborationRequestDependencyProjectionFact, ...]
    executions: tuple[CollaborationExecutionProjectionFact, ...]
    released_request_ids: tuple[UUID, ...]
    decisions: tuple[CollaborationDecisionProjectionFact, ...]
    intents: tuple[CollaborationIntentProjectionFact, ...]
    applications: tuple[CollaborationIntentApplicationProjectionFact, ...]


class CollaborationProjectionService:
    """Compose bounded, current-plan collaboration views from query facts."""

    def root_summary(
        self, facts: CollaborationProjectionFacts
    ) -> RootCollaborationSummaryProjection:
        executions = self._latest_executions(facts.executions)
        released = set(facts.released_request_ids)
        states = {
            request.id: self._request_state(
                facts,
                request,
                executions=executions,
                released=released,
            )
            for request in facts.requests
        }
        running_count = sum(
            _is_running(executions.get(request.id)) for request in facts.requests
        )
        completed_count = sum(
            _is_completed(executions.get(request.id)) for request in facts.requests
        )
        waiting_count = sum(
            state.startswith("waiting_")
            for state in states.values()
        )
        blocked_subtasks = self._blocked_subtasks(
            facts,
            states=states,
            executions=executions,
        )
        attention_required = facts.run.status == "blocked" or any(
            _is_attention_reason(request.gate_reason)
            for request in facts.requests
        )
        if attention_required:
            status = "attention"
        elif facts.run.status == "completed" or (
            facts.requests and completed_count == len(facts.requests)
        ):
            status = "completed"
        elif running_count:
            status = "running"
        else:
            status = "waiting"
        latest_decision = max(
            facts.decisions,
            key=lambda item: (item.created_at, item.sequence, str(item.id)),
            default=None,
        )
        return RootCollaborationSummaryProjection(
            enabled=facts.run.policy_enabled,
            status=status,
            strategy_label=facts.run.strategy_label,
            run_id=facts.run.id,
            plan_generation=facts.run.plan_generation,
            released_count=len(released),
            running_count=running_count,
            waiting_count=waiting_count,
            completed_count=completed_count,
            blocked_subtask_count=len(blocked_subtasks),
            blocked_subtasks=blocked_subtasks,
            latest_decision_summary=(
                latest_decision.summary
                if latest_decision is not None
                else facts.run.latest_decision_summary
            ),
            latest_decision_at=(
                latest_decision.created_at
                if latest_decision is not None
                else facts.run.latest_decision_at
            ),
            attention_required=attention_required,
            detail_href=_detail_href(facts.plan.workspace_id, facts.plan.root_task_id),
        )

    def subtask_summary(
        self, facts: CollaborationProjectionFacts, *, subtask_key: str
    ) -> SubtaskCollaborationSummaryProjection | None:
        if not facts.run.policy_enabled:
            return None
        request_values = tuple(
            request for request in facts.requests if request.subtask_key == subtask_key
        )
        request_ids = {request.id for request in request_values}
        request_map = self._request_map(facts.requests)
        executions = self._latest_executions(facts.executions)
        released = set(facts.released_request_ids)
        required = [
            dependency
            for dependency in facts.dependencies
            if dependency.downstream_request_id in request_ids
            and dependency.enforcement == "hard"
        ]
        waiting_links: list[CollaborationLinkProjection] = []
        missing_artifacts: list[str] = []
        missing_evidence: list[str] = []
        satisfied = 0
        for dependency in required:
            upstream = request_map.get(dependency.upstream_request_id)
            decision = self._dependency_decision(
                dependency,
                executions=executions,
                facts=facts,
            )
            if decision.ready:
                satisfied += 1
                continue
            if upstream is not None:
                link = self._subtask_link(facts.plan, upstream.subtask_key)
                if link not in waiting_links:
                    waiting_links.append(link)
            _append_unique(missing_artifacts, decision.missing_artifact_refs)
            _append_unique(missing_evidence, decision.missing_evidence_refs)
        states = {
            request.id: self._request_state(
                facts,
                request,
                executions=executions,
                released=released,
            )
            for request in request_values
        }
        return SubtaskCollaborationSummaryProjection(
            enabled=True,
            effective_strategy=self._strategy_for(facts.run, subtask_key),
            required_input_count=len(required),
            satisfied_input_count=satisfied,
            waiting_upstream_subtasks=tuple(waiting_links),
            missing_artifact_refs=tuple(missing_artifacts),
            missing_evidence_refs=tuple(missing_evidence),
            released_worker_item_count=sum(
                request.id in released for request in request_values
            ),
            waiting_worker_item_count=sum(
                state.startswith("waiting_") for state in states.values()
            ),
        )

    def worker_summary(
        self, facts: CollaborationProjectionFacts, *, worker_item_id: UUID
    ) -> WorkerItemCollaborationSummaryProjection | None:
        request = next(
            (
                item
                for item in facts.requests
                if item.worker_item_id == worker_item_id
            ),
            None,
        )
        if request is None or not facts.run.policy_enabled:
            return None
        executions = self._latest_executions(facts.executions)
        released = set(facts.released_request_ids)
        state = self._request_state(
            facts,
            request,
            executions=executions,
            released=released,
        )
        request_map = self._request_map(facts.requests)
        upstream_links: list[CollaborationLinkProjection] = []
        for dependency in facts.dependencies:
            if (
                dependency.downstream_request_id != request.id
                or dependency.enforcement != "hard"
            ):
                continue
            upstream = request_map.get(dependency.upstream_request_id)
            if upstream is not None:
                link = self._subtask_link(facts.plan, upstream.subtask_key)
                if link not in upstream_links:
                    upstream_links.append(link)
        return WorkerItemCollaborationSummaryProjection(
            enabled=True,
            state=state,
            waiting_summary=_state_summary(state, request.gate_reason),
            upstream_links=tuple(upstream_links),
        )

    def detail(self, facts: CollaborationProjectionFacts) -> CollaborationDetailProjection:
        summary = self.root_summary(facts)
        return CollaborationDetailProjection(
            enabled=summary.enabled,
            status=summary.status,
            strategy_label=summary.strategy_label,
            run_id=summary.run_id,
            plan_generation=summary.plan_generation,
            released_count=summary.released_count,
            running_count=summary.running_count,
            waiting_count=summary.waiting_count,
            completed_count=summary.completed_count,
            blocked_subtask_count=summary.blocked_subtask_count,
            blocked_subtasks=summary.blocked_subtasks,
            latest_decision_summary=summary.latest_decision_summary,
            latest_decision_at=summary.latest_decision_at,
            attention_required=summary.attention_required,
            detail_href=summary.detail_href,
            provider=facts.run.provider,
            interface_version=facts.run.interface_version,
            config_version=facts.run.config_version,
            config_summary=f"配置版本 {facts.run.config_version}",
        )

    def decision_history(
        self,
        facts: CollaborationProjectionFacts,
        *,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 20,
    ) -> tuple[CollaborationDecisionProjection, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        decisions = sorted(
            facts.decisions,
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )
        if after is not None:
            after_at, after_id = after
            decisions = [
                item
                for item in decisions
                if (item.created_at, item.id) < (after_at, after_id)
            ]
        return tuple(
            self._decision_projection(facts, decision)
            for decision in decisions[:limit]
        )

    def decision_history_page(
        self,
        facts: CollaborationProjectionFacts,
        *,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 20,
    ) -> CollaborationDecisionPageProjection:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        decisions = sorted(
            facts.decisions,
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )
        if after is not None:
            after_at, after_id = after
            decisions = [
                item
                for item in decisions
                if (item.created_at, item.id) < (after_at, after_id)
            ]
        page = decisions[:limit]
        return CollaborationDecisionPageProjection(
            items=tuple(self._decision_projection(facts, item) for item in page),
            next_after=(
                (page[-1].created_at, page[-1].id)
                if len(decisions) > limit and page
                else None
            ),
        )

    @staticmethod
    def _decision_projection(
        facts: CollaborationProjectionFacts,
        decision: CollaborationDecisionProjectionFact,
    ) -> CollaborationDecisionProjection:
        intents = sorted(
            (
                intent
                for intent in facts.intents
                if intent.decision_id == decision.id
            ),
            key=lambda item: (item.ordinal, str(item.id)),
        )
        requests = {request.id: request for request in facts.requests}
        links = tuple(
            CollaborationProjectionService._subtask_link(facts.plan, request.subtask_key)
            for intent in intents
            if (request := requests.get(intent.target_request_id)) is not None
        )
        applications = {
            item.intent_id: item for item in facts.applications
        }
        return CollaborationDecisionProjection(
            decision_id=decision.id,
            sequence=decision.sequence,
            outcome=decision.outcome,
            reason_code=decision.reason_code,
            summary=decision.summary,
            created_at=decision.created_at,
            projection_id=decision.projection_id,
            projection_hash=decision.projection_hash,
            target_paths=_unique_links(links),
            applications=tuple(
                CollaborationDecisionApplicationProjection(
                    intent_id=intent.id,
                    status=application.status,
                    result_code=application.result_code,
                    result_summary=application.result_summary,
                    release_id=application.release_id,
                    error_code=application.error_code,
                )
                for intent in intents
                if (application := applications.get(intent.id)) is not None
            ),
        )

    def _blocked_subtasks(
        self,
        facts: CollaborationProjectionFacts,
        *,
        states: Mapping[UUID, str],
        executions: Mapping[UUID, CollaborationExecutionProjectionFact],
    ) -> tuple[BlockedSubtaskProjection, ...]:
        grouped: dict[str, list[CollaborationRequestProjectionFact]] = {}
        for request in facts.requests:
            if states[request.id].startswith("waiting_"):
                grouped.setdefault(request.subtask_key, []).append(request)
        result: list[BlockedSubtaskProjection] = []
        for subtask in facts.plan.subtasks:
            items = grouped.get(subtask.key)
            if not items:
                continue
            relevant_executions = [
                executions[item.id]
                for item in items
                if item.id in executions
                and executions[item.id].failure_code is not None
            ]
            failure_times: list[datetime] = []
            for execution in relevant_executions:
                failure_time = execution.finished_at or execution.updated_at
                if failure_time is not None:
                    failure_times.append(failure_time)
            latest_failure = max(
                failure_times,
                default=None,
            )
            reason = self._group_reason(items, states)
            result.append(
                BlockedSubtaskProjection(
                    subtask_key=subtask.key,
                    title=subtask.title,
                    reason=reason,
                    last_failure_at=latest_failure,
                    href=self._subtask_link(facts.plan, subtask.key).href,
                )
            )
        return tuple(result)

    @staticmethod
    def _group_reason(
        requests: Iterable[CollaborationRequestProjectionFact],
        states: Mapping[UUID, str],
    ) -> str:
        ordered = sorted(
            requests,
            key=lambda item: (
                _state_priority(states[item.id]),
                item.updated_at or _MIN_DATETIME,
                str(item.id),
            ),
            reverse=True,
        )
        request = ordered[0]
        return _state_summary(states[request.id], request.gate_reason) or _human_reason(
            request.gate_reason
        )

    @staticmethod
    def _request_map(
        requests: Iterable[CollaborationRequestProjectionFact],
    ) -> dict[UUID, CollaborationRequestProjectionFact]:
        return {request.id: request for request in requests}

    @staticmethod
    def _latest_executions(
        executions: Iterable[CollaborationExecutionProjectionFact],
    ) -> dict[UUID, CollaborationExecutionProjectionFact]:
        result: dict[UUID, CollaborationExecutionProjectionFact] = {}
        for execution in executions:
            current = result.get(execution.request_id)
            if current is None or (
                execution.attempt,
                execution.updated_at or _MIN_DATETIME,
                str(execution.execution_id),
            ) > (
                current.attempt,
                current.updated_at or _MIN_DATETIME,
                str(current.execution_id),
            ):
                result[execution.request_id] = execution
        return result

    def _request_state(
        self,
        facts: CollaborationProjectionFacts,
        request: CollaborationRequestProjectionFact,
        *,
        executions: Mapping[UUID, CollaborationExecutionProjectionFact],
        released: set[UUID],
    ) -> str:
        if request.collaboration_gate_mode != "release_required":
            return "unmanaged"
        execution = executions.get(request.id)
        if execution is not None and _is_completed(execution):
            return "materialized"
        if request.id in released:
            return "released" if execution is None else "materialized"
        dependency = self._request_dependency_state(
            facts,
            request.id,
            executions=executions,
        )
        if dependency.missing_artifact_refs:
            return "waiting_artifact"
        if dependency.missing_evidence_refs:
            return "waiting_evidence"
        if dependency.missing_upstream:
            return "waiting_dependency"
        if request.gate_reason in {"dependencies_blocked", "waiting_dependency"}:
            return "waiting_dependency"
        if request.gate_reason in {"waiting_artifact", "missing_artifact"}:
            return "waiting_artifact"
        if request.gate_reason in {"waiting_evidence", "missing_evidence"}:
            return "waiting_evidence"
        return "waiting_release"

    def _request_dependency_state(
        self,
        facts: CollaborationProjectionFacts,
        request_id: UUID,
        *,
        executions: Mapping[UUID, CollaborationExecutionProjectionFact],
    ) -> _DependencyState:
        state = _DependencyState()
        for dependency in facts.dependencies:
            if (
                dependency.downstream_request_id != request_id
                or dependency.enforcement != "hard"
            ):
                continue
            decision = self._dependency_decision(
                dependency,
                executions=executions,
                facts=facts,
            )
            state.merge(decision)
        return state

    @staticmethod
    def _dependency_decision(
        dependency: CollaborationRequestDependencyProjectionFact,
        *,
        executions: Mapping[UUID, CollaborationExecutionProjectionFact],
        facts: CollaborationProjectionFacts,
    ) -> _DependencyState:
        state = _DependencyState()
        upstream = executions.get(dependency.upstream_request_id)
        if upstream is None or not _is_completed(upstream):
            state.missing_upstream = True
            state.missing_artifact_refs = list(dependency.required_artifact_refs)
            state.missing_evidence_refs = list(dependency.required_evidence_refs)
            return state
        state.missing_artifact_refs = [
            ref
            for ref in dependency.required_artifact_refs
            if ref not in upstream.artifact_refs
        ]
        state.missing_evidence_refs = [
            ref
            for ref in dependency.required_evidence_refs
            if ref not in upstream.evidence_refs
        ]
        return state

    @staticmethod
    def _strategy_for(run: CollaborationRunProjectionFact, subtask_key: str) -> str:
        return (run.strategy_by_subtask or {}).get(subtask_key, run.strategy_label)

    @staticmethod
    def _subtask_link(
        plan: CanonicalTaskPlan, subtask_key: str
    ) -> CollaborationLinkProjection:
        title = next(
            (item.title for item in plan.subtasks if item.key == subtask_key),
            subtask_key,
        )
        return CollaborationLinkProjection(
            subtask_key=subtask_key,
            title=title,
            href=f"/workspaces/{plan.workspace_id}/tasks/{plan.root_task_id}/subtasks/{subtask_key}",
        )


@dataclass(frozen=True, slots=True)
class CollaborationDetailProjection:
    enabled: bool
    status: str
    strategy_label: str
    run_id: UUID
    plan_generation: int
    released_count: int
    running_count: int
    waiting_count: int
    completed_count: int
    blocked_subtask_count: int
    blocked_subtasks: tuple[BlockedSubtaskProjection, ...]
    latest_decision_summary: str | None
    latest_decision_at: datetime | None
    attention_required: bool
    detail_href: str
    provider: str
    interface_version: str
    config_version: int
    config_summary: str


@dataclass(slots=True)
class _DependencyState:
    missing_upstream: bool = False
    missing_artifact_refs: list[str] = field(default_factory=list)
    missing_evidence_refs: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not (
            self.missing_upstream
            or self.missing_artifact_refs
            or self.missing_evidence_refs
        )

    def merge(self, other: _DependencyState) -> None:
        self.missing_upstream = self.missing_upstream or other.missing_upstream
        if self.missing_artifact_refs is None:
            self.missing_artifact_refs = []
        if self.missing_evidence_refs is None:
            self.missing_evidence_refs = []
        _append_unique(self.missing_artifact_refs, other.missing_artifact_refs)
        _append_unique(self.missing_evidence_refs, other.missing_evidence_refs)


def _is_running(execution: CollaborationExecutionProjectionFact | None) -> bool:
    return execution is not None and execution.status in {
        "queued",
        "dispatching",
        "running",
        "leased",
    }


def _is_completed(execution: CollaborationExecutionProjectionFact | None) -> bool:
    return execution is not None and execution.status == "succeeded"


def _is_attention_reason(reason: str) -> bool:
    return reason.startswith("runtime_") or reason in {
        "collaboration_run_blocked",
        "configuration_mismatch",
        "projection_invalid",
    }


def _state_priority(state: str) -> int:
    return {
        "waiting_evidence": 5,
        "waiting_artifact": 4,
        "waiting_dependency": 3,
        "waiting_release": 2,
        "released": 1,
    }.get(state, 0)


def _state_summary(state: str, reason: str) -> str | None:
    if state == "waiting_dependency":
        return "等待上游结果"
    if state == "waiting_artifact":
        return "等待必要产物"
    if state == "waiting_evidence":
        return "等待必要证据"
    if state == "waiting_release":
        return "等待协同放行"
    if state == "released":
        return "协同已放行，等待调度"
    if state == "materialized":
        return "已进入执行"
    if state == "unmanaged":
        return "按现有流程调度"
    return _human_reason(reason)


def _human_reason(reason: str) -> str:
    return {
        "collaboration_run_pending": "等待协同运行",
        "collaboration_run_blocked": "协同运行受阻",
        "runtime_output_invalid": "协同运行输出无效",
        "runtime_unavailable": "协同运行暂不可用",
        "runtime_timeout": "协同运行超时",
        "configuration_mismatch": "协同配置不匹配",
    }.get(reason, reason.replace("_", " "))


def _detail_href(workspace_id: UUID, root_task_id: UUID) -> str:
    return f"/workspaces/{workspace_id}/tasks/{root_task_id}/collaboration"


def _unique_links(
    links: Iterable[CollaborationLinkProjection],
) -> tuple[CollaborationLinkProjection, ...]:
    result: list[CollaborationLinkProjection] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        key = (link.subtask_key, link.href)
        if key not in seen:
            seen.add(key)
            result.append(link)
    return tuple(result)


def _append_unique(target: list[str], values: Iterable[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)
