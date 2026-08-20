from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import (
    InvalidTransition,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.domain.task import TaskStatus


class ExecutionCommandHandler:
    command_types = frozenset(
        {"CreateExecution", "RetryExecution", "CancelExecution"}
    )

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.command_type == "CreateExecution":
            return self._create(uow, envelope)
        if envelope.command_type == "RetryExecution":
            return self._retry(uow, envelope)
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

        execution_id = uuid4()
        execution = Execution.create(
            execution_id=execution_id,
            workspace_id=envelope.workspace_id,
            task_id=task.id,
            lineage_id=execution_id,
            attempt=1,
            max_attempts=_required_int(envelope.payload, "max_attempts"),
            purpose=_required_str(envelope.payload, "purpose"),
            capability_requirements=frozenset(
                _required_string_tuple(envelope.payload, "capability_requirements")
            ),
            pinned_worker_id=pinned_worker_id,
            not_before=_required_datetime(envelope.payload, "not_before"),
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
            lineage_id=source.lineage_id,
            attempt=next_attempt,
            max_attempts=source.max_attempts,
            purpose=source.purpose,
            capability_requirements=source.capability_requirements,
            pinned_worker_id=source.pinned_worker_id,
            not_before=_required_datetime(envelope.payload, "not_before"),
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


def _execution_payload(execution: Execution) -> dict[str, object]:
    return {
        "task_id": str(execution.task_id),
        "lineage_id": str(execution.lineage_id),
        "attempt": execution.attempt,
        "max_attempts": execution.max_attempts,
        "purpose": execution.purpose,
        "status": execution.status.value,
        "capability_requirements": sorted(execution.capability_requirements),
        "pinned_worker_id": (
            str(execution.pinned_worker_id)
            if execution.pinned_worker_id is not None
            else None
        ),
        "not_before": execution.not_before.isoformat(),
    }


def _required_str(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    return value


def _required_int(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    return value


def _required_string_tuple(payload: dict[str, Any], name: str) -> tuple[str, ...]:
    value = payload.get(name)
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValidationError(f"{name} must be a list of strings")
    return tuple(value)


def _required_uuid(payload: dict[str, Any], name: str) -> UUID:
    value = payload.get(name)
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationError(f"{name} must be a UUID") from exc


def _optional_uuid(payload: dict[str, Any], name: str) -> UUID | None:
    value = payload.get(name)
    if value is None:
        return None
    return _required_uuid(payload, name)


def _required_datetime(payload: dict[str, Any], name: str) -> datetime:
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
