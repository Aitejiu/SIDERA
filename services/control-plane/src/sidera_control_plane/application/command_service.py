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
from sidera_control_plane.domain.commands import (
    CommandEnvelope,
    CommandResult,
    StoredCommand,
)
from sidera_control_plane.domain.errors import (
    DomainError,
    InvalidTransition,
    ValidationError,
    VersionConflict,
)
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
        rejection: DomainError | None = None
        result: CommandResult | None = None
        with self._uow_factory() as uow:
            stored = uow.commands.find_duplicate(
                envelope.workspace_id,
                envelope.actor_type,
                envelope.actor_id,
                envelope.idempotency_key,
            )
            if stored is not None:
                legacy_replay = (
                    stored.target_type == envelope.target_type
                    and stored.request_hash == _legacy_hash_request(envelope)
                )
                if stored.request_hash != request_hash and not legacy_replay:
                    raise IdempotencyConflict(
                        "idempotency key was already used for a different request"
                    )
                return _replay(stored)

            try:
                result = self._dispatch(uow, envelope, request_hash)
            except (InvalidTransition, VersionConflict) as exc:
                uow.commands.record_rejected(
                    envelope,
                    request_hash,
                    exc.code,
                    _error_document(exc),
                )
                rejection = exc

        if rejection is not None:
            raise rejection
        if result is None:
            raise RuntimeError("command completed without a result")
        return result

    def _dispatch(
        self,
        uow: SqlAlchemyUnitOfWork,
        envelope: CommandEnvelope,
        request_hash: str,
    ) -> CommandResult:
        if envelope.command_type == "CreateTask":
            return self._create_task(uow, envelope, request_hash)
        if envelope.command_type in {
            "SetTaskPriority",
            "PauseTask",
            "ResumeTask",
            "CancelTask",
        }:
            return self._mutate_task(uow, envelope, request_hash)
        raise UnsupportedCommand(f"unsupported command: {envelope.command_type}")

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

    @staticmethod
    def _mutate_task(
        uow: SqlAlchemyUnitOfWork,
        envelope: CommandEnvelope,
        request_hash: str,
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
            else:
                task.cancel()
                event_type = "TaskCanceled"
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
        uow.commands.record_applied(envelope, request_hash, result)
        uow.events.append_with_outbox(event)
        return result


def hash_request(envelope: CommandEnvelope) -> str:
    return _hash_request(envelope, include_target_type=True)


def _legacy_hash_request(envelope: CommandEnvelope) -> str:
    return _hash_request(envelope, include_target_type=False)


def _hash_request(envelope: CommandEnvelope, *, include_target_type: bool) -> str:
    canonical: dict[str, Any] = {
        "command_type": envelope.command_type,
        "target_id": str(envelope.target_id) if envelope.target_id else None,
        "expected_version": envelope.expected_version,
        "payload": envelope.payload,
    }
    if include_target_type:
        canonical["target_type"] = envelope.target_type
    encoded = json.dumps(
        canonical,
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


def _replay(stored: StoredCommand) -> CommandResult:
    if stored.status == "applied":
        return _result_from_document(stored.result)
    if stored.status != "rejected" or stored.rejection_reason is None:
        raise RuntimeError("stored command status is invalid")

    message = str(stored.result.get("message", "stored command was rejected"))
    details = stored.result.get("details")
    if not isinstance(details, dict):
        raise TypeError("stored command rejection details are invalid")
    if stored.rejection_reason == VersionConflict.code:
        try:
            expected = int(details["expected"])
            actual = int(details["actual"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("stored version conflict is invalid") from exc
        raise VersionConflict(expected, actual)
    if stored.rejection_reason == InvalidTransition.code:
        raise InvalidTransition(message)
    raise RuntimeError("stored command rejection reason is invalid")


def _error_document(error: DomainError) -> dict[str, Any]:
    details: dict[str, Any] = {}
    if isinstance(error, VersionConflict):
        details = {"expected": error.expected, "actual": error.actual}
    return {"code": error.code, "message": str(error), "details": details}


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
