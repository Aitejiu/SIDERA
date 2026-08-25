from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sidera_knowledge.domain.commands import CommandEnvelope
from sidera_knowledge.domain.knowledge_errors import CommandFailure
from sidera_knowledge.persistence.repositories import PersistenceConflict


@dataclass(frozen=True)
class CommandOutcome:
    status: str
    command_id: UUID
    result: dict[str, Any]
    error_code: str | None = None


class KnowledgeCommandService:
    def __init__(
        self,
        uow_factory: Callable[[], Any],
        handlers: Mapping[str, Any],
    ) -> None:
        self._uow_factory = uow_factory
        self._handlers = dict(handlers)

    def handle(self, command: CommandEnvelope) -> CommandOutcome:
        with self._uow_factory() as uow:
            try:
                row = uow.commands.save_or_replay(command)
            except PersistenceConflict:
                uow.rollback()
                return CommandOutcome(
                    status="rejected",
                    command_id=command.command_id,
                    result={},
                    error_code="idempotency_conflict",
                )

            if row.request_hash != command.request_hash:
                uow.rollback()
                return CommandOutcome(
                    status="rejected",
                    command_id=command.command_id,
                    result={},
                    error_code="idempotency_conflict",
                )
            if row.status in {"applied", "rejected"}:
                return CommandOutcome(
                    status=row.status,
                    command_id=command.command_id,
                    result=row.result or {},
                    error_code=row.error_code,
                )

            try:
                handler = self._handlers.get(command.command_type)
                if handler is None:
                    raise CommandFailure("unsupported_command")
                if callable(handler):
                    result = cast(dict[str, Any], handler(command, uow))
                else:
                    result = cast(dict[str, Any], handler.handle(command, uow))
                uow.commands.mark_applied(
                    command.command_id, command.workspace_id, result
                )
                uow.commit()
                return CommandOutcome(
                    status="applied",
                    command_id=command.command_id,
                    result=result,
                )
            except CommandFailure as failure:
                uow.commands.mark_rejected(
                    command.command_id,
                    command.workspace_id,
                    failure.code,
                    failure.details,
                )
                uow.commit()
                return CommandOutcome(
                    status="rejected",
                    command_id=command.command_id,
                    result=failure.details,
                    error_code=failure.code,
                )
