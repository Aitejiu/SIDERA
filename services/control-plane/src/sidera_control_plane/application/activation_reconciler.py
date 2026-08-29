from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.context_requests import (
    build_execution_context_assembly_request,
)
from sidera_control_plane.application.execution_commands import (
    build_continuation_execution,
)
from sidera_control_plane.application.plan_transition_gate import (
    PlanTransitionGate,
    PlanTransitionGateOperation,
    PlanTransitionGateResult,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import InvalidTransition
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionStatus,
)
from sidera_control_plane.domain.execution_requests import (
    ActivationDecision,
    ActivationGate,
    DependencyEnforcement,
    ExecutionRequest,
    ExecutionRequestDependency,
    ExecutionRequestStatus,
    UpstreamActivationFact,
)
from sidera_control_plane.domain.plan_revisions import WorkerTransitionAction
from sidera_control_plane.domain.plan_transitions import WorkerTransitionStatus
from sidera_control_plane.domain.planning import (
    PlanningRequest,
    PlanningRequestStatus,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.task import TERMINAL_TASK_STATUSES, Task, TaskStatus
from sidera_control_plane.domain.work_context import WorkContextRevision
from sidera_control_plane.domain.worker_item import WorkerItem

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ActivationReconcileResult:
    examined: int
    readied: int
    materialized: int
    deferred: int
    failed: int
    execution_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class _ActivationContext:
    planning_request: PlanningRequest
    snapshot: TaskPlanSnapshot
    task: Task
    work_context_revision: WorkContextRevision
    worker_item: WorkerItem


class ActivationReconciler:
    """Bridge materialized ExecutionRequests to the existing Execution scheduler."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        owner_id: str = "activation-reconciler",
        clock: Callable[[], datetime] | None = None,
        execution_context_ttl_seconds: int = 7_200,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        if execution_context_ttl_seconds < 1:
            raise ValueError("execution_context_ttl_seconds must be positive")
        self._uow_factory = uow_factory
        self._owner_id = owner_id.strip()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._execution_context_ttl_seconds = execution_context_ttl_seconds

    def run_once(self, limit: int) -> ActivationReconcileResult:
        if limit < 1:
            raise ValueError("limit must be positive")
        examined = 0
        readied = 0
        materialized = 0
        deferred = 0
        failed = 0
        execution_ids: list[UUID] = []
        seen_ids: set[UUID] = set()
        now = self._clock()

        with self._uow_factory() as uow:
            blocked = uow.execution_requests.claim_blocked(now=now, limit=limit)
            for request in blocked:
                seen_ids.add(request.id)
                examined += 1
                try:
                    with uow.savepoint():
                        became_ready, was_deferred = self._reconcile_blocked(
                            uow, request, now=now
                        )
                    readied += int(became_ready)
                    deferred += int(was_deferred)
                except Exception:
                    failed += 1
                    logger.exception(
                        "activation_candidate_failed",
                        extra={
                            "workspace_id": str(request.workspace_id),
                            "execution_request_id": str(request.id),
                            "planning_request_id": str(request.planning_request_id),
                        },
                    )

            ready = uow.execution_requests.claim_ready(now=now, limit=limit)
            for request in ready:
                if request.id not in seen_ids:
                    examined += 1
                    seen_ids.add(request.id)
                try:
                    with uow.savepoint():
                        execution_id, was_deferred = self._materialize_ready(
                            uow, request, now=now
                        )
                    if execution_id is not None:
                        materialized += 1
                        execution_ids.append(execution_id)
                    deferred += int(was_deferred)
                except Exception:
                    failed += 1
                    logger.exception(
                        "activation_candidate_failed",
                        extra={
                            "workspace_id": str(request.workspace_id),
                            "execution_request_id": str(request.id),
                            "planning_request_id": str(request.planning_request_id),
                        },
                    )

        return ActivationReconcileResult(
            examined=examined,
            readied=readied,
            materialized=materialized,
            deferred=deferred,
            failed=failed,
            execution_ids=tuple(execution_ids),
        )

    def _reconcile_blocked(
        self,
        uow: ControlPlaneUnitOfWork,
        request: ExecutionRequest,
        *,
        now: datetime,
    ) -> tuple[bool, bool]:
        context, stale_reason = self._current_context(uow, request)
        if context is None:
            self._obsolete(uow, request, reason=stale_reason or "stale_context", now=now)
            return False, True

        transition_gate = self._transition_gate(uow, request)
        if transition_gate.obsolete:
            self._obsolete(uow, request, reason=transition_gate.reason, now=now)
            return False, True
        if transition_gate.blocked:
            transition_decision = ActivationDecision(
                ready=False,
                reason=transition_gate.reason,
                details=transition_gate.details,
            )
            self._log_decision(request, transition_decision, now=now)
            self._apply_gate(
                uow,
                request,
                transition_decision,
                now=now,
                envelope_action="gate-transition-blocked",
            )
            return False, True

        decision = self._decision(uow, request, context, now=now)
        self._log_decision(request, decision, now=now)
        if decision.ready:
            became_ready = self._apply_gate(
                uow, request, decision, now=now, envelope_action="gate-ready"
            )
            return became_ready, False
        self._apply_gate(
            uow, request, decision, now=now, envelope_action="gate-blocked"
        )
        return False, True

    def _materialize_ready(
        self,
        uow: ControlPlaneUnitOfWork,
        request: ExecutionRequest,
        *,
        now: datetime,
    ) -> tuple[UUID | None, bool]:
        context, stale_reason = self._current_context(uow, request)
        if context is None:
            self._obsolete(uow, request, reason=stale_reason or "stale_context", now=now)
            return None, True

        transition_gate = self._transition_gate(uow, request)
        if transition_gate.obsolete:
            self._obsolete(uow, request, reason=transition_gate.reason, now=now)
            return None, True
        if transition_gate.blocked:
            transition_decision = ActivationDecision(
                ready=False,
                reason=transition_gate.reason,
                details=transition_gate.details,
            )
            self._log_decision(request, transition_decision, now=now)
            self._apply_gate(
                uow,
                request,
                transition_decision,
                now=now,
                envelope_action="gate-transition-blocked",
            )
            return None, True

        decision = self._decision(uow, request, context, now=now)
        self._log_decision(request, decision, now=now)
        if not decision.ready:
            self._apply_gate(
                uow, request, decision, now=now, envelope_action="gate-blocked"
            )
            return None, True

        if context.task.status in {TaskStatus.BLOCKED, TaskStatus.PAUSED}:
            task_decision = ActivationDecision(
                ready=False,
                reason="task_not_ready",
                details={
                    "task_id": str(context.task.id),
                    "task_status": context.task.status.value,
                },
            )
            self._log_decision(request, task_decision, now=now)
            self._apply_gate(
                uow,
                request,
                task_decision,
                now=now,
                envelope_action="gate-blocked",
            )
            return None, True
        if context.task.status in TERMINAL_TASK_STATUSES:
            self._obsolete(uow, request, reason="task_terminal", now=now)
            return None, True

        execution_id = uuid4()
        if request.source_execution_id is not None:
            source = uow.executions.get(
                request.source_execution_id,
                request.workspace_id,
            )
            source_session = (
                uow.sessions.get_any(
                    request.source_session_id,
                    request.workspace_id,
                )
                if request.source_session_id is not None
                else None
            )
            if source is None or source_session is None:
                raise InvalidTransition(
                    "continuation request source lineage is unavailable"
                )
            execution = build_continuation_execution(
                source,
                source_session,
                execution_id=execution_id,
                task_id=request.subtask_id,
                max_attempts=request.max_attempts,
                purpose=request.purpose,
                capability_requirements=request.capability_requirements,
                adapter_preferences=request.adapter_preferences,
                adapter_forbidden=request.adapter_forbidden,
                not_before=request.not_before,
            )
        else:
            execution = Execution.create(
                execution_id=execution_id,
                workspace_id=request.workspace_id,
                task_id=request.subtask_id,
                worker_item_id=request.worker_item_id,
                lineage_id=execution_id,
                attempt=1,
                max_attempts=request.max_attempts,
                purpose=request.purpose,
                capability_requirements=request.capability_requirements,
                pinned_worker_id=None,
                not_before=request.not_before,
                adapter_preferences=request.adapter_preferences,
                adapter_forbidden=request.adapter_forbidden,
            )
        task_event: EventData | None = None
        previous_request_version = request.version
        envelope = self._envelope(
            request, action="materialize", version=previous_request_version
        )
        if context.task.status in {TaskStatus.PENDING, TaskStatus.WAITING}:
            previous_task_version = context.task.version
            previous_status = context.task.status.value
            context.task.mark_ready()
            uow.tasks.save(context.task, previous_task_version)
            task_event = _event(
                envelope,
                aggregate_type="task",
                aggregate_id=context.task.id,
                aggregate_version=context.task.version,
                event_type="TaskReady",
                payload={
                    "status": context.task.status.value,
                    "previous_status": previous_status,
                    "root_task_id": str(request.root_task_id),
                    "worker_item_id": str(request.worker_item_id),
                },
            )

        uow.executions.add(execution)
        uow.context_assemblies.add(
            build_execution_context_assembly_request(
                execution=execution,
                execution_request=request,
                worker_item=context.worker_item,
                work_context_revision=context.work_context_revision,
                ttl_seconds=self._execution_context_ttl_seconds,
                created_at=now,
            )
        )
        uow.flush()
        request.materialize(
            execution_lineage_id=execution.lineage_id,
            first_execution_id=execution.id,
            now=now,
        )
        uow.execution_requests.save(request, previous_request_version)
        transition_event = self._bind_worker_transition(
            uow, request, execution, now=now, envelope=envelope
        )

        events = [
            _event(
                envelope,
                aggregate_type="execution",
                aggregate_id=execution.id,
                aggregate_version=execution.version,
                event_type="ExecutionCreated",
                payload={
                    **_execution_payload(execution),
                    "root_task_id": str(request.root_task_id),
                },
            ),
            _event(
                envelope,
                aggregate_type="execution_request",
                aggregate_id=request.id,
                aggregate_version=request.version,
                event_type="ExecutionRequestMaterialized",
                payload={
                    "execution_request_id": str(request.id),
                    "execution_id": str(execution.id),
                    "execution_lineage_id": str(execution.lineage_id),
                    "planning_request_id": str(request.planning_request_id),
                    "proposal_revision_id": str(request.proposal_revision_id),
                    "plan_snapshot_id": str(request.plan_snapshot_id),
                    "plan_generation": request.plan_generation,
                    "root_task_id": str(request.root_task_id),
                    "subtask_task_id": str(request.subtask_id),
                    "worker_item_id": str(request.worker_item_id),
                    "status": request.status.value,
                },
            ),
        ]
        if task_event is not None:
            events.insert(0, task_event)
        if transition_event is not None:
            events.append(transition_event)
        self._record_events(uow, envelope, request, tuple(events))
        return execution.id, False

    def _bind_worker_transition(
        self,
        uow: ControlPlaneUnitOfWork,
        request: ExecutionRequest,
        execution: Execution,
        *,
        now: datetime,
        envelope: CommandEnvelope,
    ) -> EventData | None:
        transitions = getattr(uow, "worker_transitions", None)
        if transitions is None:
            return None
        transition = transitions.get_active_for_worker_item(
            request.worker_item_id,
            request.workspace_id,
            for_update=True,
        )
        if transition is None or transition.status is not WorkerTransitionStatus.WAITING_ACTIVATION:
            return None
        if transition.action not in {
            WorkerTransitionAction.CONTINUE,
            WorkerTransitionAction.CREATE,
        }:
            return None
        plan = uow.plan_transitions.get(
            transition.plan_transition_id, request.workspace_id
        )
        if (
            plan is None
            or plan.to_snapshot_id != request.plan_snapshot_id
            or plan.to_generation != request.plan_generation
        ):
            return None
        previous_version = transition.version
        transition.bind_target(execution_id=execution.id, session_id=None, now=now)
        transitions.save(transition, previous_version)
        return _event(
            envelope,
            aggregate_type="worker_transition",
            aggregate_id=transition.id,
            aggregate_version=transition.version,
            event_type="WorkerTransitionWaitingForActivation",
            payload={
                "worker_transition_id": str(transition.id),
                "worker_item_id": str(transition.worker_item_id),
                "execution_request_id": str(request.id),
                "target_execution_id": str(execution.id),
                "status": transition.status.value,
            },
        )

    def _current_context(
        self, uow: ControlPlaneUnitOfWork, request: ExecutionRequest
    ) -> tuple[_ActivationContext | None, str | None]:
        planning_request = uow.planning_requests.get(
            request.planning_request_id, request.workspace_id
        )
        if planning_request is None:
            return None, "planning_request_missing"
        if planning_request.status is not PlanningRequestStatus.MATERIALIZED:
            return None, "planning_request_not_materialized"
        if not self._request_matches_current_plan(uow, request, planning_request):
            return None, "stale_plan"
        snapshot = uow.plans.get_current_for_root(
            request.root_task_id, request.workspace_id
        )
        if snapshot is None:
            return None, "plan_missing"
        root_task = uow.tasks.get(request.root_task_id, request.workspace_id)
        if root_task is None:
            return None, "root_task_missing"
        if root_task.status in TERMINAL_TASK_STATUSES:
            return None, "root_task_terminal"
        task = uow.tasks.get(request.subtask_id, request.workspace_id, for_update=True)
        if task is None:
            return None, "subtask_missing"
        if task.status in TERMINAL_TASK_STATUSES:
            return None, "task_terminal"
        worker_item = uow.worker_items.get(
            request.worker_item_id, request.workspace_id, for_update=True
        )
        if worker_item is None:
            return None, "worker_item_missing"
        work_context = uow.work_contexts.get_by_planning_request(
            request.planning_request_id, request.workspace_id
        )
        if work_context is None:
            # Revision requests share the root task's WorkContext aggregate.
            work_context = uow.work_contexts.get_by_root_task(
                request.root_task_id, request.workspace_id
            )
        if work_context is None:
            return None, "work_context_missing"
        work_context_revision = uow.work_contexts.get_revision(
            work_context.current_revision_id, request.workspace_id
        )
        if work_context_revision is None:
            return None, "work_context_revision_missing"
        return _ActivationContext(
            planning_request,
            snapshot,
            task,
            work_context_revision,
            worker_item,
        ), None

    def _transition_gate(
        self, uow: ControlPlaneUnitOfWork, request: ExecutionRequest
    ) -> PlanTransitionGateResult:
        allow_transition_id = self._activation_transition_id(uow, request)
        return PlanTransitionGate(uow).evaluate(
            worker_item_id=request.worker_item_id,
            workspace_id=request.workspace_id,
            plan_generation=request.plan_generation,
            operation=PlanTransitionGateOperation.ACTIVATE,
            allow_transition_id=allow_transition_id,
        )

    @staticmethod
    def _activation_transition_id(
        uow: ControlPlaneUnitOfWork, request: ExecutionRequest
    ) -> UUID | None:
        transitions = getattr(uow, "worker_transitions", None)
        plan_transitions = getattr(uow, "plan_transitions", None)
        if transitions is None or plan_transitions is None:
            return None
        transition = transitions.get_active_for_worker_item(
            request.worker_item_id,
            request.workspace_id,
            for_update=True,
        )
        if transition is None or transition.status is not WorkerTransitionStatus.WAITING_ACTIVATION:
            return None
        if transition.action not in {
            WorkerTransitionAction.CONTINUE,
            WorkerTransitionAction.CREATE,
        }:
            return None
        plan = plan_transitions.get(
            transition.plan_transition_id,
            request.workspace_id,
        )
        if (
            plan is None
            or plan.to_snapshot_id != request.plan_snapshot_id
            or plan.to_generation != request.plan_generation
        ):
            return None
        if transition.action is WorkerTransitionAction.CONTINUE and (
            request.source_execution_id != transition.source_execution_id
            or request.source_session_id != transition.source_session_id
        ):
            return None
        if transition.action is WorkerTransitionAction.CREATE and (
            request.source_execution_id is not None
            or request.source_session_id is not None
        ):
            return None
        expected_request_id = uuid5(
            NAMESPACE_URL,
            f"sidera.plan-transition.execution-request:{transition.id}:{request.worker_item_key}",
        )
        return transition.id if request.id == expected_request_id else None

    @staticmethod
    def _request_matches_current_plan(
        uow: ControlPlaneUnitOfWork,
        request: ExecutionRequest,
        planning_request: PlanningRequest,
    ) -> bool:
        if planning_request.current_proposal_revision is None:
            return False
        proposal = uow.planning_records.get_current_proposal(
            planning_request.id, planning_request.workspace_id
        )
        if proposal is None or proposal.id != request.proposal_revision_id:
            return False
        snapshot = uow.plans.get_current_for_root(
            request.root_task_id, request.workspace_id
        )
        return snapshot is not None and (
            snapshot.id == request.plan_snapshot_id
            and snapshot.generation == request.plan_generation
        )

    def _decision(
        self,
        uow: ControlPlaneUnitOfWork,
        request: ExecutionRequest,
        context: _ActivationContext,
        *,
        now: datetime,
    ) -> ActivationDecision:
        del now
        if context.task.status in {TaskStatus.BLOCKED, TaskStatus.PAUSED}:
            return ActivationDecision(
                ready=False,
                reason="task_not_ready",
                details={
                    "task_id": str(context.task.id),
                    "task_status": context.task.status.value,
                },
            )
        dependencies = uow.execution_requests.list_dependencies(
            request.id, request.workspace_id
        )
        facts = self._upstream_facts(uow, request, dependencies)
        return ActivationGate.evaluate(
            dependencies=dependencies,
            upstream_facts=facts,
        )

    def _upstream_facts(
        self,
        uow: ControlPlaneUnitOfWork,
        request: ExecutionRequest,
        dependencies: Iterable[ExecutionRequestDependency],
    ) -> Mapping[UUID, UpstreamActivationFact]:
        facts: dict[UUID, UpstreamActivationFact] = {}
        upstream_ids = tuple(
            dict.fromkeys(
                dependency.upstream_request_id
                for dependency in dependencies
                if dependency.enforcement is DependencyEnforcement.HARD
            )
        )
        for upstream_id in upstream_ids:
            upstream = uow.execution_requests.get(upstream_id, request.workspace_id)
            if upstream is None:
                continue
            facts[upstream_id] = self._fact_for_upstream(uow, upstream)
        return facts

    def _fact_for_upstream(
        self, uow: ControlPlaneUnitOfWork, request: ExecutionRequest
    ) -> UpstreamActivationFact:
        if request.status is not ExecutionRequestStatus.MATERIALIZED:
            return UpstreamActivationFact(request_id=request.id)
        planning_request = uow.planning_requests.get(
            request.planning_request_id, request.workspace_id
        )
        if (
            planning_request is None
            or planning_request.status is not PlanningRequestStatus.MATERIALIZED
            or not self._request_matches_current_plan(uow, request, planning_request)
            or request.execution_lineage_id is None
        ):
            return UpstreamActivationFact(
                request_id=request.id,
                execution_status=ExecutionStatus.CANCELED,
            )

        attempts = uow.executions.list_for_lineage(
            request.execution_lineage_id, request.workspace_id
        )
        successful = tuple(
            execution
            for execution in attempts
            if execution.status is ExecutionStatus.SUCCEEDED
        )
        if not successful:
            latest = attempts[-1] if attempts else None
            return UpstreamActivationFact(
                request_id=request.id,
                execution_status=latest.status if latest is not None else None,
                execution_lineage_id=request.execution_lineage_id,
            )

        successful_ids = tuple(execution.id for execution in successful)
        artifacts = uow.artifacts.list_for_executions(
            successful_ids, request.workspace_id
        )
        artifact_ids = {artifact.id for artifact in artifacts}
        artifact_refs = tuple(
            dict.fromkeys(f"artifact_type:{artifact.artifact_type.value}" for artifact in artifacts)
        )
        evidence_refs: list[str] = []
        claims = uow.evidence_claims.list_for_executions(
            successful_ids, request.workspace_id
        )
        for claim in claims:
            if not claim.artifact_ids or not set(claim.artifact_ids).issubset(artifact_ids):
                continue
            ref = f"acceptance:{claim.acceptance_criterion_ref}"
            if ref not in evidence_refs:
                evidence_refs.append(ref)
        return UpstreamActivationFact(
            request_id=request.id,
            execution_status=ExecutionStatus.SUCCEEDED,
            execution_lineage_id=request.execution_lineage_id,
            artifact_refs=artifact_refs,
            evidence_refs=tuple(evidence_refs),
        )

    def _apply_gate(
        self,
        uow: ControlPlaneUnitOfWork,
        request: ExecutionRequest,
        decision: ActivationDecision,
        *,
        now: datetime,
        envelope_action: str,
    ) -> bool:
        if decision.ready:
            if request.status is not ExecutionRequestStatus.BLOCKED:
                return False
            previous_version = request.version
            request.mark_ready(now=now, decision=decision)
            uow.execution_requests.save(request, previous_version)
            envelope = self._envelope(
                request, action=envelope_action, version=previous_version
            )
            event = _event(
                envelope,
                aggregate_type="execution_request",
                aggregate_id=request.id,
                aggregate_version=request.version,
                event_type="ExecutionRequestGateReady",
                payload=self._gate_payload(request, decision),
            )
            self._record_events(uow, envelope, request, (event,))
            return True

        if request.status not in {
            ExecutionRequestStatus.BLOCKED,
            ExecutionRequestStatus.READY,
        }:
            raise InvalidTransition("activation gate is closed")
        if _gate_fingerprint(request) == _decision_fingerprint(decision):
            return False
        previous_version = request.version
        request.refresh_gate(decision=decision, now=now)
        uow.execution_requests.save(request, previous_version)
        envelope = self._envelope(
            request, action=envelope_action, version=previous_version
        )
        event = _event(
            envelope,
            aggregate_type="execution_request",
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_type="ExecutionRequestGateBlocked",
            payload=self._gate_payload(request, decision),
        )
        self._record_events(uow, envelope, request, (event,))
        return False

    def _obsolete(
        self,
        uow: ControlPlaneUnitOfWork,
        request: ExecutionRequest,
        *,
        reason: str,
        now: datetime,
    ) -> None:
        if request.status not in {
            ExecutionRequestStatus.BLOCKED,
            ExecutionRequestStatus.READY,
        }:
            return
        previous_version = request.version
        request.obsolete(now=now, reason=reason)
        uow.execution_requests.save(request, previous_version)
        envelope = self._envelope(request, action="obsolete", version=previous_version)
        event = _event(
            envelope,
            aggregate_type="execution_request",
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_type="ExecutionRequestObsoleted",
            payload={
                "execution_request_id": str(request.id),
                "planning_request_id": str(request.planning_request_id),
                "proposal_revision_id": str(request.proposal_revision_id),
                "plan_snapshot_id": str(request.plan_snapshot_id),
                "plan_generation": request.plan_generation,
                "root_task_id": str(request.root_task_id),
                "subtask_task_id": str(request.subtask_id),
                "worker_item_id": str(request.worker_item_id),
                "reason": reason,
                "status": request.status.value,
            },
        )
        self._record_events(uow, envelope, request, (event,))

    def _record_events(
        self,
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
        request: ExecutionRequest,
        events: tuple[EventData, ...],
    ) -> None:
        uow.commands.record_applied(
            envelope,
            hash_request(envelope),
            CommandResult(
                command_id=envelope.id,
                aggregate_id=request.id,
                aggregate_version=request.version,
                event_ids=tuple(event.id for event in events),
            ),
        )
        for event in events:
            uow.events.append_with_outbox(event)

    def _gate_payload(
        self, request: ExecutionRequest, decision: ActivationDecision
    ) -> dict[str, object]:
        return {
            "execution_request_id": str(request.id),
            "planning_request_id": str(request.planning_request_id),
            "proposal_revision_id": str(request.proposal_revision_id),
            "plan_snapshot_id": str(request.plan_snapshot_id),
            "plan_generation": request.plan_generation,
            "root_task_id": str(request.root_task_id),
            "subtask_task_id": str(request.subtask_id),
            "worker_item_id": str(request.worker_item_id),
            "gate_reason": decision.reason,
            "gate_details": _json_details(decision.details),
            "status": request.status.value,
        }

    def _envelope(
        self, request: ExecutionRequest, *, action: str, version: int
    ) -> CommandEnvelope:
        command_id = uuid5(
            NAMESPACE_URL,
            f"sidera.activation:{self._owner_id}:{action}:{request.id}:v{version}",
        )
        return CommandEnvelope(
            id=command_id,
            workspace_id=request.workspace_id,
            command_type="ActivationReconcile",
            target_type="execution_request",
            actor_type="system",
            actor_id=self._owner_id,
            target_id=request.id,
            payload={"execution_request_id": str(request.id), "action": action},
            expected_version=version,
            idempotency_key=f"activation:{request.id}:{action}:v{version}",
            correlation_id=request.planning_request_id,
        )

    def _log_decision(
        self,
        request: ExecutionRequest,
        decision: ActivationDecision,
        *,
        now: datetime,
    ) -> None:
        elapsed = max(0.0, (now - request.created_at).total_seconds())
        logger.info(
            "activation_gate_decision",
            extra={
                "workspace_id": str(request.workspace_id),
                "planning_request_id": str(request.planning_request_id),
                "proposal_revision_id": str(request.proposal_revision_id),
                "execution_request_id": str(request.id),
                "plan_generation": request.plan_generation,
                "decision_reason": decision.reason,
                "missing_upstream_request_ids": [
                    str(item) for item in decision.missing_upstream_request_ids
                ],
                "missing_artifact_refs": list(decision.missing_artifact_refs),
                "missing_evidence_refs": list(decision.missing_evidence_refs),
                "elapsed_seconds": elapsed,
            },
        )


def _execution_payload(execution: Execution) -> dict[str, object]:
    return {
        "task_id": str(execution.task_id),
        "worker_item_id": str(execution.worker_item_id)
        if execution.worker_item_id is not None
        else None,
        "lineage_id": str(execution.lineage_id),
        "attempt": execution.attempt,
        "max_attempts": execution.max_attempts,
        "purpose": execution.purpose,
        "status": execution.status.value,
        "stop_requested": execution.stop_requested,
        "capability_requirements": sorted(execution.capability_requirements),
        "pinned_worker_id": str(execution.pinned_worker_id)
        if execution.pinned_worker_id is not None
        else None,
        "not_before": execution.not_before.isoformat(),
        "source_execution_id": str(execution.source_execution_id)
        if execution.source_execution_id is not None
        else None,
        "source_session_id": str(execution.source_session_id)
        if execution.source_session_id is not None
        else None,
    }


def _event(
    envelope: CommandEnvelope,
    *,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    event_type: str,
    payload: dict[str, object],
) -> EventData:
    return EventData(
        id=uuid4(),
        workspace_id=envelope.workspace_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        event_type=event_type,
        payload=payload,
        actor_type=envelope.actor_type,
        actor_id=envelope.actor_id,
        causation_id=envelope.id,
        correlation_id=envelope.correlation_id,
    )


def _decision_fingerprint(decision: ActivationDecision) -> tuple[object, ...]:
    return (
        decision.reason,
        tuple(sorted(str(item) for item in decision.missing_upstream_request_ids)),
        tuple(sorted(str(item) for item in decision.blocked_upstream_request_ids)),
        tuple(sorted(decision.missing_artifact_refs)),
        tuple(sorted(decision.missing_evidence_refs)),
    )


def _gate_fingerprint(request: ExecutionRequest) -> tuple[object, ...]:
    details = request.gate_details
    return (
        request.gate_reason,
        _detail_tuple(details.get("missing_upstream_request_ids")),
        _detail_tuple(details.get("blocked_upstream_request_ids")),
        _detail_tuple(details.get("missing_artifact_refs")),
        _detail_tuple(details.get("missing_evidence_refs")),
    )


def _detail_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted(str(item) for item in value))


def _json_details(details: Mapping[str, object]) -> dict[str, object]:
    return {
        key: [str(item) for item in value]
        if isinstance(value, (list, tuple, set, frozenset))
        else value
        for key, value in details.items()
    }
