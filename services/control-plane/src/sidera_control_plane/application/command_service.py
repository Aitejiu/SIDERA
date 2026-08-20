import hashlib
import json
from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

from sidera_control_plane.application.errors import (
    IdempotencyConflict,
    UnsupportedCommand,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import (
    CommandEnvelope,
    CommandResult,
    StoredCommand,
)
from sidera_control_plane.domain.errors import DomainError, ReplayedDomainError


class CommandHandler(Protocol):
    command_types: frozenset[str]

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        raise NotImplementedError


class CommandService:
    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        handlers: tuple[CommandHandler, ...],
    ) -> None:
        self._uow_factory = uow_factory
        self._handlers: dict[str, CommandHandler] = {}
        for handler in handlers:
            for command_type in handler.command_types:
                if command_type in self._handlers:
                    raise ValueError(f"duplicate command handler: {command_type}")
                self._handlers[command_type] = handler

    def handle(self, envelope: CommandEnvelope) -> CommandResult:
        handler = self._handlers.get(envelope.command_type)
        if handler is None:
            raise UnsupportedCommand(f"unsupported command: {envelope.command_type}")

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
                if not _request_hash_matches(stored, envelope, request_hash):
                    raise IdempotencyConflict(
                        "idempotency key was already used for a different request"
                    )
                return replay_command(stored)

            try:
                with uow.savepoint():
                    result = handler.handle(uow, envelope)
            except DomainError as exc:
                uow.commands.record_rejected(
                    envelope,
                    request_hash,
                    exc.code,
                    {
                        "code": exc.code,
                        "message": str(exc),
                        "details": exc.details,
                    },
                )
                rejection = exc
            else:
                uow.commands.record_applied(envelope, request_hash, result)

        if rejection is not None:
            raise rejection
        if result is None:
            raise RuntimeError("command completed without a result")
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


def _request_hash_matches(
    stored: StoredCommand, envelope: CommandEnvelope, request_hash: str
) -> bool:
    if stored.request_hash == request_hash:
        return True
    return (
        stored.target_type == envelope.target_type
        and stored.request_hash == _legacy_hash_request(envelope)
    )


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


def replay_command(stored: StoredCommand) -> CommandResult:
    if stored.status == "applied":
        return _result_from_document(stored.result)
    if stored.status != "rejected" or stored.rejection_reason is None:
        raise RuntimeError("stored command status is invalid")

    code = stored.result.get("code")
    message = stored.result.get("message")
    details = stored.result.get("details")
    if (
        not isinstance(code, str)
        or not isinstance(message, str)
        or not isinstance(details, dict)
    ):
        raise TypeError("stored command rejection document is invalid")
    if code != stored.rejection_reason:
        raise RuntimeError("stored command rejection code is invalid")
    raise ReplayedDomainError(code, message, details)
