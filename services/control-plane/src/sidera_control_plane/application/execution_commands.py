from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    WorkerItemPlacement,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import (
    ExecutionContextChanged,
    InvalidTransition,
    StaleFencingToken,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import (
    TERMINAL_EXECUTION_STATUSES,
    Execution,
    ExecutionSession,
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.gateway import (
    GatewayOperationStatus,
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.domain.inputs import ExecutionInput
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.plan_normalization import PlanNormalizer
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.task import TaskStatus
from sidera_control_plane.domain.worker_item import WorkerItem, WorkerItemStatus


def _continuation_worker_id(execution: Execution, session: ExecutionSession) -> UUID:
    """Keep a continuation on the worker that owned the original session."""
    del execution
    return session.worker_id


def build_continuation_execution(
    source: Execution,
    source_session: ExecutionSession,
    *,
    not_before: datetime,
    execution_id: UUID,
    task_id: UUID | None = None,
    max_attempts: int | None = None,
    purpose: str | None = None,
    capability_requirements: frozenset[str] | None = None,
    adapter_preferences: tuple[str, ...] | None = None,
    adapter_forbidden: frozenset[str] | None = None,
) -> Execution:
    """Build a new attempt that keeps the WorkerItem and source lineage."""
    if source_session.execution_id != source.id:
        raise ValidationError("source Session does not belong to the source execution")
    if source_session.workspace_id != source.workspace_id:
        raise ValidationError("source Session belongs to another workspace")
    if source_session.status is ExecutionSessionStatus.ACTIVE:
        raise InvalidTransition("only a terminal Session can be continued")
    if source.status not in TERMINAL_EXECUTION_STATUSES:
        raise InvalidTransition("source execution is not terminal")
    if source.active_lease_id is not None:
        raise InvalidTransition("source execution still has an active lease")
    return Execution.create(
        execution_id=execution_id,
        workspace_id=source.workspace_id,
        task_id=task_id if task_id is not None else source.task_id,
        worker_item_id=source.worker_item_id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=max_attempts if max_attempts is not None else source.max_attempts,
        purpose=purpose if purpose is not None else source.purpose,
        capability_requirements=(
            capability_requirements
            if capability_requirements is not None
            else source.capability_requirements
        ),
        pinned_worker_id=_continuation_worker_id(source, source_session),
        not_before=not_before,
        source_execution_id=source.id,
        source_session_id=source_session.id,
        adapter_preferences=(
            adapter_preferences
            if adapter_preferences is not None
            else source.adapter_preferences
        ),
        adapter_forbidden=(
            adapter_forbidden
            if adapter_forbidden is not None
            else source.adapter_forbidden
        ),
    )


class ExecutionCommandHandler:
    command_types = frozenset(
        {
            "CreateExecution",
            "RetryExecution",
            "CancelExecution",
            "StopExecution",
            "ContinueExecutionSession",
            "RenewLease",
            "ReportExecutionStarted",
            "ReportExecutionSucceeded",
            "ReportExecutionFailed",
        }
    )

    def __init__(
        self, clock: Callable[[], datetime], *, lease_ttl_seconds: int = 30
    ) -> None:
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be positive")
        self._clock = clock
        self._lease_ttl = timedelta(seconds=lease_ttl_seconds)

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.command_type == "CreateExecution":
            return self._create(uow, envelope)
        if envelope.command_type == "RetryExecution":
            return self._retry(uow, envelope)
        if envelope.command_type == "RenewLease":
            return self._renew_lease(uow, envelope)
        if envelope.command_type == "ReportExecutionStarted":
            return self._report_started(uow, envelope)
        if envelope.command_type == "ReportExecutionSucceeded":
            return self._report_finished(uow, envelope, succeeded=True)
        if envelope.command_type == "ReportExecutionFailed":
            return self._report_finished(uow, envelope, succeeded=False)
        if envelope.command_type == "StopExecution":
            return self._request_stop(uow, envelope)
        if envelope.command_type == "ContinueExecutionSession":
            return self._continue_session(uow, envelope)
        return self._cancel(uow, envelope)

    def _create(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        task = uow.tasks.get(
            _required_uuid(envelope.payload, "task_id"), envelope.workspace_id
        )
        if task is None:
            raise NotFound("task not found")
        if task.status in {
            TaskStatus.PAUSED,
            TaskStatus.CANCELED,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
        }:
            raise InvalidTransition(f"task cannot receive an execution from {task.status}")

        pinned_worker_id = _optional_uuid(envelope.payload, "pinned_worker_id")
        if pinned_worker_id is not None and uow.workers.get(
            pinned_worker_id, envelope.workspace_id
        ) is None:
            raise NotFound("worker not found")
        worker_item_id = _validate_execution_worker_item(
            uow, envelope, task.id
        )

        execution_id = uuid4()
        execution = Execution.create(
            execution_id=execution_id,
            workspace_id=envelope.workspace_id,
            task_id=task.id,
            worker_item_id=worker_item_id,
            lineage_id=execution_id,
            attempt=1,
            max_attempts=_required_int(envelope.payload, "max_attempts"),
            purpose=_required_str(envelope.payload, "purpose"),
            capability_requirements=frozenset(
                _required_string_tuple(envelope.payload, "capability_requirements")
            ),
            pinned_worker_id=pinned_worker_id,
            not_before=_required_datetime(envelope.payload, "not_before"),
            adapter_preferences=_required_string_tuple(
                envelope.payload, "adapter_preferences", allow_empty=True
            ),
            adapter_forbidden=_required_string_tuple(
                envelope.payload, "adapter_forbidden", allow_empty=True
            ),
        )
        events: list[EventData] = []
        if task.status in {TaskStatus.PENDING, TaskStatus.WAITING}:
            previous_status = task.status.value
            previous_version = task.version
            task.mark_ready()
            uow.tasks.save(task, previous_version)
            events.append(
                _event(
                    envelope,
                    workspace_id=task.workspace_id,
                    aggregate_type="task",
                    aggregate_id=task.id,
                    aggregate_version=task.version,
                    event_type="TaskReady",
                    payload={
                        "status": task.status.value,
                        "previous_status": previous_status,
                    },
                )
            )

        uow.executions.add(execution)
        events.append(
            _event(
                envelope,
                workspace_id=execution.workspace_id,
                aggregate_type="execution",
                aggregate_id=execution.id,
                aggregate_version=execution.version,
                event_type="ExecutionCreated",
                payload=_execution_payload(execution),
            )
        )
        for event in events:
            uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=execution.id,
            aggregate_version=execution.version,
            event_ids=tuple(event.id for event in events),
        )

    def _request_stop(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        execution = _get_target_execution(uow, envelope, for_update=True)
        if execution.active_lease_id is None:
            raise InvalidTransition("stopping an execution requires an active lease")
        session = uow.sessions.get_active_for_execution(execution.id, for_update=True)
        if session is None:
            raise InvalidTransition("stopping an execution requires an active Session")
        previous_version = execution.version
        execution.request_stop()
        uow.executions.save(execution, previous_version)
        event = _event(
            envelope,
            workspace_id=execution.workspace_id,
            aggregate_type="execution",
            aggregate_id=execution.id,
            aggregate_version=execution.version,
            event_type="ExecutionStopRequested",
            payload={
                "execution_id": str(execution.id),
                "session_id": str(session.id),
                "lease_id": str(execution.active_lease_id),
                "fencing_token": execution.last_fencing_token,
                "command_id": str(envelope.id),
            },
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=execution.id,
            aggregate_version=execution.version,
            event_ids=(event.id,),
        )

    def _continue_session(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        session = _get_target_session(uow, envelope, for_update=True)
        if session.status is ExecutionSessionStatus.ACTIVE:
            raise InvalidTransition("only a terminal Session can be continued")
        execution = uow.executions.get(
            session.execution_id, envelope.workspace_id, for_update=True
        )
        if execution is None:
            raise NotFound("source execution not found")
        if execution.status not in TERMINAL_EXECUTION_STATUSES:
            raise InvalidTransition("source execution is not terminal")
        if execution.active_lease_id is not None:
            raise InvalidTransition("source execution still has an active lease")
        if execution.stop_requested:
            stop_operation = uow.gateway_operations.get_by_operation_id(
                gateway_operation_id(
                    execution.id, GatewayOperationType.STOP_SESSION
                ),
                envelope.workspace_id,
            )
            if (
                stop_operation is None
                or stop_operation.status is not GatewayOperationStatus.SUCCEEDED
            ):
                raise InvalidTransition(
                    "continuation is blocked until the stop operation is confirmed"
                )
        task = uow.tasks.get(execution.task_id, envelope.workspace_id)
        if task is None:
            raise NotFound("task not found")
        if task.status in {
            TaskStatus.PAUSED,
            TaskStatus.CANCELED,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
        }:
            raise InvalidTransition(f"task cannot receive a continuation from {task.status}")

        continuation = build_continuation_execution(
            execution,
            session,
            not_before=_required_datetime(envelope.payload, "not_before"),
            execution_id=uuid4(),
        )
        events: list[EventData] = []
        if task.status in {TaskStatus.PENDING, TaskStatus.WAITING}:
            previous_task_status = task.status.value
            previous_task_version = task.version
            task.mark_ready()
            uow.tasks.save(task, previous_task_version)
            events.append(
                _event(
                    envelope,
                    workspace_id=task.workspace_id,
                    aggregate_type="task",
                    aggregate_id=task.id,
                    aggregate_version=task.version,
                    event_type="TaskReady",
                    payload={
                        "status": task.status.value,
                        "previous_status": previous_task_status,
                    },
                )
            )
        uow.executions.add(continuation)
        events.append(
            _event(
                envelope,
                workspace_id=continuation.workspace_id,
                aggregate_type="execution",
                aggregate_id=continuation.id,
                aggregate_version=continuation.version,
                event_type="ExecutionContinued",
                payload=_execution_payload(continuation),
            )
        )
        for event in events:
            uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=continuation.id,
            aggregate_version=continuation.version,
            event_ids=tuple(event.id for event in events),
        )
    def _retry(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        source = _get_target_execution(uow, envelope, for_update=True)
        if source.status not in {ExecutionStatus.FAILED, ExecutionStatus.LOST}:
            raise InvalidTransition("only failed or lost executions can be retried")
        if source.attempt >= source.max_attempts:
            raise InvalidTransition("execution retry limit has been reached")
        next_attempt = source.attempt + 1
        if uow.executions.attempt_exists(source.lineage_id, next_attempt):
            raise InvalidTransition("execution retry attempt already exists")

        task = uow.tasks.get(source.task_id, envelope.workspace_id)
        if task is None:
            raise NotFound("task not found")
        execution = Execution.create(
            execution_id=uuid4(),
            workspace_id=source.workspace_id,
            task_id=source.task_id,
            worker_item_id=source.worker_item_id,
            lineage_id=source.lineage_id,
            attempt=next_attempt,
            max_attempts=source.max_attempts,
            purpose=source.purpose,
            capability_requirements=source.capability_requirements,
            pinned_worker_id=source.pinned_worker_id,
            not_before=_required_datetime(envelope.payload, "not_before"),
            adapter_preferences=source.adapter_preferences,
            adapter_forbidden=source.adapter_forbidden,
        )
        events: list[EventData] = []
        if task.status is TaskStatus.WAITING:
            previous_status = task.status.value
            previous_version = task.version
            task.mark_ready()
            uow.tasks.save(task, previous_version)
            events.append(
                _event(
                    envelope,
                    workspace_id=task.workspace_id,
                    aggregate_type="task",
                    aggregate_id=task.id,
                    aggregate_version=task.version,
                    event_type="TaskReady",
                    payload={
                        "status": task.status.value,
                        "previous_status": previous_status,
                    },
                )
            )

        uow.executions.add(execution)
        events.append(
            _event(
                envelope,
                workspace_id=execution.workspace_id,
                aggregate_type="execution",
                aggregate_id=execution.id,
                aggregate_version=execution.version,
                event_type="ExecutionRetryQueued",
                payload=_execution_payload(execution),
            )
        )
        for event in events:
            uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=execution.id,
            aggregate_version=execution.version,
            event_ids=tuple(event.id for event in events),
        )

    def _cancel(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        execution = _get_target_execution(uow, envelope, for_update=True)
        task = uow.tasks.get(execution.task_id, envelope.workspace_id)
        if task is None:
            raise NotFound("task not found")

        lease = None
        worker = None
        session = None
        lease_previous_version = 0
        worker_previous_version = 0
        session_previous_version = 0
        if execution.active_lease_id is not None:
            lease = uow.leases.get(
                execution.active_lease_id, envelope.workspace_id, for_update=True
            )
            if lease is None:
                raise NotFound("lease not found")
            worker_id = execution.assigned_worker_id or lease.worker_id
            worker = uow.workers.get(worker_id, envelope.workspace_id, for_update=True)
            if worker is None:
                raise NotFound("worker not found")
            session = uow.sessions.get_active_for_execution(
                execution.id, for_update=True
            )
            lease_previous_version = lease.version
            lease.revoke(now=self._clock())
            worker_previous_version = worker.version
            worker.release()
            if session is not None:
                session_previous_version = session.version
                session.close(lost=False, now=self._clock())

        execution_previous_version = execution.version
        previous_status = execution.status.value
        execution.cancel(now=self._clock())
        if lease is not None:
            uow.leases.save(lease, lease_previous_version)
        if worker is not None:
            uow.workers.save(worker, worker_previous_version)
        if session is not None:
            uow.sessions.save(session, session_previous_version)
        uow.executions.save(execution, execution_previous_version)

        events = [
            _event(
                envelope,
                workspace_id=execution.workspace_id,
                aggregate_type="execution",
                aggregate_id=execution.id,
                aggregate_version=execution.version,
                event_type="ExecutionCanceled",
                payload={
                    "status": execution.status.value,
                    "previous_status": previous_status,
                },
            )
        ]
        if lease is not None:
            events.append(
                _event(
                    envelope,
                    workspace_id=lease.workspace_id,
                    aggregate_type="lease",
                    aggregate_id=lease.id,
                    aggregate_version=lease.version,
                    event_type="LeaseReleased",
                    payload={
                        "status": lease.status.value,
                        "fencing_token": lease.fencing_token,
                    },
                )
            )
        if worker is not None:
            events.append(
                _event(
                    envelope,
                    workspace_id=worker.workspace_id,
                    aggregate_type="worker",
                    aggregate_id=worker.id,
                    aggregate_version=worker.version,
                    event_type="WorkerCapacityReleased",
                    payload={
                        "status": worker.status.value,
                        "desired_state": worker.desired_state.value,
                        "current_load": worker.current_load,
                        "capacity": worker.capacity,
                    },
                )
            )
        if session is not None:
            events.append(
                _event(
                    envelope,
                    workspace_id=session.workspace_id,
                    aggregate_type="execution_session",
                    aggregate_id=session.id,
                    aggregate_version=session.version,
                    event_type="ExecutionSessionClosed",
                    payload={
                        "status": session.status.value,
                        "last_sequence": session.last_sequence,
                    },
                )
            )
        if task.status in {TaskStatus.READY, TaskStatus.RUNNING} and not uow.executions.has_live_for_task(
            task.id
        ):
            previous_task_status = task.status.value
            previous_task_version = task.version
            task.wait_for_decision()
            uow.tasks.save(task, previous_task_version)
            events.append(
                _event(
                    envelope,
                    workspace_id=task.workspace_id,
                    aggregate_type="task",
                    aggregate_id=task.id,
                    aggregate_version=task.version,
                    event_type="TaskWaitingForDecision",
                    payload={
                        "status": task.status.value,
                        "previous_status": previous_task_status,
                    },
                )
            )
        for event in events:
            uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=execution.id,
            aggregate_version=execution.version,
            event_ids=tuple(event.id for event in events),
        )

    def _renew_lease(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        lease = _get_target_lease(uow, envelope, for_update=True)
        worker_id = _required_uuid(envelope.payload, "worker_id")
        fencing_token = _required_int(envelope.payload, "fencing_token")
        _assert_worker_actor(envelope, worker_id)

        now = self._clock()
        previous_version = lease.version
        old_expires_at = lease.expires_at
        lease.renew(
            worker_id=worker_id,
            fencing_token=fencing_token,
            now=now,
            ttl=self._lease_ttl,
        )
        uow.leases.save(lease, previous_version)
        event = _event(
            envelope,
            workspace_id=lease.workspace_id,
            aggregate_type="lease",
            aggregate_id=lease.id,
            aggregate_version=lease.version,
            event_type="LeaseRenewed",
            payload={
                "status": lease.status.value,
                "old_expires_at": old_expires_at.isoformat(),
                "expires_at": lease.expires_at.isoformat(),
                "fencing_token": lease.fencing_token,
            },
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=lease.id,
            aggregate_version=lease.version,
            event_ids=(event.id,),
        )

    def _report_started(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        execution = _get_target_execution(uow, envelope, for_update=True)
        worker_id = _required_uuid(envelope.payload, "worker_id")
        lease_id = _required_uuid(envelope.payload, "lease_id")
        fencing_token = _required_int(envelope.payload, "fencing_token")
        _assert_worker_actor(envelope, worker_id)

        lease = uow.leases.get(lease_id, envelope.workspace_id, for_update=True)
        if lease is None:
            raise NotFound("lease not found")
        now = self._clock()
        _assert_execution_lease(execution, lease, worker_id, fencing_token, now)
        worker = uow.workers.get(worker_id, envelope.workspace_id, for_update=True)
        if worker is None:
            raise NotFound("worker not found")
        task = uow.tasks.get(execution.task_id, envelope.workspace_id)
        if task is None:
            raise NotFound("task not found")
        if uow.sessions.get_active_for_execution(execution.id, for_update=True) is not None:
            raise InvalidTransition("execution already has an active session")

        previous_execution_version = execution.version
        previous_worker_version = worker.version
        previous_task_version = task.version
        execution.start(
            lease_id=lease.id,
            fencing_token=fencing_token,
            now=now,
        )
        worker.mark_running()
        session = ExecutionSession.start(
            session_id=uuid4(),
            workspace_id=execution.workspace_id,
            execution_id=execution.id,
            worker_id=worker.id,
            adapter_session_id=_required_str(
                envelope.payload, "adapter_session_id"
            ),
            now=now,
        )
        task_changed = task.status is TaskStatus.READY
        if task_changed:
            task.mark_running()

        uow.executions.save(execution, previous_execution_version)
        uow.workers.save(worker, previous_worker_version)
        uow.sessions.add(session)
        if task_changed:
            uow.tasks.save(task, previous_task_version)

        events = [
            _event(
                envelope,
                workspace_id=execution.workspace_id,
                aggregate_type="execution",
                aggregate_id=execution.id,
                aggregate_version=execution.version,
                event_type="ExecutionStarted",
                payload={
                    "status": execution.status.value,
                    "lease_id": str(lease.id),
                    "worker_id": str(worker.id),
                    "fencing_token": fencing_token,
                },
            ),
            _event(
                envelope,
                workspace_id=session.workspace_id,
                aggregate_type="execution_session",
                aggregate_id=session.id,
                aggregate_version=session.version,
                event_type="ExecutionSessionStarted",
                payload={
                    "status": session.status.value,
                    "adapter_session_id": session.adapter_session_id,
                    "input_mode": session.input_mode,
                    "last_sequence": session.last_sequence,
                },
            ),
            _event(
                envelope,
                workspace_id=worker.workspace_id,
                aggregate_type="worker",
                aggregate_id=worker.id,
                aggregate_version=worker.version,
                event_type="WorkerRunning",
                payload={
                    "status": worker.status.value,
                    "desired_state": worker.desired_state.value,
                    "current_load": worker.current_load,
                    "capacity": worker.capacity,
                },
            ),
        ]
        if task_changed:
            events.append(
                _event(
                    envelope,
                    workspace_id=task.workspace_id,
                    aggregate_type="task",
                    aggregate_id=task.id,
                    aggregate_version=task.version,
                    event_type="TaskRunning",
                    payload={"status": task.status.value},
                )
            )
        for event in events:
            uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=execution.id,
            aggregate_version=execution.version,
            event_ids=tuple(event.id for event in events),
        )

    def _report_finished(
        self,
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
        *,
        succeeded: bool,
    ) -> CommandResult:
        execution = _get_target_execution(uow, envelope, for_update=True)
        worker_id = _required_uuid(envelope.payload, "worker_id")
        lease_id = _required_uuid(envelope.payload, "lease_id")
        fencing_token = _required_int(envelope.payload, "fencing_token")
        _assert_worker_actor(envelope, worker_id)

        lease = uow.leases.get(lease_id, envelope.workspace_id, for_update=True)
        if lease is None:
            raise NotFound("lease not found")
        now = self._clock()
        _assert_execution_lease(execution, lease, worker_id, fencing_token, now)
        worker = uow.workers.get(worker_id, envelope.workspace_id, for_update=True)
        if worker is None:
            raise NotFound("worker not found")
        task = uow.tasks.get(execution.task_id, envelope.workspace_id)
        if task is None:
            raise NotFound("task not found")
        session = uow.sessions.get_active_for_execution(execution.id, for_update=True)
        if session is None:
            raise NotFound("active execution session not found")

        previous_execution_version = execution.version
        previous_lease_version = lease.version
        previous_worker_version = worker.version
        previous_session_version = session.version
        previous_task_version = task.version
        if succeeded:
            execution.succeed(
                lease_id=lease.id,
                fencing_token=fencing_token,
                now=now,
            )
        else:
            execution.fail(
                lease_id=lease.id,
                fencing_token=fencing_token,
                failure_code=_required_str(envelope.payload, "failure_code"),
                now=now,
            )
        lease.release(
            worker_id=worker_id,
            fencing_token=fencing_token,
            now=now,
        )
        worker.release()
        session.close(lost=False, now=now)

        uow.executions.save(execution, previous_execution_version)
        uow.leases.save(lease, previous_lease_version)
        uow.workers.save(worker, previous_worker_version)
        uow.sessions.save(session, previous_session_version)

        event_type = "ExecutionSucceeded" if succeeded else "ExecutionFailed"
        execution_payload: dict[str, object] = {
            "status": execution.status.value,
            "failure_code": execution.failure_code,
        }
        events = [
            _event(
                envelope,
                workspace_id=execution.workspace_id,
                aggregate_type="execution",
                aggregate_id=execution.id,
                aggregate_version=execution.version,
                event_type=event_type,
                payload=execution_payload,
            ),
            _event(
                envelope,
                workspace_id=lease.workspace_id,
                aggregate_type="lease",
                aggregate_id=lease.id,
                aggregate_version=lease.version,
                event_type="LeaseReleased",
                payload={
                    "status": lease.status.value,
                    "fencing_token": lease.fencing_token,
                },
            ),
            _event(
                envelope,
                workspace_id=worker.workspace_id,
                aggregate_type="worker",
                aggregate_id=worker.id,
                aggregate_version=worker.version,
                event_type="WorkerCapacityReleased",
                payload={
                    "status": worker.status.value,
                    "desired_state": worker.desired_state.value,
                    "current_load": worker.current_load,
                    "capacity": worker.capacity,
                },
            ),
            _event(
                envelope,
                workspace_id=session.workspace_id,
                aggregate_type="execution_session",
                aggregate_id=session.id,
                aggregate_version=session.version,
                event_type="ExecutionSessionClosed",
                payload={
                    "status": session.status.value,
                    "last_sequence": session.last_sequence,
                },
            ),
        ]
        if task.status in {
            TaskStatus.READY,
            TaskStatus.RUNNING,
        } and not uow.executions.has_live_for_task(task.id):
            previous_task_status = task.status.value
            task.wait_for_decision()
            uow.tasks.save(task, previous_task_version)
            events.append(
                _event(
                    envelope,
                    workspace_id=task.workspace_id,
                    aggregate_type="task",
                    aggregate_id=task.id,
                    aggregate_version=task.version,
                    event_type="TaskWaitingForDecision",
                    payload={
                        "status": task.status.value,
                        "previous_status": previous_task_status,
                    },
                )
            )
        for event in events:
            uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=execution.id,
            aggregate_version=execution.version,
            event_ids=tuple(event.id for event in events),
        )


class ExecutionInputCommandHandler:
    command_types = frozenset(
        {
            "SendExecutionInput",
            "UpdatePendingExecutionInput",
            "CancelExecutionInput",
            "RetryBlockedExecutionInput",
        }
    )

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.command_type == "SendExecutionInput":
            return self._send(uow, envelope)
        session = self._load_session(uow, envelope)
        item = self._load_input(uow, envelope, session.id)
        previous_version = item.version
        if envelope.command_type == "UpdatePendingExecutionInput":
            item.update_text(_required_str(envelope.payload, "text"))
            event_type = "ExecutionInputUpdated"
        elif envelope.command_type == "CancelExecutionInput":
            item.cancel(now=self._clock())
            event_type = "ExecutionInputCanceled"
        else:
            item.retry_blocked()
            event_type = "ExecutionInputRetryQueued"
        uow.execution_inputs.save(item, previous_version)
        event = _input_event(envelope, item, event_type)
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=item.id,
            aggregate_version=item.version,
            event_ids=(event.id,),
        )

    def _send(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        context = _required_context(envelope.payload)
        context_session_id = cast(UUID, context["session_id"])
        context_worker_item_id = cast(UUID, context["worker_item_id"])
        _validate_input_target(envelope, context_session_id)

        # Discover the Session without a lock, then keep the execution/root-task
        # lock order aligned with plan publication before revalidating the Session.
        assert envelope.target_id is not None
        session_probe = uow.sessions.get_any(
            envelope.target_id, envelope.workspace_id, for_update=False
        )
        if session_probe is None:
            raise NotFound("execution session not found")
        # Keep intervention revalidation deterministic: WorkerItem -> Execution
        # -> active Session -> Binding -> runtime Worker.
        worker_item = uow.worker_items.get(
            context_worker_item_id, envelope.workspace_id, for_update=True
        )
        execution = uow.executions.get(
            session_probe.execution_id, envelope.workspace_id, for_update=True
        )
        if execution is None:
            raise NotFound("execution not found")
        current_worker_item = worker_item
        if execution.worker_item_id is not None and (
            current_worker_item is None
            or current_worker_item.id != execution.worker_item_id
        ):
            current_worker_item = uow.worker_items.get(
                execution.worker_item_id, envelope.workspace_id
            )
        snapshot = uow.plans.get_current_for_task(
            execution.task_id, envelope.workspace_id
        )
        if snapshot is not None:
            snapshot = uow.plans.get_current_for_root(
                snapshot.root_task_id, envelope.workspace_id
            )
        session = uow.sessions.get_any(
            session_probe.id, envelope.workspace_id, for_update=True
        )
        if session is None:
            raise NotFound("execution session not found")
        if session.status is not ExecutionSessionStatus.ACTIVE:
            raise InvalidTransition("input requires an active Session")
        binding = uow.bindings.get_for_execution(
            execution.id, envelope.workspace_id, for_update=True
        )
        current_worker_id = (
            binding.worker_id
            if binding is not None
            else execution.assigned_worker_id or session.worker_id
        )
        worker = uow.workers.get(
            current_worker_id, envelope.workspace_id, for_update=True
        )
        changed_fields, current = _execution_context_mismatches(
            context=context,
            snapshot=snapshot,
            placement=(
                uow.worker_items.get_current_placement(
                    current_worker_item.id, envelope.workspace_id
                )
                if current_worker_item is not None
                else None
            ),
            worker_item=current_worker_item,
            execution=execution,
            session=session,
            binding_worker_id=binding.worker_id if binding is not None else None,
            worker_loaded=worker is not None,
        )
        if changed_fields:
            raise ExecutionContextChanged(changed_fields, current=current)
        if worker_item is None:
            raise ExecutionContextChanged(("worker_item_id",), current=current)
        if worker_item.status is not WorkerItemStatus.ACTIVE:
            raise InvalidTransition("WorkerItem is not active")
        if not worker_item.context_is_synchronized:
            raise InvalidTransition("WorkerItem context is not acknowledged")

        client_key = _required_str(envelope.payload, "client_idempotency_key")
        text = _required_str(envelope.payload, "text")
        attachment_refs = _required_string_tuple(
            envelope.payload, "attachment_refs", allow_empty=True
        )
        existing = uow.execution_inputs.find_by_client_idempotency_key(
            session.id, envelope.workspace_id, client_key, for_update=True
        )
        if existing is not None:
            if existing.text != text or existing.attachment_refs != attachment_refs:
                raise ValidationError(
                    "client idempotency key was reused with different input"
                )
            return CommandResult(
                command_id=envelope.id,
                aggregate_id=existing.id,
                aggregate_version=existing.version,
                event_ids=(),
            )
        if uow.execution_inputs.get_pending_for_session(
            session.id, envelope.workspace_id, for_update=True
        ) is not None:
            raise InvalidTransition("the Session already has a pending input")
        item = ExecutionInput.accept(
            id=uuid4(),
            workspace_id=envelope.workspace_id,
            execution_id=session.execution_id,
            session_id=session.id,
            client_idempotency_key=client_key,
            text=text,
            attachment_refs=attachment_refs,
            now=self._clock(),
            source_command_id=envelope.id,
        )
        item.queue()
        uow.execution_inputs.add(item)
        event = _input_event(
            envelope,
            item,
            "ExecutionInputAccepted",
            context=_context_payload(context),
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=item.id,
            aggregate_version=item.version,
            event_ids=(event.id,),
        )

    @staticmethod
    def _load_session(
        uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> ExecutionSession:
        if envelope.target_type != "execution_session" or envelope.target_id is None:
            raise ValidationError("input commands require an execution_session target")
        if envelope.expected_version is None or envelope.expected_version < 1:
            raise ValidationError("input commands require a positive expected_version")
        session = uow.sessions.get(
            envelope.target_id, envelope.workspace_id, for_update=True
        )
        if session is None:
            raise NotFound("active execution session not found")
        if session.version != envelope.expected_version:
            raise VersionConflict(envelope.expected_version, session.version)
        return session

    @staticmethod
    def _load_input(
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
        session_id: UUID,
    ) -> ExecutionInput:
        client_key = _required_str(envelope.payload, "client_idempotency_key")
        item = uow.execution_inputs.find_by_client_idempotency_key(
            session_id, envelope.workspace_id, client_key, for_update=True
        )
        if item is None:
            raise NotFound("execution input not found")
        execution_id = _required_uuid(envelope.payload, "execution_id")
        if item.execution_id != execution_id:
            raise ValidationError("input execution_id does not match the input")
        return item


def _get_target_execution(
    uow: ControlPlaneUnitOfWork,
    envelope: CommandEnvelope,
    *,
    for_update: bool,
) -> Execution:
    if envelope.target_id is None or envelope.expected_version is None:
        raise ValidationError(
            "mutation commands require target_id and expected_version"
        )
    if envelope.expected_version < 1:
        raise ValidationError("expected_version must be positive")
    execution = uow.executions.get(
        envelope.target_id, envelope.workspace_id, for_update=for_update
    )
    if execution is None:
        raise NotFound("execution not found")
    if execution.version != envelope.expected_version:
        raise VersionConflict(envelope.expected_version, execution.version)
    return execution


def _validate_execution_worker_item(
    uow: ControlPlaneUnitOfWork,
    envelope: CommandEnvelope,
    task_id: UUID,
) -> UUID | None:
    worker_item_id = _optional_uuid(envelope.payload, "worker_item_id")
    if worker_item_id is not None and uow.worker_items.get(
        worker_item_id, envelope.workspace_id
    ) is None:
        raise NotFound("worker item not found")

    snapshot = uow.plans.get_current_for_task(task_id, envelope.workspace_id)
    if snapshot is None or snapshot.schema_version != 2:
        return worker_item_id
    canonical = PlanNormalizer().normalize(snapshot)
    if not any(subtask.task_id == task_id for subtask in canonical.subtasks):
        return worker_item_id
    if worker_item_id is None:
        raise ValidationError(
            "worker_item_id is required for a Task First v2 Subtask"
        )
    worker_item = uow.worker_items.get(
        worker_item_id, envelope.workspace_id, for_update=True
    )
    if worker_item is None:
        raise NotFound("worker item not found")
    if worker_item.status is not WorkerItemStatus.ACTIVE:
        raise ValidationError("WorkerItem must be active")
    if worker_item.root_task_id != snapshot.root_task_id:
        raise ValidationError("WorkerItem root task does not match the plan")
    placement = uow.worker_items.get_current_placement(
        worker_item_id, envelope.workspace_id
    )
    if placement is None:
        raise ValidationError("WorkerItem has no current plan placement")
    if placement.plan_snapshot_id != snapshot.id:
        raise ValidationError("WorkerItem placement is not in the current plan")
    if placement.workspace_id != envelope.workspace_id:
        raise ValidationError("WorkerItem placement workspace does not match")
    if placement.root_task_id != snapshot.root_task_id:
        raise ValidationError("WorkerItem placement root task does not match")
    if placement.worker_item_id != worker_item.id:
        raise ValidationError("WorkerItem placement identity does not match")
    if placement.subtask_task_id != task_id:
        raise ValidationError("WorkerItem placement Task does not match execution")
    return worker_item_id


def _get_target_session(
    uow: ControlPlaneUnitOfWork,
    envelope: CommandEnvelope,
    *,
    for_update: bool,
) -> ExecutionSession:
    if envelope.target_type != "execution_session":
        raise ValidationError("session commands require target_type=execution_session")
    if envelope.target_id is None or envelope.expected_version is None:
        raise ValidationError(
            "session mutation commands require target_id and expected_version"
        )
    if envelope.expected_version < 1:
        raise ValidationError("expected_version must be positive")
    session = uow.sessions.get_any(
        envelope.target_id, envelope.workspace_id, for_update=for_update
    )
    if session is None:
        raise NotFound("execution session not found")
    if session.version != envelope.expected_version:
        raise VersionConflict(envelope.expected_version, session.version)
    return session


def _get_target_lease(
    uow: ControlPlaneUnitOfWork,
    envelope: CommandEnvelope,
    *,
    for_update: bool,
) -> AssignmentLease:
    if envelope.target_id is None or envelope.expected_version is None:
        raise ValidationError(
            "mutation commands require target_id and expected_version"
        )
    if envelope.expected_version < 1:
        raise ValidationError("expected_version must be positive")
    lease = uow.leases.get(
        envelope.target_id, envelope.workspace_id, for_update=for_update
    )
    if lease is None:
        raise NotFound("lease not found")
    if lease.version != envelope.expected_version:
        raise VersionConflict(envelope.expected_version, lease.version)
    return lease


def _assert_worker_actor(envelope: CommandEnvelope, worker_id: UUID) -> None:
    if envelope.actor_type != "worker" or envelope.actor_id != str(worker_id):
        raise ValidationError("worker command actor must match worker_id")


def _assert_execution_lease(
    execution: Execution,
    lease: AssignmentLease,
    worker_id: UUID,
    fencing_token: int,
    now: datetime,
) -> None:
    if lease.execution_id != execution.id:
        raise StaleFencingToken("lease does not belong to execution")
    lease.assert_owner(worker_id, fencing_token, now)
    execution.assert_fence(lease_id=lease.id, fencing_token=fencing_token)


def _event(
    envelope: CommandEnvelope,
    *,
    workspace_id: UUID,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    event_type: str,
    payload: dict[str, Any],
) -> EventData:
    return EventData(
        id=uuid4(),
        workspace_id=workspace_id,
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


def _input_event(
    envelope: CommandEnvelope,
    item: ExecutionInput,
    event_type: str,
    *,
    context: Mapping[str, object] | None = None,
) -> EventData:
    payload: dict[str, Any] = {
        "execution_id": str(item.execution_id),
        "session_id": str(item.session_id),
        "client_idempotency_key": item.client_idempotency_key,
        "status": item.status.value,
        "text_length": len(item.text),
        "attachment_count": len(item.attachment_refs),
        "failure_code": item.failure_code,
    }
    if context is not None:
        payload["execution_context"] = dict(context)
    return _event(
        envelope,
        workspace_id=item.workspace_id,
        aggregate_type="execution_input",
        aggregate_id=item.id,
        aggregate_version=item.version,
        event_type=event_type,
        payload=payload,
    )


def _execution_payload(execution: Execution) -> dict[str, object]:
    return {
        "task_id": str(execution.task_id),
        "worker_item_id": (
            str(execution.worker_item_id)
            if execution.worker_item_id is not None
            else None
        ),
        "lineage_id": str(execution.lineage_id),
        "attempt": execution.attempt,
        "max_attempts": execution.max_attempts,
        "purpose": execution.purpose,
        "status": execution.status.value,
        "stop_requested": execution.stop_requested,
        "capability_requirements": sorted(execution.capability_requirements),
        "pinned_worker_id": (
            str(execution.pinned_worker_id)
            if execution.pinned_worker_id is not None
            else None
        ),
        "not_before": execution.not_before.isoformat(),
        "source_execution_id": (
            str(execution.source_execution_id)
            if execution.source_execution_id is not None
            else None
        ),
        "source_session_id": (
            str(execution.source_session_id)
            if execution.source_session_id is not None
            else None
        ),
    }


def _required_str(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    return value


def _required_context(payload: dict[str, Any]) -> dict[str, UUID | int]:
    raw_context = payload.get("context")
    if not isinstance(raw_context, Mapping):
        raise ValidationError("context is required for SendExecutionInput")
    context = cast(Mapping[str, Any], raw_context)
    return {
        "worker_item_id": _required_uuid(context, "worker_item_id"),
        "plan_generation": _required_positive_int(context, "plan_generation"),
        "context_version": _required_positive_int(context, "context_version"),
        "execution_id": _required_uuid(context, "execution_id"),
        "session_id": _required_uuid(context, "session_id"),
        "session_version": _required_positive_int(context, "session_version"),
        "worker_id": _required_uuid(context, "worker_id"),
    }


def _context_payload(context: Mapping[str, UUID | int]) -> dict[str, str]:
    return {key: str(value) for key, value in context.items()}


def _validate_input_target(
    envelope: CommandEnvelope, context_session_id: UUID | str
) -> None:
    if envelope.target_type != "execution_session" or envelope.target_id is None:
        raise ValidationError("input commands require an execution_session target")
    if envelope.expected_version is None or envelope.expected_version < 1:
        raise ValidationError("input commands require a positive expected_version")
    if context_session_id != envelope.target_id:
        raise ExecutionContextChanged(
            ("session_id",),
            current={"session_id": str(envelope.target_id)},
        )


def _execution_context_mismatches(
    *,
    context: Mapping[str, UUID | int],
    snapshot: TaskPlanSnapshot | None,
    placement: WorkerItemPlacement | None,
    worker_item: WorkerItem | None,
    execution: Execution,
    session: ExecutionSession,
    binding_worker_id: UUID | None,
    worker_loaded: bool,
) -> tuple[tuple[str, ...], dict[str, object]]:
    worker_ids = {
        worker_id
        for worker_id in (
            execution.assigned_worker_id,
            session.worker_id,
            binding_worker_id,
        )
        if worker_id is not None
    }
    current_worker_id = next(iter(worker_ids), None) if len(worker_ids) == 1 else None
    current = {
        "worker_item_id": (
            str(execution.worker_item_id)
            if execution.worker_item_id is not None
            else None
        ),
        "plan_generation": placement.plan_generation if placement is not None else (
            snapshot.generation if snapshot is not None else None
        ),
        "context_version": worker_item.context_version if worker_item is not None else None,
        "worker_id": str(current_worker_id) if current_worker_id is not None else None,
        "execution_id": str(execution.id),
        "session_id": str(session.id),
        "session_version": session.version,
    }
    changed: set[str] = set()
    if (
        worker_item is None
        or execution.worker_item_id != context["worker_item_id"]
    ):
        changed.add("worker_item_id")
    if (
        placement is None
        or context["plan_generation"] != placement.plan_generation
    ):
        changed.add("plan_generation")
    if (
        worker_item is None
        or context["context_version"] != worker_item.context_version
    ):
        changed.add("context_version")
    if (
        len(worker_ids) != 1
        or current_worker_id != context["worker_id"]
        or not worker_loaded
    ):
        changed.add("worker_id")
    if context["execution_id"] != execution.id or session.execution_id != execution.id:
        changed.add("execution_id")
    if context["session_id"] != session.id:
        changed.add("session_id")
    if context["session_version"] != session.version:
        changed.add("session_version")
    return tuple(field for field in _CONTEXT_FIELDS if field in changed), current


_CONTEXT_FIELDS = (
    "worker_item_id",
    "plan_generation",
    "context_version",
    "execution_id",
    "session_id",
    "session_version",
    "worker_id",
)


def _required_int(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    return value


def _required_positive_int(payload: Mapping[str, Any], name: str) -> int:
    value = _required_int(payload, name)
    if value < 1:
        raise ValidationError(f"{name} must be positive")
    return value


def _required_string_tuple(
    payload: Mapping[str, Any], name: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    value = payload.get(name)
    if value is None and allow_empty:
        return ()
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValidationError(f"{name} must be a list of strings")
    return tuple(value)


def _required_uuid(payload: Mapping[str, Any], name: str) -> UUID:
    value = payload.get(name)
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationError(f"{name} must be a UUID") from exc


def _optional_uuid(payload: Mapping[str, Any], name: str) -> UUID | None:
    value = payload.get(name)
    if value is None:
        return None
    return _required_uuid(payload, name)


def _required_datetime(payload: Mapping[str, Any], name: str) -> datetime:
    value = payload.get(name)
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{name} must be an ISO datetime") from exc
    else:
        raise ValidationError(f"{name} must be an ISO datetime")
    if result.tzinfo is None:
        raise ValidationError(f"{name} must be timezone-aware")
    return result.astimezone(UTC)
