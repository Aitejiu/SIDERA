from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

CommandTerminalStatus = Literal["applied", "rejected"]
CommandStatus = Literal["pending", "applied", "rejected"]


@dataclass(frozen=True, slots=True)
class CommandTerminalView:
    command_id: UUID
    status: CommandTerminalStatus
    result: dict[str, object]
    error_code: str | None


class CommandQueryStore(Protocol):
    def get_terminal(
        self, workspace_id: UUID, command_id: UUID
    ) -> CommandTerminalView | None: ...


@dataclass(frozen=True, slots=True)
class CommandStatusView:
    command_id: UUID
    status: CommandStatus
    result: dict[str, object] | None
    error_code: str | None


class CommandQueryService:
    def __init__(self, store: CommandQueryStore) -> None:
        self._store = store

    def get_status(self, workspace_id: UUID, command_id: UUID) -> CommandStatusView:
        terminal = self._store.get_terminal(workspace_id, command_id)
        if terminal is None:
            return CommandStatusView(
                command_id=command_id,
                status="pending",
                result=None,
                error_code=None,
            )
        return CommandStatusView(
            command_id=terminal.command_id,
            status=terminal.status,
            result=terminal.result if terminal.status == "applied" else None,
            error_code=terminal.error_code if terminal.status == "rejected" else None,
        )
