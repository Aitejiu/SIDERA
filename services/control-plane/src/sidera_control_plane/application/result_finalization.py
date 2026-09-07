from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.execution_commands import ExecutionCommandHandler
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.lease import LeaseStatus
from sidera_control_plane.domain.plan_transitions import (
    WORKER_TRANSITION_IN_FLIGHT,
    PlanTransitionStatus,
    WorkerTransitionStatus,
    WriterState,
)
from sidera_control_plane.domain.result_operations import (
    ResultOperationStatus,
    TaskResultHead,
)
from sidera_control_plane.domain.result_verification import TaskAcceptanceDecisionV1
from sidera_control_plane.domain.task import TERMINAL_TASK_STATUSES, Task, TaskStatus

_FINALIZATION_NAMESPACE = uuid5(NAMESPACE_URL, "sidera:result-finalization:v1")
_ACTIVE_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.QUEUED,
        ExecutionStatus.LEASED,
        ExecutionStatus.RUNNING,
    }
)


class FinalizationState(StrEnum):
    COMPLETED = "completed"
    PENDING = "pending"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class FinalizationOutcome:
    state: str
    blocking_execution_ids: tuple[UUID, ...] = ()
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {item.value for item in FinalizationState}:
            raise ValueError(f"unsupported finalization state: {self.state}")
        ordered_ids = tuple(sorted(set(self.blocking_execution_ids), key=str))
        if len(ordered_ids) != len(self.blocking_execution_ids):
            raise ValueError("blocking execution ids must be unique")
        object.__setattr__(self, "blocking_execution_ids", ordered_ids)
        if self.failure_code is not None:
            code = self.failure_code.strip()
            if not code:
                raise ValueError("failure_code must not be empty")
            object.__setattr__(self, "failure_code", code)


@dataclass(frozen=True, slots=True)
class _DrainResult:
    state: str
    failure_code: str | None = None


class ResultFinalizationService:
    """Drain accepted-root execution work and complete the root exactly once."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        clock: Callable[[], datetime] | None = None,
        owner_id: str = "result-finalizer",
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner_id = owner_id.strip()

    def reconcile(self, workspace_id: UUID, root_task_id: UUID) -> FinalizationOutcome:
        with self._uow_factory() as uow:
            return self._reconcile_in_uow(uow, workspace_id, root_task_id)

    def reconcile_in_uow(
        self,
        uow: ControlPlaneUnitOfWork,
        workspace_id: UUID,
        root_task_id: UUID,
    ) -> FinalizationOutcome:
        """Reconcile while the caller owns the current unit of work.

        Result application uses this boundary after clearing its active
        operation so the receipt, head transition, and finalization decision
        commit together.
        """

        return self._reconcile_in_uow(uow, workspace_id, root_task_id)

    def _reconcile_in_uow(
        self,
        uow: ControlPlaneUnitOfWork,
        workspace_id: UUID,
        root_task_id: UUID,
    ) -> FinalizationOutcome:
        # Acceptance records and finalization use the same root -> head lock order.
        task = uow.tasks.get(root_task_id, workspace_id, for_update=True)
        if task is None:
            return _blocked("root_task_missing")
        head = uow.results.get_head(workspace_id, root_task_id, for_update=True)
        if head is None:
            return _blocked("result_head_missing")
        if head.current_acceptance_id is None:
            return _blocked("acceptance_required")

        acceptance = uow.results.get_record(
            "acceptance", head.current_acceptance_id, workspace_id
        )
        if not isinstance(acceptance, TaskAcceptanceDecisionV1):
            return _blocked("acceptance_record_missing")
        if (
            acceptance.workspace_id != workspace_id
            or acceptance.root_task_id != root_task_id
            or acceptance.id != head.current_acceptance_id
        ):
            return _blocked("acceptance_scope_mismatch")

        if task.status is TaskStatus.SUCCEEDED:
            return _completed()
        if task.status in TERMINAL_TASK_STATUSES:
            return _blocked("task_terminal")
        if task.status is not TaskStatus.WAITING:
            return _blocked("task_not_waiting_for_finalization")

        operation_outcome = self._active_operation_outcome(uow, head, workspace_id)
        if operation_outcome is not None:
            return operation_outcome

        plan = uow.plans.get_current_for_root(root_task_id, workspace_id)
        if plan is None:
            return _blocked("current_plan_missing")
        spec = plan.integration_spec
        if spec is None:
            return _blocked("integration_spec_required")

        transition_outcome = self._transition_outcome(
            uow, root_task_id, workspace_id, plan.id, plan.generation
        )
        if transition_outcome is not None:
            return transition_outcome

        requests = tuple(
            request
            for request in uow.execution_requests.list_for_root(
                root_task_id, workspace_id, for_update=True
            )
            if request.plan_snapshot_id == plan.id
            and request.plan_generation == plan.generation
        )
        blocking_ids: list[UUID] = []
        failure_code: str | None = None
        for request in requests:
            if request.status in {
                ExecutionRequestStatus.BLOCKED,
                ExecutionRequestStatus.READY,
            }:
                self._cancel_request(uow, request, acceptance.id)
                continue
            if request.execution_lineage_id is None:
                if request.status is ExecutionRequestStatus.MATERIALIZED:
                    failure_code = failure_code or "execution_lineage_missing"
                continue

            executions = uow.executions.list_for_lineage(
                request.execution_lineage_id, workspace_id
            )
            if not executions and request.first_execution_id is not None:
                first = uow.executions.get(request.first_execution_id, workspace_id)
                if first is not None:
                    executions = (first,)
            if not executions:
                if request.status is ExecutionRequestStatus.MATERIALIZED:
                    failure_code = failure_code or "execution_missing"
                continue

            for probe in executions:
                if probe.id in blocking_ids:
                    continue
                execution = uow.executions.get(probe.id, workspace_id, for_update=True)
                if execution is None:
                    failure_code = failure_code or "execution_missing"
                    continue
                drained = self._drain_execution(
                    uow,
                    execution,
                    acceptance_id=acceptance.id,
                    root_task_id=root_task_id,
                )
                if drained.state != "done":
                    blocking_ids.append(execution.id)
                    failure_code = failure_code or drained.failure_code

        if failure_code is not None:
            return _blocked(failure_code, blocking_ids)
        if blocking_ids:
            return _pending(tuple(blocking_ids))
        return complete_accepted_root(
            uow,
            task,
            head,
            acceptance,
            clock=self._clock,
            owner_id=self._owner_id,
        )

    def _active_operation_outcome(
        self,
        uow: ControlPlaneUnitOfWork,
        head: TaskResultHead,
        workspace_id: UUID,
    ) -> FinalizationOutcome | None:
        operation_id = head.active_operation_id
        if operation_id is None:
            return None
        operation = uow.results.get_operation(
            operation_id, workspace_id, for_update=True
        )
        if operation is None:
            return _blocked("active_result_operation_missing")
        status = getattr(operation, "status", None)
        if status is ResultOperationStatus.UNKNOWN:
            return _blocked("result_operation_inspect_required")
        if status in {ResultOperationStatus.PENDING, ResultOperationStatus.RUNNING}:
            return _pending(failure_code="result_operation_in_progress")
        if status in {ResultOperationStatus.BLOCKED, ResultOperationStatus.FAILED}:
            return _blocked(
                getattr(operation, "failure_code", None) or "result_operation_failed"
            )
        if status is ResultOperationStatus.SUCCEEDED:
            return _pending(failure_code="result_apply_confirmation_pending")
        return _blocked("result_operation_state_invalid")

    @staticmethod
    def _transition_outcome(
        uow: ControlPlaneUnitOfWork,
        root_task_id: UUID,
        workspace_id: UUID,
        snapshot_id: UUID,
        generation: int,
    ) -> FinalizationOutcome | None:
        for plan_transition in uow.plan_transitions.list_for_root(
            root_task_id, workspace_id
        ):
            if (
                plan_transition.to_snapshot_id != snapshot_id
                or plan_transition.to_generation != generation
            ):
                continue
            if plan_transition.status is PlanTransitionStatus.DEGRADED:
                return _blocked("plan_transition_degraded")
            if plan_transition.status is PlanTransitionStatus.CONVERGING:
                return _pending(failure_code="plan_transition_in_progress")
            for worker_transition in uow.worker_transitions.list_for_plan_transition(
                plan_transition.id, workspace_id
            ):
                if worker_transition.status is WorkerTransitionStatus.BLOCKED_UNKNOWN:
                    return _blocked("worker_transition_unknown")
                if worker_transition.writer_state is WriterState.UNKNOWN:
                    return _blocked("worker_writer_unknown")
                if worker_transition.status in WORKER_TRANSITION_IN_FLIGHT:
                    return _pending(failure_code="worker_transition_in_progress")
        return None

    def _drain_execution(
        self,
        uow: ControlPlaneUnitOfWork,
        execution: Execution,
        *,
        acceptance_id: UUID,
        root_task_id: UUID,
    ) -> _DrainResult:
        if execution.status not in _ACTIVE_EXECUTION_STATUSES:
            return _DrainResult("done")
        if execution.status is ExecutionStatus.QUEUED:
            return self._run_execution_command(
                uow,
                execution,
                command_type="CancelExecution",
                acceptance_id=acceptance_id,
                root_task_id=root_task_id,
            )

        lease_id = execution.active_lease_id
        if lease_id is None:
            return _DrainResult("blocked", "writer_state_unknown")
        lease = uow.leases.get(lease_id, execution.workspace_id, for_update=True)
        if lease is None or lease.status is not LeaseStatus.ACTIVE:
            return _DrainResult("blocked", "writer_state_unknown")

        session = uow.sessions.get_active_for_execution(execution.id, for_update=True)
        if execution.status is ExecutionStatus.LEASED and session is None:
            return self._run_execution_command(
                uow,
                execution,
                command_type="CancelExecution",
                acceptance_id=acceptance_id,
                root_task_id=root_task_id,
            )
        if session is None:
            return _DrainResult("blocked", "writer_state_unknown")
        if execution.stop_requested:
            return _DrainResult("pending")
        return self._run_execution_command(
            uow,
            execution,
            command_type="StopExecution",
            acceptance_id=acceptance_id,
            root_task_id=root_task_id,
        )

    def _run_execution_command(
        self,
        uow: ControlPlaneUnitOfWork,
        execution: Execution,
        *,
        command_type: str,
        acceptance_id: UUID,
        root_task_id: UUID,
    ) -> _DrainResult:
        envelope = _finalization_envelope(
            workspace_id=execution.workspace_id,
            root_task_id=root_task_id,
            acceptance_id=acceptance_id,
            target_type="execution",
            target_id=execution.id,
            command_type=command_type,
            expected_version=execution.version,
            owner_id=self._owner_id,
        )
        try:
            result = ExecutionCommandHandler(self._clock).handle(uow, envelope)
            uow.commands.record_applied(envelope, hash_request(envelope), result)
        except DomainError as exc:
            return _DrainResult("blocked", _failure_code(exc, "writer_stop_unknown"))
        return _DrainResult("pending" if command_type == "StopExecution" else "done")

    def _cancel_request(
        self,
        uow: ControlPlaneUnitOfWork,
        request: ExecutionRequest,
        acceptance_id: UUID,
    ) -> None:
        previous_version = request.version
        request.cancel(now=self._clock(), reason="result_acceptance_recorded")
        uow.execution_requests.save(request, previous_version)
        envelope = _finalization_envelope(
            workspace_id=request.workspace_id,
            root_task_id=request.root_task_id,
            acceptance_id=acceptance_id,
            target_type="execution_request",
            target_id=request.id,
            command_type="CancelExecutionRequest",
            expected_version=previous_version,
            owner_id=self._owner_id,
        )
        event = _event(
            envelope,
            aggregate_type="execution_request",
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_type="ExecutionRequestCanceled",
            payload={
                "execution_request_id": str(request.id),
                "root_task_id": str(request.root_task_id),
                "subtask_task_id": str(request.subtask_id),
                "worker_item_id": str(request.worker_item_id),
                "acceptance_id": str(acceptance_id),
                "reason": "result_acceptance_recorded",
                "status": request.status.value,
            },
        )
        result = CommandResult(
            command_id=envelope.id,
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_ids=(event.id,),
        )
        uow.commands.record_applied(envelope, hash_request(envelope), result)
        uow.events.append_with_outbox(event)


def complete_accepted_root(
    uow: ControlPlaneUnitOfWork,
    task: Task,
    head: TaskResultHead,
    acceptance: TaskAcceptanceDecisionV1,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    owner_id: str = "result-finalizer",
) -> FinalizationOutcome:
    if (
        head.current_acceptance_id != acceptance.id
        or acceptance.root_task_id != task.id
        or acceptance.workspace_id != task.workspace_id
    ):
        return _blocked("acceptance_scope_mismatch")
    if head.active_operation_id is not None:
        return _blocked("active_result_operation")
    if task.status is TaskStatus.SUCCEEDED:
        return _completed()
    if task.status in TERMINAL_TASK_STATUSES:
        return _blocked("task_terminal")
    if task.status is not TaskStatus.WAITING:
        return _blocked("task_not_waiting_for_finalization")

    previous_version = task.version
    task.complete()
    uow.tasks.save(task, previous_version)
    envelope = _finalization_envelope(
        workspace_id=task.workspace_id,
        root_task_id=task.id,
        acceptance_id=acceptance.id,
        target_type="task",
        target_id=task.id,
        command_type="FinalizeAcceptedTask",
        expected_version=previous_version,
        owner_id=owner_id,
    )
    event = _event(
        envelope,
        aggregate_type="task",
        aggregate_id=task.id,
        aggregate_version=task.version,
        event_type="TaskCompleted",
        payload={
            "status": task.status.value,
            "previous_status": TaskStatus.WAITING.value,
            "acceptance_id": str(acceptance.id),
            "candidate_id": str(acceptance.candidate_id),
        },
    )
    result = CommandResult(
        command_id=envelope.id,
        aggregate_id=task.id,
        aggregate_version=task.version,
        event_ids=(event.id,),
    )
    uow.commands.record_applied(envelope, hash_request(envelope), result)
    uow.events.append_with_outbox(event)
    return _completed()


def _finalization_envelope(
    *,
    workspace_id: UUID,
    root_task_id: UUID,
    acceptance_id: UUID,
    target_type: str,
    target_id: UUID,
    command_type: str,
    expected_version: int,
    owner_id: str,
) -> CommandEnvelope:
    command_id = uuid5(
        _FINALIZATION_NAMESPACE,
        f"{command_type}:{root_task_id}:{acceptance_id}:{target_id}:v{expected_version}",
    )
    return CommandEnvelope(
        id=command_id,
        workspace_id=workspace_id,
        command_type=command_type,
        target_type=target_type,
        actor_type="system",
        actor_id=owner_id,
        target_id=target_id,
        payload={
            "root_task_id": str(root_task_id),
            "acceptance_id": str(acceptance_id),
        },
        expected_version=expected_version,
        idempotency_key=(
            f"result-finalization:{command_type}:{target_id}:v{expected_version}"
        ),
        correlation_id=root_task_id,
    )


def _event(
    envelope: CommandEnvelope,
    *,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    event_type: str,
    payload: dict[str, object],
) -> EventData:
    event_id = uuid5(
        _FINALIZATION_NAMESPACE,
        f"event:{envelope.id}:{aggregate_type}:{aggregate_id}:v{aggregate_version}",
    )
    return EventData(
        id=event_id,
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


def _completed() -> FinalizationOutcome:
    return FinalizationOutcome(FinalizationState.COMPLETED.value)


def _pending(
    blocking_execution_ids: tuple[UUID, ...] = (),
    *,
    failure_code: str | None = None,
) -> FinalizationOutcome:
    return FinalizationOutcome(
        FinalizationState.PENDING.value,
        blocking_execution_ids,
        failure_code,
    )


def _blocked(
    failure_code: str, blocking_execution_ids: list[UUID] | tuple[UUID, ...] = ()
) -> FinalizationOutcome:
    return FinalizationOutcome(
        FinalizationState.BLOCKED.value,
        tuple(blocking_execution_ids),
        failure_code,
    )


def _failure_code(error: DomainError, fallback: str) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code.strip() else fallback


__all__ = [
    "FinalizationOutcome",
    "FinalizationState",
    "ResultFinalizationService",
    "complete_accepted_root",
]
