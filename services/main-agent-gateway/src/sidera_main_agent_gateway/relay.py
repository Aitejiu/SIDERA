from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Literal

from sidera_main_agent_gateway.contracts import (
    JsonValue,
    MainAgentTurnHandle,
    TurnStatus,
)
from sidera_main_agent_gateway.errors import GatewayError

_TERMINAL_EVENTS = frozenset({"completed", "failed", "interrupted", "cancelled"})


class RelayError(GatewayError):
    """Base class for relay lifecycle and cursor errors."""


class RelayCursorExpired(RelayError):
    """The requested cursor is older than the bounded relay buffer."""


class RelayAlreadyActive(RelayError):
    """A provider stream already exists for the same thread and turn."""


@dataclass(frozen=True, slots=True)
class RelayEvent:
    sequence: int
    event: str
    data: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("relay sequence must be positive")
        if not isinstance(self.event, str) or not self.event:
            raise ValueError("relay event must be a non-empty string")
        if not isinstance(self.data, Mapping):
            raise TypeError("relay event data must be a mapping")
        if any(not isinstance(key, str) for key in self.data):
            raise TypeError("relay event data keys must be strings")
        try:
            json.dumps(dict(self.data), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("relay event data must be JSON-compatible") from exc


@dataclass(frozen=True, slots=True)
class RelayAvailability:
    stream_available: bool
    recovery_mode: Literal["poll"] | None


class TurnRelay:
    def __init__(
        self,
        handle: MainAgentTurnHandle,
        *,
        event_limit: int,
    ) -> None:
        if event_limit < 1:
            raise ValueError("event_limit must be positive")
        self.thread_id = handle.thread_id
        self.turn_id = handle.id
        self._handle = handle
        self._events: deque[RelayEvent] = deque(maxlen=event_limit)
        self._next_sequence = 1
        self._condition = asyncio.Condition()
        self._terminal = False
        self._stream_lost = False
        self._closed = False
        self._task: asyncio.Task[None] | None = None

    @property
    def terminal(self) -> bool:
        return self._terminal

    @property
    def stream_lost(self) -> bool:
        return self._stream_lost

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stream_available(self) -> bool:
        return not self._stream_lost and not self._closed or self._terminal

    def start(self) -> None:
        if self._task is not None:
            raise RelayAlreadyActive("relay consumer already started")
        self._task = asyncio.create_task(
            self._consume_provider_stream(),
            name=f"main-agent-relay:{self.thread_id}:{self.turn_id}",
        )

    async def _append(
        self,
        event: str,
        data: Mapping[str, JsonValue],
    ) -> RelayEvent | None:
        async with self._condition:
            if self._closed:
                return None
            relay_event = RelayEvent(self._next_sequence, event, dict(data))
            self._next_sequence += 1
            self._events.append(relay_event)
            if event in _TERMINAL_EVENTS:
                self._terminal = True
                self._closed = True
            elif event == "stream_lost":
                self._stream_lost = True
                self._closed = True
            self._condition.notify_all()
            return relay_event

    async def _append_stream_lost(self) -> None:
        if self._closed:
            return
        await self._append("stream_lost", {"reason": "provider_stream_unavailable"})

    async def _consume_provider_stream(self) -> None:
        try:
            async for provider_event in self._handle.events():
                await self._append(provider_event.type, provider_event.data)
                if provider_event.type in _TERMINAL_EVENTS:
                    return
            await self._append_stream_lost()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - provider failures become stream_lost
            await self._append_stream_lost()

    def _cursor_expired(self, after: int) -> bool:
        if not self._events:
            return False
        return after < self._events[0].sequence - 1

    async def events(self, *, after: int = 0) -> AsyncIterator[RelayEvent]:
        if after < 0:
            raise ValueError("relay cursor must not be negative")
        cursor = after
        while True:
            batch: list[RelayEvent]
            async with self._condition:
                if self._cursor_expired(cursor):
                    raise RelayCursorExpired(
                        f"relay cursor expired: after={cursor}, oldest={self._events[0].sequence}"
                    )
                batch = [
                    event for event in self._events if event.sequence > cursor
                ]
                if not batch and self._closed:
                    return
                if not batch:
                    await self._condition.wait()
                    continue
            for event in batch:
                if event.sequence <= cursor:
                    continue
                cursor = event.sequence
                yield event

    async def wait_closed(self) -> None:
        task = self._task
        if task is not None:
            await asyncio.shield(task)

    async def shutdown(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._condition:
            self._closed = True
            self._condition.notify_all()


class TurnRelayRegistry:
    def __init__(self, *, event_limit: int) -> None:
        if event_limit < 1:
            raise ValueError("event_limit must be positive")
        self._event_limit = event_limit
        self._relays: dict[tuple[str, str], TurnRelay] = {}
        self._lock = asyncio.Lock()

    async def start(self, handle: MainAgentTurnHandle) -> TurnRelay:
        key = (handle.thread_id, handle.id)
        async with self._lock:
            existing = self._relays.get(key)
            if existing is not None:
                if not existing.closed:
                    raise RelayAlreadyActive(
                        f"relay already active: {handle.thread_id}/{handle.id}"
                    )
                return existing
            if any(
                relay.thread_id == handle.thread_id and not relay.closed
                for relay in self._relays.values()
            ):
                raise RelayAlreadyActive(
                    f"thread already has an active relay: {handle.thread_id}"
                )
            relay = TurnRelay(handle, event_limit=self._event_limit)
            self._relays[key] = relay
            relay.start()
            return relay

    def get(self, thread_id: str, turn_id: str) -> TurnRelay | None:
        return self._relays.get((thread_id, turn_id))

    async def shutdown_turn(self, thread_id: str, turn_id: str) -> None:
        async with self._lock:
            relay = self._relays.get((thread_id, turn_id))
        if relay is not None:
            await relay.shutdown()

    def availability(
        self,
        thread_id: str,
        turn_id: str,
        status: TurnStatus | str,
    ) -> RelayAvailability:
        relay = self.get(thread_id, turn_id)
        if relay is not None and relay.stream_available:
            return RelayAvailability(stream_available=True, recovery_mode=None)
        if status in {"queued", "in_progress"}:
            return RelayAvailability(stream_available=False, recovery_mode="poll")
        return RelayAvailability(stream_available=False, recovery_mode=None)

    async def shutdown(self) -> None:
        relays = tuple(self._relays.values())
        if relays:
            await asyncio.gather(*(relay.shutdown() for relay in relays))
