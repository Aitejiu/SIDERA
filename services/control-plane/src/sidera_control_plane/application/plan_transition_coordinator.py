from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.context_patch_delivery import (
    ContextPatchDeliveryService,
    ContextPatchGateway,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextPackageStatus,
    ContextPurpose,
)
from sidera_control_plane.domain.errors import InvalidTransition, StaleFencingToken
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import (
    TERMINAL_EXECUTION_STATUSES,
    Execution,
    ExecutionSession,
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.execution_requests import (
    DependencyEnforcement,
    ExecutionRequest,
    ExecutionRequestDependency,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.gateway import (
    GatewayOperationStatus,
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.domain.inputs import ExecutionInputStatus
from sidera_control_plane.domain.lease import LeaseStatus
from sidera_control_plane.domain.plan_revisions import WorkerTransitionAction
from sidera_control_plane.domain.plan_transitions import (
    WorkerTransition,
    WorkerTransitionStatus,
)
from sidera_control_plane.domain.planning import PlanningRequestStatus
from sidera_control_plane.domain.plans import TaskPlanSnapshot

logger = logging.getLogger(__name__)


class TransitionRuntimeGateway(ContextPatchGateway, Protocol):
    """Runtime boundary for patch/continuation actions."""


@dataclass(frozen=True, slots=True)
class PlanTransitionRunResult:
    claimed: int
    converged: int
    deferred: int
    failed: int
    reconciled: int


@dataclass(frozen=True, slots=True)
class _ClaimedWorkerTransition:
    id: UUID
    workspace_id: UUID
    plan_transition_id: UUID
    version: int
    lease_id: UUID | None
    fencing_token: int
    action: WorkerTransitionAction


class PlanTransitionCoordinator:
    """Advance safe parts of a confirmed plan transition.

    Preserve and move retain the existing Execution/Session and need no adapter
    call. Patch uses the separate ContextPatch delivery service. Continuation and
    create materialize a new ExecutionRequest; retire closes the WorkerItem only
    after the existing stop outbox path confirms the old Session is closed.
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        clock: Callable[[], datetime] | None = None,
        owner_id: str = "plan-transition-coordinator",
        lease_ttl_seconds: int = 60,
        backoff_max_seconds: int = 60,
        gateway: TransitionRuntimeGateway | None = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        if lease_ttl_seconds < 1 or backoff_max_seconds < 1:
            raise ValueError("transition coordinator limits must be positive")
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner_id = owner_id.strip()
        self._lease_ttl_seconds = lease_ttl_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._gateway = gateway
        self._patch_delivery = (
            ContextPatchDeliveryService(
                uow_factory=uow_factory,
                gateway=gateway,
                clock=self._clock,
                backoff_max_seconds=backoff_max_seconds,
            )
            if gateway is not None
            and callable(getattr(gateway, "apply_context_patch", None))
            else None
        )

    def run_once(self, limit: int = 20) -> PlanTransitionRunResult:
        if limit < 1:
            raise ValueError("transition coordinator limit must be positive")
        now = self._clock()
        claimed, deferred = self._claim_pending(now=now, limit=limit)
        converged = 0
        failed = 0
        touched_plan_ids: set[UUID] = set()
        for item in claimed:
            touched_plan_ids.add(item.plan_transition_id)
            try:
                if item.action is WorkerTransitionAction.PATCH:
                    outcome = self._converge_patch(item)
                    if outcome == "converged":
                        converged += 1
                    elif outcome in {"failed", "unknown"}:
                        failed += 1
                    else:
                        deferred += 1
                elif item.action in {
                    WorkerTransitionAction.CONTINUE,
                    WorkerTransitionAction.CREATE,
                    WorkerTransitionAction.RETIRE,
                }:
                    outcome = self._converge_lifecycle(item, now=self._clock())
                    if outcome == "converged":
                        converged += 1
                    elif outcome in {"failed", "unknown"}:
                        failed += 1
                    else:
                        deferred += 1
                elif self._converge_preserve_or_move(item, now=self._clock()):
                    converged += 1
                else:
                    deferred += 1
            except Exception:
                failed += 1
                logger.exception(
                    "plan_transition_worker_failed",
                    extra={
                        "worker_transition_id": str(item.id),
                        "plan_transition_id": str(item.plan_transition_id),
                        "workspace_id": str(item.workspace_id),
                        "action": item.action.value,
                    },
                )
        reconciled = self._reconcile_plans(
            now=self._clock(), limit=max(limit, len(touched_plan_ids))
        )
        return PlanTransitionRunResult(
            claimed=len(claimed),
            converged=converged,
            deferred=deferred,
            failed=failed,
            reconciled=reconciled,
        )

    def _claim_pending(
        self, *, now: datetime, limit: int
    ) -> tuple[tuple[_ClaimedWorkerTransition, ...], int]:
        claimed: list[_ClaimedWorkerTransition] = []
        deferred = 0
        with self._uow_factory() as uow:
            candidates = uow.worker_transitions.claim_pending(now=now, limit=limit)
            for transition in self._dependency_order(uow, candidates):
                if transition.action not in {
                    WorkerTransitionAction.PRESERVE,
                    WorkerTransitionAction.MOVE,
                    WorkerTransitionAction.PATCH,
                    WorkerTransitionAction.CONTINUE,
                    WorkerTransitionAction.CREATE,
                    WorkerTransitionAction.RETIRE,
                }:
                    continue
                if not self._head_matches(uow, transition):
                    deferred += 1
                    continue
                if not self._dependencies_ready(uow, transition):
                    deferred += 1
                    continue
                effective_action = transition.action
                previous_version = transition.version
                if transition.action is WorkerTransitionAction.PATCH:
                    if self._patch_delivery is not None and self._patch_ready(
                        uow, transition, now=now
                    ):
                        lease = self._resolve_patch_lease(uow, transition, now=now)
                    elif self._patch_fallback_available(uow, transition):
                        transition.use_fallback()
                        effective_action = transition.action
                        lease = self._resolve_lifecycle_lease(
                            uow, transition, now=now
                        )
                    else:
                        deferred += 1
                        continue
                elif transition.action in {
                    WorkerTransitionAction.CONTINUE,
                    WorkerTransitionAction.CREATE,
                    WorkerTransitionAction.RETIRE,
                }:
                    lease = self._resolve_lifecycle_lease(uow, transition, now=now)
                else:
                    lease = self._resolve_claim_lease(uow, transition, now=now)
                if lease is None:
                    if transition.version != previous_version:
                        uow.worker_transitions.save(transition, previous_version)
                    deferred += 1
                    continue
                try:
                    if transition.status is WorkerTransitionStatus.PENDING:
                        transition.claim(
                            lease_id=lease[0],
                            fencing_token=lease[1],
                            now=now,
                        )
                        transition.wait_for_safe_boundary(now=now)
                        uow.worker_transitions.save(transition, previous_version)
                    elif (
                        transition.lease_id != lease[0]
                        or transition.fencing_token != lease[1]
                    ):
                        raise StaleFencingToken(
                            "waiting worker transition is owned by another fence"
                        )
                    elif transition.version != previous_version:
                        uow.worker_transitions.save(transition, previous_version)
                except StaleFencingToken:
                    deferred += 1
                    continue
                claimed.append(
                    _ClaimedWorkerTransition(
                        id=transition.id,
                        workspace_id=transition.workspace_id,
                        plan_transition_id=transition.plan_transition_id,
                        version=transition.version,
                        lease_id=transition.lease_id,
                        fencing_token=transition.fencing_token,
                        action=effective_action,
                    )
                )
        return tuple(claimed), deferred

    def _converge_lifecycle(
        self, claimed: _ClaimedWorkerTransition, *, now: datetime
    ) -> str:
        with self._uow_factory() as uow:
            transition = uow.worker_transitions.get(
                claimed.id,
                claimed.workspace_id,
                for_update=True,
            )
            if transition is None:
                return "deferred"
            if (
                transition.version != claimed.version
                or transition.lease_id != claimed.lease_id
                or transition.fencing_token != claimed.fencing_token
            ):
                return "deferred"
            if not self._head_matches(uow, transition):
                return "deferred"

            if transition.status is WorkerTransitionStatus.WAITING_ACTIVATION:
                if transition.action in {
                    WorkerTransitionAction.CONTINUE,
                    WorkerTransitionAction.CREATE,
                }:
                    return self._converge_waiting_activation(
                        uow, transition, now=now
                    )
                return "deferred"
            if transition.status is not WorkerTransitionStatus.WAITING_SAFE_BOUNDARY:
                return "deferred"

            if transition.action in {
                WorkerTransitionAction.CONTINUE,
                WorkerTransitionAction.RETIRE,
            }:
                boundary = self._ensure_source_safe(
                    uow, transition, now=now
                )
                if boundary != "safe":
                    return boundary

            if transition.action in {
                WorkerTransitionAction.CONTINUE,
                WorkerTransitionAction.CREATE,
            }:
                previous_version = transition.version
                transition.begin_apply(now=now)
                request = self._ensure_execution_request(
                    uow, transition, now=now
                )
                if request.status is ExecutionRequestStatus.MATERIALIZED:
                    if request.first_execution_id is None:
                        raise InvalidTransition(
                            "materialized transition request has no execution"
                        )
                    transition.bind_target(
                        execution_id=request.first_execution_id,
                        session_id=None,
                        now=now,
                    )
                    transition.wait_for_activation(now=now)
                    uow.worker_transitions.save(transition, previous_version)
                    return "deferred"
                transition.wait_for_activation(now=now)
                uow.worker_transitions.save(transition, previous_version)
                return "deferred"

            if transition.action is WorkerTransitionAction.RETIRE:
                previous_version = transition.version
                transition.begin_apply(now=now)
                worker_item = uow.worker_items.get(
                    transition.worker_item_id,
                    transition.workspace_id,
                    for_update=True,
                )
                if worker_item is None:
                    transition.mark_unknown(
                        failure_code="worker_item_missing",
                        now=now,
                    )
                    uow.worker_transitions.save(transition, previous_version)
                    return "unknown"
                if worker_item.status.value != "retired":
                    worker_previous_version = worker_item.version
                    worker_item.retire(now=now)
                    uow.worker_items.save(worker_item, worker_previous_version)
                transition.converge(now=now)
                uow.worker_transitions.save(transition, previous_version)
                return "converged"
        return "deferred"

    def _converge_waiting_activation(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        now: datetime,
    ) -> str:
        """Close a lifecycle transition only after the target writer is observable."""
        target_execution_id = transition.target_execution_id
        if target_execution_id is None:
            return "deferred"
        execution = uow.executions.get(
            target_execution_id,
            transition.workspace_id,
            for_update=True,
        )
        if execution is None:
            return self._mark_unknown(
                uow,
                transition,
                failure_code="target_execution_missing",
                now=now,
            )
        if execution.worker_item_id != transition.worker_item_id:
            return self._mark_unknown(
                uow,
                transition,
                failure_code="target_worker_item_identity_mismatch",
                now=now,
            )

        worker_item = uow.worker_items.get(
            transition.worker_item_id,
            transition.workspace_id,
            for_update=True,
        )
        if worker_item is None:
            return self._mark_unknown(
                uow,
                transition,
                failure_code="target_worker_item_missing",
                now=now,
            )
        if worker_item.context_version != transition.target_context_version:
            return self._mark_unknown(
                uow,
                transition,
                failure_code="target_context_version_mismatch",
                now=now,
            )
        if not worker_item.context_is_synchronized:
            return "deferred"

        package = uow.context_packages.get_for_consumer(
            ContextPurpose.EXECUTION,
            execution.id,
            transition.workspace_id,
            for_update=True,
        )
        if package is None:
            if execution.status in TERMINAL_EXECUTION_STATUSES:
                return self._mark_failed(
                    uow,
                    transition,
                    failure_code="target_context_package_missing",
                    now=now,
                )
            return "deferred"
        if (
            package.consumer_id != execution.id
            or package.worker_item_id != worker_item.id
            or package.context_version != transition.target_context_version
        ):
            return self._mark_unknown(
                uow,
                transition,
                failure_code="target_context_package_mismatch",
                now=now,
            )

        binding = uow.bindings.get_for_execution(
            execution.id,
            transition.workspace_id,
            for_update=True,
        )
        active_session = uow.sessions.get_active_for_execution(
            execution.id,
            for_update=True,
        )
        latest_session = uow.sessions.get_latest_for_execution(
            execution.id,
            transition.workspace_id,
        )
        if binding is None:
            if active_session is not None or execution.status not in {
                ExecutionStatus.QUEUED,
                ExecutionStatus.LEASED,
            }:
                return self._mark_unknown(
                    uow,
                    transition,
                    failure_code="target_runtime_binding_missing",
                    now=now,
                )
            return "deferred"

        acknowledgement = uow.context_packages.get_ack(
            package.package_id,
            ContextConsumerType.WORKER,
            execution.id,
            transition.workspace_id,
            for_update=True,
        )
        if acknowledgement is None:
            if active_session is not None or latest_session is not None:
                return self._mark_unknown(
                    uow,
                    transition,
                    failure_code="target_context_ack_missing",
                    now=now,
                )
            return "deferred"
        if (
            package.status is not ContextPackageStatus.ACTIVE
            or package.expires_at <= now
        ):
            if active_session is not None or latest_session is not None:
                return self._mark_unknown(
                    uow,
                    transition,
                    failure_code="target_context_package_unavailable",
                    now=now,
                )
            return self._mark_failed(
                uow,
                transition,
                failure_code="target_context_package_unavailable",
                now=now,
            )
        if (
            acknowledgement.package_hash != package.package_hash
            or acknowledgement.consumer_type is not ContextConsumerType.WORKER
            or acknowledgement.consumer_id != execution.id
            or acknowledgement.context_version != transition.target_context_version
            or acknowledgement.adapter_module_id != binding.adapter_module_id
            or acknowledgement.adapter_config_version != binding.adapter_config_version
        ):
            return self._mark_unknown(
                uow,
                transition,
                failure_code="target_context_ack_mismatch",
                now=now,
            )

        if active_session is not None:
            if (
                execution.status is not ExecutionStatus.RUNNING
                or execution.active_lease_id != binding.lease_id
                or execution.last_fencing_token != binding.fencing_token
                or active_session.execution_id != execution.id
                or active_session.worker_id != binding.worker_id
            ):
                return self._mark_unknown(
                    uow,
                    transition,
                    failure_code="target_session_runtime_mismatch",
                    now=now,
                )
            return self._finish_activation(
                uow,
                transition,
                execution_id=execution.id,
                session_id=active_session.id,
                now=now,
            )

        if execution.status in TERMINAL_EXECUTION_STATUSES:
            if latest_session is None:
                return self._mark_failed(
                    uow,
                    transition,
                    failure_code="target_execution_finished_without_session",
                    now=now,
                )
            if (
                latest_session.execution_id != execution.id
                or latest_session.worker_id != binding.worker_id
            ):
                return self._mark_unknown(
                    uow,
                    transition,
                    failure_code="target_session_identity_mismatch",
                    now=now,
                )
            return self._finish_activation(
                uow,
                transition,
                execution_id=execution.id,
                session_id=latest_session.id,
                now=now,
            )
        if execution.status in {
            ExecutionStatus.QUEUED,
            ExecutionStatus.LEASED,
        }:
            return "deferred"
        return self._mark_unknown(
            uow,
            transition,
            failure_code="target_execution_without_session",
            now=now,
        )

    def _finish_activation(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        execution_id: UUID,
        session_id: UUID,
        now: datetime,
    ) -> str:
        if (
            transition.target_execution_id is not None
            and transition.target_execution_id != execution_id
        ):
            return self._mark_unknown(
                uow,
                transition,
                failure_code="target_execution_identity_changed",
                now=now,
            )
        if (
            transition.target_session_id is not None
            and transition.target_session_id != session_id
        ):
            return self._mark_unknown(
                uow,
                transition,
                failure_code="target_session_identity_changed",
                now=now,
            )
        previous_version = transition.version
        transition.bind_target(
            execution_id=execution_id,
            session_id=session_id,
            now=now,
        )
        transition.converge(now=now)
        uow.worker_transitions.save(transition, previous_version)
        self._append_transition_event(
            uow,
            transition,
            aggregate_type="worker_transition",
            aggregate_id=transition.id,
            aggregate_version=transition.version,
            event_type="WorkerTransitionActivated",
            payload={
                "worker_transition_id": str(transition.id),
                "worker_item_id": str(transition.worker_item_id),
                "target_execution_id": str(execution_id),
                "target_session_id": str(session_id),
                "status": transition.status.value,
            },
            now=now,
        )
        return "converged"

    def _ensure_source_safe(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        now: datetime,
    ) -> str:
        if transition.source_execution_id is None:
            return "safe"
        execution = uow.executions.get(
            transition.source_execution_id,
            transition.workspace_id,
            for_update=True,
        )
        if execution is None:
            return self._mark_unknown(
                uow, transition, failure_code="source_execution_missing", now=now
            )
        if transition.source_session_id is not None:
            source_session = uow.sessions.get_any(
                transition.source_session_id,
                transition.workspace_id,
                for_update=True,
            )
            if (
                source_session is None
                or source_session.execution_id != execution.id
                or source_session.workspace_id != execution.workspace_id
            ):
                return self._mark_unknown(
                    uow,
                    transition,
                    failure_code="source_session_identity_mismatch",
                    now=now,
                )
        elif transition.action is WorkerTransitionAction.CONTINUE:
            return self._mark_unknown(
                uow,
                transition,
                failure_code="source_session_missing",
                now=now,
            )
        active_session = uow.sessions.get_active_for_execution(
            execution.id, for_update=True
        )
        if active_session is not None:
            if transition.source_session_id != active_session.id:
                return self._mark_unknown(
                    uow,
                    transition,
                    failure_code="source_session_identity_mismatch",
                    now=now,
                )
            if active_session.status is not ExecutionSessionStatus.ACTIVE:
                return self._mark_unknown(
                    uow,
                    transition,
                    failure_code="source_session_not_active",
                    now=now,
                )
            if execution.active_lease_id is None:
                return self._mark_unknown(
                    uow, transition, failure_code="source_lease_missing", now=now
                )
            lease = uow.leases.get(
                execution.active_lease_id,
                transition.workspace_id,
                for_update=True,
            )
            if (
                lease is None
                or lease.status is not LeaseStatus.ACTIVE
                or now >= lease.expires_at
                or lease.fencing_token != execution.last_fencing_token
            ):
                return self._mark_unknown(
                    uow, transition, failure_code="source_lease_not_safe", now=now
                )
            operation = uow.gateway_operations.get_by_operation_id(
                self._stop_operation_id(execution.id),
                transition.workspace_id,
            )
            if execution.stop_requested:
                if operation is not None and operation.status is GatewayOperationStatus.UNKNOWN:
                    return self._mark_unknown(
                        uow,
                        transition,
                        failure_code="stop_result_unknown",
                        now=now,
                    )
                if operation is not None and operation.status is GatewayOperationStatus.FAILED:
                    return self._mark_unknown(
                        uow,
                        transition,
                        failure_code="stop_operation_failed",
                        now=now,
                    )
                if operation is not None and operation.status is GatewayOperationStatus.SUCCEEDED:
                    return self._mark_unknown(
                        uow,
                        transition,
                        failure_code="stop_success_not_reflected",
                        now=now,
                    )
                if operation is None:
                    self._append_stop_event(
                        uow, transition, execution, active_session, now=now
                    )
                return "deferred"

            previous_version = execution.version
            execution.request_stop()
            uow.executions.save(execution, previous_version)
            self._append_stop_event(
                uow, transition, execution, active_session, now=now
            )
            return "deferred"

        if execution.status is ExecutionStatus.QUEUED and execution.active_lease_id is None:
            previous_version = execution.version
            execution.cancel(now=now)
            uow.executions.save(execution, previous_version)
            self._append_transition_event(
                uow,
                transition,
                aggregate_type="execution",
                aggregate_id=execution.id,
                aggregate_version=execution.version,
                event_type="ExecutionCanceledByPlanTransition",
                payload={
                    "execution_id": str(execution.id),
                    "worker_item_id": str(transition.worker_item_id),
                    "reason": "plan_transition_safe_boundary",
                },
                now=now,
            )
            self._supersede_pending_input(uow, transition, now=now)
            return "safe"
        if (
            execution.status in TERMINAL_EXECUTION_STATUSES
            and execution.active_lease_id is None
        ):
            self._supersede_pending_input(uow, transition, now=now)
            return "safe"
        return self._mark_unknown(
            uow,
            transition,
            failure_code="source_runtime_without_active_session",
            now=now,
        )

    def _ensure_execution_request(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        now: datetime,
    ) -> ExecutionRequest:
        plan = uow.plan_transitions.get(
            transition.plan_transition_id,
            transition.workspace_id,
        )
        snapshot = (
            uow.plans.get(plan.to_snapshot_id, transition.workspace_id)
            if plan is not None
            else None
        )
        planning_request = (
            uow.planning_requests.get(
                plan.planning_request_id, transition.workspace_id
            )
            if plan is not None
            else None
        )
        proposal = (
            uow.planning_records.get_proposal(
                plan.proposal_revision_id, transition.workspace_id
            )
            if plan is not None
            else None
        )
        if plan is None or snapshot is None or planning_request is None or proposal is None:
            raise InvalidTransition("transition target planning records are unavailable")
        if planning_request.status is not PlanningRequestStatus.MATERIALIZED:
            raise InvalidTransition("transition planning request is not materialized")
        if proposal.id != plan.proposal_revision_id:
            raise InvalidTransition("transition proposal revision is not current")

        worker_plan = next(
            (
                item
                for item in snapshot.worker_item_plans
                if item.worker_item_id == transition.worker_item_id
            ),
            None,
        )
        if worker_plan is None:
            raise InvalidTransition("transition target WorkerItem plan is missing")
        subtask = next(
            (item for item in snapshot.subtasks if item.key == worker_plan.subtask_key),
            None,
        )
        if subtask is None or subtask.task_id is None:
            raise InvalidTransition("transition target subtask is missing")
        intent = next(
            (
                item
                for item in proposal.payload.execution_intents
                if item.worker_item_key == worker_plan.key
            ),
            None,
        )
        if intent is None:
            raise InvalidTransition("transition target execution intent is missing")

        source_execution_id = (
            transition.source_execution_id
            if transition.action is WorkerTransitionAction.CONTINUE
            else None
        )
        source_session_id = (
            transition.source_session_id
            if source_execution_id is not None
            else None
        )
        request = uow.execution_requests.get_by_proposal_worker_item(
            plan.proposal_revision_id,
            worker_plan.key,
            transition.workspace_id,
            for_update=True,
        )
        if request is not None:
            self._assert_request_matches_transition(
                request,
                transition=transition,
                plan=plan,
                snapshot=snapshot,
                subtask_id=subtask.task_id,
                source_execution_id=source_execution_id,
                source_session_id=source_session_id,
            )
            return request

        request_id = uuid5(
            NAMESPACE_URL,
            f"sidera.plan-transition.execution-request:{transition.id}:{worker_plan.key}",
        )
        request = ExecutionRequest.create(
            request_id=request_id,
            workspace_id=transition.workspace_id,
            planning_request_id=plan.planning_request_id,
            proposal_revision_id=plan.proposal_revision_id,
            plan_snapshot_id=plan.to_snapshot_id,
            plan_generation=plan.to_generation,
            root_task_id=plan.root_task_id,
            subtask_id=subtask.task_id,
            worker_item_id=transition.worker_item_id,
            worker_item_key=worker_plan.key,
            intent_key=worker_plan.key,
            purpose=intent.purpose,
            capability_requirements=frozenset(intent.capability_requirements),
            adapter_preferences=intent.adapter_preferences,
            adapter_forbidden=planning_request.profile_snapshot.adapter_forbidden,
            budget_class=intent.budget_class,
            not_before=now + timedelta(seconds=intent.not_before_seconds or 0),
            max_attempts=intent.max_attempts,
            expected_artifact_refs=intent.expected_artifact_refs,
            expected_evidence_refs=intent.required_evidence_refs,
            expected_outputs=subtask.expected_outputs,
            now=now,
            source_execution_id=source_execution_id,
            source_session_id=source_session_id,
        )
        uow.execution_requests.add(request)
        uow.flush()
        self._add_request_dependencies(
            uow, request, snapshot=snapshot, worker_plan_key=worker_plan.key
        )
        self._append_transition_event(
            uow,
            transition,
            aggregate_type="execution_request",
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_type="ExecutionRequestCreatedByPlanTransition",
            payload={
                "execution_request_id": str(request.id),
                "worker_item_id": str(request.worker_item_id),
                "worker_item_key": request.worker_item_key,
                "plan_snapshot_id": str(request.plan_snapshot_id),
                "plan_generation": request.plan_generation,
                "source_execution_id": str(source_execution_id)
                if source_execution_id is not None
                else None,
                "source_session_id": str(source_session_id)
                if source_session_id is not None
                else None,
            },
            now=now,
        )
        return request

    def _supersede_pending_input(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        now: datetime,
    ) -> None:
        if transition.source_session_id is None:
            return
        item = uow.execution_inputs.get_pending_for_session(
            transition.source_session_id,
            transition.workspace_id,
            for_update=True,
        )
        if item is None or item.status not in {
            ExecutionInputStatus.ACCEPTED,
            ExecutionInputStatus.QUEUED,
        }:
            return
        previous_version = item.version
        item.supersede(now=now, reason="session_superseded_by_plan_revision")
        uow.execution_inputs.save(item, previous_version)
        self._append_transition_event(
            uow,
            transition,
            aggregate_type="execution_input",
            aggregate_id=item.id,
            aggregate_version=item.version,
            event_type="ExecutionInputSuperseded",
            payload={
                "execution_id": str(item.execution_id),
                "session_id": str(item.session_id),
                "worker_item_id": str(transition.worker_item_id),
                "status": item.status.value,
                "reason": item.superseded_reason,
            },
            now=now,
        )

    def _mark_unknown(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        failure_code: str,
        now: datetime,
    ) -> str:
        previous_version = transition.version
        transition.mark_unknown(failure_code=failure_code, now=now)
        uow.worker_transitions.save(transition, previous_version)
        self._append_transition_event(
            uow,
            transition,
            aggregate_type="worker_transition",
            aggregate_id=transition.id,
            aggregate_version=transition.version,
            event_type="WorkerTransitionBlockedUnknown",
            payload={
                "worker_transition_id": str(transition.id),
                "worker_item_id": str(transition.worker_item_id),
                "failure_code": failure_code,
                "status": transition.status.value,
                "writer_state": transition.writer_state.value,
            },
            now=now,
        )
        return "unknown"

    def _mark_failed(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        failure_code: str,
        now: datetime,
    ) -> str:
        previous_version = transition.version
        transition.fail(failure_code=failure_code, writer_closed=True, now=now)
        uow.worker_transitions.save(transition, previous_version)
        self._append_transition_event(
            uow,
            transition,
            aggregate_type="worker_transition",
            aggregate_id=transition.id,
            aggregate_version=transition.version,
            event_type="WorkerTransitionFailed",
            payload={
                "worker_transition_id": str(transition.id),
                "worker_item_id": str(transition.worker_item_id),
                "failure_code": failure_code,
                "status": transition.status.value,
                "writer_state": transition.writer_state.value,
            },
            now=now,
        )
        return "failed"

    def _append_stop_event(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        execution: Execution,
        session: ExecutionSession,
        *,
        now: datetime,
    ) -> None:
        execution_id = execution.id
        session_id = session.id
        lease_id = execution.active_lease_id
        fencing_token = execution.last_fencing_token
        if lease_id is None:
            raise InvalidTransition("stop event requires an active execution lease")
        command_id = uuid5(
            NAMESPACE_URL,
            f"sidera.plan-transition.stop:{transition.id}:{execution_id}",
        )
        envelope = self._transition_envelope(
            transition,
            event_type="ExecutionStopRequested",
            aggregate_id=execution_id,
            aggregate_version=execution.version,
            command_id=command_id,
        )
        event = EventData(
            id=uuid4(),
            workspace_id=transition.workspace_id,
            aggregate_type="execution",
            aggregate_id=execution_id,
            aggregate_version=execution.version,
            event_type="ExecutionStopRequested",
            payload={
                "execution_id": str(execution_id),
                "session_id": str(session_id),
                "lease_id": str(lease_id),
                "fencing_token": fencing_token,
                "command_id": str(command_id),
                "worker_transition_id": str(transition.id),
                "now": now.isoformat(),
            },
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )
        self._record_internal_event(uow, envelope, event)

    def _append_transition_event(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        aggregate_version: int,
        event_type: str,
        payload: dict[str, object],
        now: datetime,
    ) -> None:
        del now
        envelope = self._transition_envelope(
            transition,
            event_type=event_type,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
        )
        event = EventData(
            id=uuid4(),
            workspace_id=transition.workspace_id,
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
        self._record_internal_event(uow, envelope, event)

    @staticmethod
    def _record_internal_event(
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
        event: EventData,
    ) -> None:
        if uow.commands.find_by_id(envelope.workspace_id, envelope.id) is not None:
            return
        uow.commands.record_applied(
            envelope,
            hash_request(envelope),
            CommandResult(
                command_id=envelope.id,
                aggregate_id=event.aggregate_id,
                aggregate_version=event.aggregate_version,
                event_ids=(event.id,),
            ),
        )
        uow.events.append_with_outbox(event)

    def _transition_envelope(
        self,
        transition: WorkerTransition,
        *,
        event_type: str,
        aggregate_id: UUID,
        aggregate_version: int,
        command_id: UUID | None = None,
    ) -> CommandEnvelope:
        resolved_command_id = command_id or uuid5(
            NAMESPACE_URL,
            f"sidera.plan-transition:{transition.id}:"
            f"{event_type}:{aggregate_id}:v{aggregate_version}",
        )
        return CommandEnvelope(
            id=resolved_command_id,
            workspace_id=transition.workspace_id,
            command_type="PlanTransitionRuntime",
            actor_type="system",
            actor_id=self._owner_id,
            target_type="worker_transition",
            target_id=transition.id,
            payload={
                "worker_transition_id": str(transition.id),
                "event_type": event_type,
                "aggregate_id": str(aggregate_id),
            },
            expected_version=aggregate_version,
            idempotency_key=(
                f"plan-transition:{transition.id}:{event_type}:"
                f"{aggregate_id}:v{aggregate_version}"
            ),
            correlation_id=transition.plan_transition_id,
        )

    @staticmethod
    def _assert_request_matches_transition(
        request: ExecutionRequest,
        *,
        transition: WorkerTransition,
        plan: object,
        snapshot: TaskPlanSnapshot,
        subtask_id: UUID,
        source_execution_id: UUID | None,
        source_session_id: UUID | None,
    ) -> None:
        if (
            request.workspace_id != transition.workspace_id
            or request.plan_snapshot_id != snapshot.id
            or request.plan_generation != snapshot.generation
            or request.worker_item_id != transition.worker_item_id
            or request.subtask_id != subtask_id
            or request.source_execution_id != source_execution_id
            or request.source_session_id != source_session_id
        ):
            raise InvalidTransition("existing transition request has different lineage")
        if request.status in {
            ExecutionRequestStatus.CANCELLED,
            ExecutionRequestStatus.OBSOLETE,
        }:
            raise InvalidTransition("existing transition request is closed")
        del plan

    @staticmethod
    def _add_request_dependencies(
        uow: ControlPlaneUnitOfWork,
        request: ExecutionRequest,
        *,
        snapshot: TaskPlanSnapshot,
        worker_plan_key: str,
    ) -> None:
        for dependency in snapshot.worker_item_dependencies:
            if dependency.downstream_worker_item_key != worker_plan_key:
                continue
            upstream = uow.execution_requests.get_by_proposal_worker_item(
                request.proposal_revision_id,
                dependency.upstream_worker_item_key,
                request.workspace_id,
            )
            if upstream is None:
                continue
            uow.execution_requests.add_dependency(
                ExecutionRequestDependency(
                    downstream_request_id=request.id,
                    upstream_request_id=upstream.id,
                    enforcement=(
                        DependencyEnforcement.HARD
                        if dependency.kind == "dependency"
                        else DependencyEnforcement.INFORMATIONAL
                    ),
                    required_artifact_refs=dependency.required_artifact_refs,
                )
            )

    def _converge_preserve_or_move(
        self, claimed: _ClaimedWorkerTransition, *, now: datetime
    ) -> bool:
        with self._uow_factory() as uow:
            transition = uow.worker_transitions.get(
                claimed.id,
                claimed.workspace_id,
                for_update=True,
            )
            if transition is None:
                return False
            if (
                transition.version != claimed.version
                or transition.status is not WorkerTransitionStatus.WAITING_SAFE_BOUNDARY
                or transition.lease_id != claimed.lease_id
                or transition.fencing_token != claimed.fencing_token
            ):
                return False
            if not self._head_matches(uow, transition):
                return False
            if not self._runtime_is_safe(uow, transition, now=now):
                return False
            previous_version = transition.version
            transition.converge(now=now)
            uow.worker_transitions.save(transition, previous_version)
            return True

    def _converge_patch(self, claimed: _ClaimedWorkerTransition) -> str:
        if self._patch_delivery is None or claimed.lease_id is None:
            return "deferred"
        result = self._patch_delivery.deliver(
            transition_id=claimed.id,
            workspace_id=claimed.workspace_id,
            expected_version=claimed.version,
            lease_id=claimed.lease_id,
            fencing_token=claimed.fencing_token,
        )
        return result.outcome

    def _reconcile_plans(self, *, now: datetime, limit: int) -> int:
        reconciled = 0
        with self._uow_factory() as uow:
            plans = uow.plan_transitions.claim_converging(now=now, limit=limit)
            for plan in plans:
                workers = uow.worker_transitions.list_for_plan_transition(
                    plan.id, plan.workspace_id
                )
                previous_version = plan.version
                plan.reconcile(workers, now=now)
                uow.plan_transitions.save(plan, previous_version)
                reconciled += 1
        return reconciled

    def _resolve_claim_lease(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        now: datetime,
    ) -> tuple[UUID | None, int] | None:
        next_token = max(1, transition.fencing_token + 1)
        if transition.source_execution_id is None:
            return None, next_token

        execution = uow.executions.get(
            transition.source_execution_id,
            transition.workspace_id,
            for_update=True,
        )
        if execution is None:
            return None
        if not self._runtime_is_safe(uow, transition, now=now, execution=execution):
            return None
        if execution.active_lease_id is None:
            return (None, next_token)
        lease = uow.leases.get(
            execution.active_lease_id,
            transition.workspace_id,
            for_update=True,
        )
        if lease is None or lease.status is not LeaseStatus.ACTIVE:
            return None
        if now >= lease.expires_at:
            return None
        if transition.fencing_token > lease.fencing_token:
            return None
        return lease.id, lease.fencing_token

    def _resolve_patch_lease(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        now: datetime,
    ) -> tuple[UUID, int] | None:
        if transition.source_execution_id is None:
            return None
        execution = uow.executions.get(
            transition.source_execution_id,
            transition.workspace_id,
            for_update=True,
        )
        if execution is None or execution.active_lease_id is None:
            return None
        lease = uow.leases.get(
            execution.active_lease_id,
            transition.workspace_id,
            for_update=True,
        )
        if (
            lease is None
            or lease.status is not LeaseStatus.ACTIVE
            or now >= lease.expires_at
        ):
            return None
        if transition.lease_id is not None and transition.lease_id != lease.id:
            return None
        if transition.fencing_token > lease.fencing_token:
            return None
        return lease.id, lease.fencing_token

    def _resolve_lifecycle_lease(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        now: datetime,
    ) -> tuple[UUID | None, int] | None:
        if transition.status is not WorkerTransitionStatus.PENDING:
            return transition.lease_id, max(1, transition.fencing_token)
        next_token = max(1, transition.fencing_token + 1)
        if transition.source_execution_id is None:
            return None, next_token
        execution = uow.executions.get(
            transition.source_execution_id,
            transition.workspace_id,
            for_update=True,
        )
        if execution is None:
            return None
        active_session = uow.sessions.get_active_for_execution(
            execution.id, for_update=True
        )
        if active_session is not None:
            try:
                transition.remember_source_session(active_session.id)
            except StaleFencingToken:
                return None
            if execution.active_lease_id is None:
                return None
            lease = uow.leases.get(
                execution.active_lease_id,
                transition.workspace_id,
                for_update=True,
            )
            if (
                lease is None
                or lease.status is not LeaseStatus.ACTIVE
                or now >= lease.expires_at
                or transition.fencing_token > lease.fencing_token
            ):
                return None
            return lease.id, lease.fencing_token

        if transition.source_session_id is None:
            latest = uow.sessions.get_latest_for_execution(
                execution.id,
                transition.workspace_id,
            )
            if latest is not None:
                try:
                    transition.remember_source_session(latest.id)
                except StaleFencingToken:
                    return None

        if execution.active_lease_id is not None:
            lease = uow.leases.get(
                execution.active_lease_id,
                transition.workspace_id,
                for_update=True,
            )
            if (
                lease is None
                or lease.status is not LeaseStatus.ACTIVE
                or now >= lease.expires_at
                or transition.fencing_token > lease.fencing_token
            ):
                return None
            return lease.id, lease.fencing_token

        if execution.status in {
            ExecutionStatus.QUEUED,
            *TERMINAL_EXECUTION_STATUSES,
        }:
            if transition.lease_id is not None:
                return transition.lease_id, max(1, transition.fencing_token)
            return None, next_token
        return None

    def _patch_fallback_available(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
    ) -> bool:
        if transition.fallback_action is not WorkerTransitionAction.CONTINUE:
            return False
        if self._patch_delivery is None:
            return True
        if transition.source_execution_id is None:
            return False
        binding = uow.bindings.get_for_execution(
            transition.source_execution_id,
            transition.workspace_id,
        )
        if binding is None:
            return True
        return 1 not in binding.capability_snapshot.context_patch_versions

    @staticmethod
    def _stop_operation_id(execution_id: UUID) -> UUID:
        return gateway_operation_id(execution_id, GatewayOperationType.STOP_SESSION)

    @staticmethod
    def _patch_ready(
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        now: datetime,
    ) -> bool:
        """Check durable patch prerequisites before taking the transition fence."""
        if transition.source_execution_id is None:
            return False
        execution = uow.executions.get(
            transition.source_execution_id,
            transition.workspace_id,
            for_update=True,
        )
        if execution is None or execution.active_lease_id is None:
            return False
        lease = uow.leases.get(
            execution.active_lease_id,
            transition.workspace_id,
            for_update=True,
        )
        if (
            lease is None
            or lease.status is not LeaseStatus.ACTIVE
            or now >= lease.expires_at
            or lease.fencing_token < transition.fencing_token
        ):
            return False
        if execution.worker_item_id != transition.worker_item_id:
            return False
        session = uow.sessions.get_active_for_execution(
            execution.id, for_update=True
        )
        if session is None:
            return False
        worker_item = uow.worker_items.get(
            transition.worker_item_id,
            transition.workspace_id,
            for_update=True,
        )
        if (
            worker_item is None
            or worker_item.context_version != transition.target_context_version
            or worker_item.acknowledged_context_version
            >= transition.target_context_version
        ):
            return False
        binding = uow.bindings.get_for_execution(
            execution.id, transition.workspace_id, for_update=True
        )
        if (
            binding is None
            or binding.lease_id != lease.id
            or binding.fencing_token != lease.fencing_token
            or 1 not in binding.capability_snapshot.context_patch_versions
        ):
            return False
        package = uow.context_packages.get_for_consumer(
            ContextPurpose.EXECUTION,
            execution.id,
            transition.workspace_id,
            for_update=True,
        )
        if (
            package is None
            or package.status is not ContextPackageStatus.ACTIVE
            or package.worker_item_id != worker_item.id
            or package.context_version is None
            or package.context_version >= transition.target_context_version
            or now >= package.expires_at
        ):
            return False
        package_ack = uow.context_packages.get_ack(
            package.package_id,
            ContextConsumerType.WORKER,
            execution.id,
            transition.workspace_id,
        )
        return bool(
            package_ack is not None
            and package_ack.package_hash == package.package_hash
            and package_ack.context_version == package.context_version
            and package_ack.consumer_id == execution.id
            and package_ack.adapter_module_id == binding.adapter_module_id
            and package_ack.adapter_config_version == binding.adapter_config_version
        )

    def _runtime_is_safe(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        now: datetime,
        execution: object | None = None,
    ) -> bool:
        if transition.source_execution_id is None:
            return True
        source = execution
        if source is None:
            source = uow.executions.get(
                transition.source_execution_id,
                transition.workspace_id,
                for_update=True,
            )
        if source is None:
            return False
        active_lease_id = getattr(source, "active_lease_id", None)
        has_valid_lease = False
        if active_lease_id is not None:
            lease = uow.leases.get(
                active_lease_id,
                transition.workspace_id,
                for_update=True,
            )
            if lease is None or lease.status is not LeaseStatus.ACTIVE:
                return False
            if now >= lease.expires_at:
                return False
            has_valid_lease = True
        active_session = uow.sessions.get_active_for_execution(
            transition.source_execution_id
        )
        if active_session is not None:
            return False
        if transition.source_session_id is not None:
            session = uow.sessions.get(
                transition.source_session_id,
                transition.workspace_id,
                for_update=True,
            )
            if session is not None:
                return False
        return has_valid_lease or getattr(source, "status", None) in {
            *TERMINAL_EXECUTION_STATUSES,
            ExecutionStatus.QUEUED,
        }

    @staticmethod
    def _head_matches(
        uow: ControlPlaneUnitOfWork, transition: WorkerTransition
    ) -> bool:
        plan = uow.plan_transitions.get(
            transition.plan_transition_id,
            transition.workspace_id,
        )
        if plan is None:
            return False
        head = uow.plans.get_head(
            transition.root_task_id,
            transition.workspace_id,
        )
        return bool(
            head is not None
            and head.active_snapshot_id == plan.to_snapshot_id
            and head.active_generation == plan.to_generation
        )

    @staticmethod
    def _dependencies_ready(
        uow: ControlPlaneUnitOfWork, transition: WorkerTransition
    ) -> bool:
        plan = uow.plan_transitions.get(
            transition.plan_transition_id, transition.workspace_id
        )
        if plan is None:
            return False
        snapshot = uow.plans.get(plan.to_snapshot_id, transition.workspace_id)
        if snapshot is None:
            return False
        if not snapshot.worker_item_dependencies:
            return True
        worker_key_by_id = {
            item.worker_item_id: item.key
            for item in snapshot.worker_item_plans
            if item.worker_item_id is not None
        }
        current_key = worker_key_by_id.get(transition.worker_item_id)
        if current_key is None:
            return False
        statuses = {
            item.worker_item_id: item.status
            for item in uow.worker_transitions.list_for_plan_transition(
                transition.plan_transition_id, transition.workspace_id
            )
        }
        for dependency in snapshot.worker_item_dependencies:
            if dependency.downstream_worker_item_key != current_key:
                continue
            if dependency.kind != "dependency":
                continue
            upstream_id = next(
                (
                    worker_id
                    for worker_id, key in worker_key_by_id.items()
                    if key == dependency.upstream_worker_item_key
                ),
                None,
            )
            if upstream_id is None or statuses.get(upstream_id) is not WorkerTransitionStatus.CONVERGED:
                return False
        return True

    @staticmethod
    def _dependency_order(
        uow: ControlPlaneUnitOfWork,
        transitions: Iterable[WorkerTransition],
    ) -> tuple[WorkerTransition, ...]:
        items = tuple(transitions)
        by_plan: dict[UUID, list[WorkerTransition]] = defaultdict(list)
        for item in items:
            by_plan[item.plan_transition_id].append(item)
        ordered: list[WorkerTransition] = []
        for plan_id, plan_items in sorted(by_plan.items(), key=lambda pair: str(pair[0])):
            first = plan_items[0]
            plan = uow.plan_transitions.get(plan_id, first.workspace_id)
            snapshot = (
                uow.plans.get(plan.to_snapshot_id, first.workspace_id)
                if plan is not None
                else None
            )
            key_by_id = (
                {
                    item.worker_item_id: item.key
                    for item in snapshot.worker_item_plans
                    if item.worker_item_id is not None
                }
                if snapshot is not None
                else {}
            )
            by_key = {key_by_id.get(item.worker_item_id, str(item.worker_item_id)): item for item in plan_items}
            edges: dict[UUID, set[UUID]] = {item.id: set() for item in plan_items}
            indegree = {item.id: 0 for item in plan_items}
            if snapshot is not None:
                for dependency in snapshot.worker_item_dependencies:
                    upstream = by_key.get(dependency.upstream_worker_item_key)
                    downstream = by_key.get(dependency.downstream_worker_item_key)
                    if upstream is None or downstream is None or downstream.id in edges[upstream.id]:
                        continue
                    edges[upstream.id].add(downstream.id)
                    indegree[downstream.id] += 1
            ready = sorted(
                (item.id for item in plan_items if indegree[item.id] == 0),
                key=lambda item_id: _stable_transition_key(item_id, by_key),
            )
            by_id = {item.id: item for item in plan_items}
            group_ordered: list[WorkerTransition] = []
            while ready:
                item_id = ready.pop(0)
                group_ordered.append(by_id[item_id])
                for downstream_id in sorted(edges[item_id], key=str):
                    indegree[downstream_id] -= 1
                    if indegree[downstream_id] == 0:
                        ready.append(downstream_id)
                        ready.sort(key=lambda value: _stable_transition_key(value, by_key))
            if len(group_ordered) < len(plan_items):
                seen = {item.id for item in group_ordered}
                group_ordered.extend(
                    sorted(
                        (item for item in plan_items if item.id not in seen),
                        key=lambda item: _stable_transition_key(item.id, by_key),
                    )
                )
            ordered.extend(group_ordered)
        return tuple(ordered)


def _stable_transition_key(
    transition_id: UUID, by_key: dict[str, WorkerTransition]
) -> tuple[str, str]:
    item = next((value for value in by_key.values() if value.id == transition_id), None)
    return (str(item.worker_item_id) if item is not None else "", str(transition_id))
