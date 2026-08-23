from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import JsonValue

STATE_KEY = "sidera_task_bridge_state_v1"


class KeyValueStore(Protocol):
    def get(self, key: str, default: object = None) -> object: ...

    def set(self, key: str, value: object) -> None: ...


class StateConflict(RuntimeError):
    """A command or state mutation conflicts with durable plugin state."""


class StateCorrupt(RuntimeError):
    """The plugin-data document cannot be trusted or understood."""


class PrivateRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str
    chat_id: str
    session_key: str


class OutboundCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    resource_lock_key: str
    request_json: dict[str, JsonValue]
    created_at: datetime
    last_attempted_at: datetime
    attempt_count: int = Field(ge=0)


class SettledCommandAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    outcome: Literal["applied", "rejected"]
    error_code: str | None
    settled_at: datetime


class BridgeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    unresolved_commands: dict[str, OutboundCommand] = Field(default_factory=dict)
    source_routes: dict[str, PrivateRoute] = Field(default_factory=dict)
    settled_commands: list[SettledCommandAudit] = Field(default_factory=list)


class BridgeStateStore:
    """Serialize all plugin-data mutations through one in-process lock."""

    def __init__(self, kv_store: KeyValueStore) -> None:
        self._kv_store = kv_store
        self._lock = asyncio.Lock()

    async def snapshot(self) -> BridgeState:
        async with self._lock:
            return self._load()

    async def get_unresolved(self, command_id: UUID) -> OutboundCommand | None:
        async with self._lock:
            return self._load().unresolved_commands.get(str(command_id))

    async def list_unresolved(self) -> tuple[OutboundCommand, ...]:
        async with self._lock:
            state = self._load()
            return tuple(
                sorted(
                    state.unresolved_commands.values(),
                    key=lambda item: (item.created_at, str(item.command_id)),
                )
            )

    async def reserve(self, command: OutboundCommand) -> OutboundCommand:
        """Persist an exact command before the caller sends it externally."""

        async with self._lock:
            state = self._load()
            key = str(command.command_id)
            existing = state.unresolved_commands.get(key)
            if existing is not None:
                if existing != command:
                    raise StateConflict("command identity already has different state")
                return existing
            state.unresolved_commands[key] = command
            self._save(state)
            return command

    async def record_attempt(self, command_id: UUID, *, at: datetime) -> None:
        async with self._lock:
            state = self._load()
            key = str(command_id)
            existing = state.unresolved_commands.get(key)
            if existing is None:
                raise StateConflict("unresolved command does not exist")
            state.unresolved_commands[key] = existing.model_copy(
                update={
                    "last_attempted_at": at,
                    "attempt_count": existing.attempt_count + 1,
                }
            )
            self._save(state)

    async def settle(
        self,
        command_id: UUID,
        *,
        outcome: Literal["applied", "rejected"],
        error_code: str | None,
        settled_at: datetime | None = None,
    ) -> None:
        async with self._lock:
            state = self._load()
            key = str(command_id)
            if key not in state.unresolved_commands:
                raise StateConflict("cannot settle an unknown command")
            del state.unresolved_commands[key]
            state.settled_commands.append(
                SettledCommandAudit(
                    command_id=command_id,
                    outcome=outcome,
                    error_code=error_code,
                    settled_at=settled_at or datetime.now().astimezone(),
                )
            )
            state.settled_commands = state.settled_commands[-128:]
            self._save(state)

    async def save_route(self, source_ref: str, route: PrivateRoute) -> None:
        async with self._lock:
            state = self._load()
            existing = state.source_routes.get(source_ref)
            if existing is not None and existing != route:
                raise StateConflict("source route already has different state")
            state.source_routes[source_ref] = route
            self._save(state)

    def _load(self) -> BridgeState:
        raw = self._kv_store.get(STATE_KEY, None)
        if raw is None:
            return BridgeState()
        try:
            return BridgeState.model_validate(raw)
        except ValidationError as exc:
            raise StateCorrupt("task bridge state is invalid") from exc

    def _save(self, state: BridgeState) -> None:
        self._kv_store.set(STATE_KEY, state.model_dump(mode="json"))
