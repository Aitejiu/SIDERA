import hashlib
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from sidera_control_plane.application.errors import (
    IdempotencyConflict,
    NotFound,
    UnsupportedCommand,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.task import Task
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


class TaskCommandService:
    def __init__(
        self, uow_factory: Callable[[], SqlAlchemyUnitOfWork]
    ) -> None:
        self._uow_factory = uow_factory

    def handle(self, envelope: CommandEnvelope) -> CommandResult:
        request_hash = hash_request(envelope)
        with self._uow_factory() as uow:
            stored = uow.commands.find_duplicate(
                envelope.workspace_id,
                envelope.actor_type,
                envelope.actor_id,
                envelope.idempotency_key,
            )
            if stored is not None:
                if stored.request_hash != request_hash:
                    raise IdempotencyConflict(
                        "idempotency key was already used for a different request"
                    )
                return _result_from_document(stored.result)

            if envelope.command_type != "CreateTask":
                raise UnsupportedCommand(
                    f"unsupported command: {envelope.command_type}"
                )
            return self._create_task(uow, envelope, request_hash)

    @staticmethod
    def _create_task(
        uow: SqlAlchemyUnitOfWork,
        envelope: CommandEnvelope,
        request_hash: str,
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
        uow.commands.record_applied(envelope, request_hash, result)
        uow.events.append_with_outbox(event)
        return result


def hash_request(envelope: CommandEnvelope) -> str:
    encoded = json.dumps(
        {
            "command_type": envelope.command_type,
            "target_id": str(envelope.target_id) if envelope.target_id else None,
            "expected_version": envelope.expected_version,
            "payload": envelope.payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _result_from_document(document: dict[str, Any]) -> CommandResult:
    try:
        return CommandResult(
            command_id=UUID(str(document["command_id"])),
            aggregate_id=UUID(str(document["aggregate_id"])),
            aggregate_version=int(document["aggregate_version"]),
            event_ids=tuple(UUID(str(item)) for item in document["event_ids"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("stored command result is invalid") from exc


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
