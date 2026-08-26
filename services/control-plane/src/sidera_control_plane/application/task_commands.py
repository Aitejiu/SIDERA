from typing import Any
from uuid import uuid4

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.task import Task


class TaskCommandHandler:
    command_types = frozenset(
        {
            "CreateTask",
            "SetTaskPriority",
            "PauseTask",
            "ResumeTask",
            "CancelTask",
            "CompleteTask",
        }
    )

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.command_type == "CreateTask":
            return self._create_task(uow, envelope)
        return self._mutate_task(uow, envelope)

    @staticmethod
    def _create_task(
        uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if not uow.tasks.workspace_exists(envelope.workspace_id):
            raise NotFound("workspace not found")

        task = Task.create(
            task_id=uuid4(),
            workspace_id=envelope.workspace_id,
            title=_required_str(envelope.payload, "title"),
            description=_required_str(envelope.payload, "description"),
            acceptance_criteria=_required_string_tuple(
                envelope.payload, "acceptance_criteria"
            ),
            priority=_required_int(envelope.payload, "priority"),
            requested_by=envelope.actor_id,
        )
        event = EventData(
            id=uuid4(),
            workspace_id=task.workspace_id,
            aggregate_type="task",
            aggregate_id=task.id,
            aggregate_version=task.version,
            event_type="TaskCreated",
            payload={
                "title": task.title,
                "description": task.description,
                "acceptance_criteria": list(task.acceptance_criteria),
                "status": task.status.value,
                "priority": task.priority,
                "requested_by": task.requested_by,
            },
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )
        result = CommandResult(
            command_id=envelope.id,
            aggregate_id=task.id,
            aggregate_version=task.version,
            event_ids=(event.id,),
        )
        uow.tasks.add(task)
        uow.events.append_with_outbox(event)
        return result

    @staticmethod
    def _mutate_task(
        uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.target_id is None or envelope.expected_version is None:
            raise ValidationError(
                "mutation commands require target_id and expected_version"
            )
        if envelope.expected_version < 1:
            raise ValidationError("expected_version must be positive")

        task = uow.tasks.get(envelope.target_id, envelope.workspace_id)
        if task is None:
            raise NotFound("task not found")
        if task.version != envelope.expected_version:
            raise VersionConflict(envelope.expected_version, task.version)

        previous_version = task.version
        event_type: str
        event_payload: dict[str, Any]
        if envelope.command_type == "SetTaskPriority":
            previous_priority = task.priority
            task.set_priority(_required_int(envelope.payload, "priority"))
            event_type = "TaskPriorityChanged"
            event_payload = {
                "priority": task.priority,
                "previous_priority": previous_priority,
            }
        else:
            previous_status = task.status.value
            if envelope.command_type == "PauseTask":
                task.pause()
                event_type = "TaskPaused"
            elif envelope.command_type == "ResumeTask":
                task.resume()
                event_type = "TaskResumed"
            elif envelope.command_type == "CancelTask":
                task.cancel()
                event_type = "TaskCanceled"
            else:
                task.complete()
                event_type = "TaskCompleted"
            event_payload = {
                "status": task.status.value,
                "previous_status": previous_status,
            }

        uow.tasks.save(task, previous_version)
        event = EventData(
            id=uuid4(),
            workspace_id=task.workspace_id,
            aggregate_type="task",
            aggregate_id=task.id,
            aggregate_version=task.version,
            event_type=event_type,
            payload=event_payload,
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )
        result = CommandResult(
            command_id=envelope.id,
            aggregate_id=task.id,
            aggregate_version=task.version,
            event_ids=(event.id,),
        )
        uow.events.append_with_outbox(event)
        return result


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
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"{name} must be a list of strings")
    return tuple(value)
