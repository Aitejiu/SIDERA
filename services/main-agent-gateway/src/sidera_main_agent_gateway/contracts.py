from __future__ import annotations

import math
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

type JsonPrimitive = None | bool | int | float | str
type JsonValue = JsonPrimitive | list[JsonValue] | dict[str, JsonValue]

TurnStatus = Literal[
    "queued",
    "in_progress",
    "completed",
    "interrupted",
    "failed",
    "cancelled",
]
ItemType = Literal["userMessage", "assistantMessage", "toolCall", "error"]
EventType = Literal[
    "queued",
    "started",
    "item",
    "completed",
    "failed",
    "interrupted",
    "cancelled",
    "stream_lost",
]

_TURN_STATUSES = frozenset(
    {"queued", "in_progress", "completed", "interrupted", "failed", "cancelled"}
)
_ITEM_TYPES = frozenset({"userMessage", "assistantMessage", "toolCall", "error"})
_EVENT_TYPES = frozenset(
    {
        "queued",
        "started",
        "item",
        "completed",
        "failed",
        "interrupted",
        "cancelled",
        "stream_lost",
    }
)


def _validate_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _validate_timestamp(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _validate_json(value: object, field_name: str) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must be JSON-compatible")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field_name} must be JSON-compatible")
            _validate_json(child, f"{field_name}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json(child, f"{field_name}[{index}]")
        return
    raise ValueError(f"{field_name} must be JSON-compatible")


def _validate_json_mapping(
    value: Mapping[str, JsonValue] | None,
    field_name: str,
) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be JSON-compatible")
    _validate_json(value, field_name)


@dataclass(frozen=True, slots=True)
class MainAgentThread:
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    context_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_id(self.id, "thread id")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("thread title must be a non-empty string")
        _validate_timestamp(self.created_at, "thread created_at")
        _validate_timestamp(self.updated_at, "thread updated_at")
        if not isinstance(self.context_refs, tuple):
            raise TypeError("thread context_refs must be a tuple")
        for context_ref in self.context_refs:
            _validate_id(context_ref, "thread context ref")


@dataclass(frozen=True, slots=True)
class MainAgentItem:
    id: str
    type: ItemType
    data: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        _validate_id(self.id, "item id")
        if self.type not in _ITEM_TYPES:
            raise ValueError(f"unknown item type: {self.type}")
        _validate_json_mapping(self.data, "item data")


@dataclass(frozen=True, slots=True)
class MainAgentTurn:
    id: str
    thread_id: str
    status: TurnStatus
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    final_response: str | None
    items: tuple[MainAgentItem, ...]
    usage: Mapping[str, JsonValue] | None
    error: Mapping[str, JsonValue] | None

    def __post_init__(self) -> None:
        _validate_id(self.id, "turn id")
        _validate_id(self.thread_id, "turn thread_id")
        if self.status not in _TURN_STATUSES:
            raise ValueError(f"unknown turn status: {self.status}")
        _validate_timestamp(self.created_at, "turn created_at")
        if self.started_at is not None:
            _validate_timestamp(self.started_at, "turn started_at")
        if self.completed_at is not None:
            _validate_timestamp(self.completed_at, "turn completed_at")
        if self.final_response is not None and not isinstance(
            self.final_response, str
        ):
            raise ValueError("turn final_response must be a string")
        if not isinstance(self.items, tuple) or not all(
            isinstance(item, MainAgentItem) for item in self.items
        ):
            raise ValueError("turn items must be a tuple of MainAgentItem")
        _validate_json_mapping(self.usage, "turn usage")
        _validate_json_mapping(self.error, "turn error")


@dataclass(frozen=True, slots=True)
class MainAgentEvent:
    sequence: int
    type: EventType
    data: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("event sequence must be positive")
        if self.type not in _EVENT_TYPES:
            raise ValueError(f"unknown event type: {self.type}")
        _validate_json_mapping(self.data, "event data")


@dataclass(frozen=True, slots=True)
class MainAgentThreadPage:
    threads: tuple[MainAgentThread, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.threads, tuple) or not all(
            isinstance(thread, MainAgentThread) for thread in self.threads
        ):
            raise ValueError("thread page threads must be a tuple of MainAgentThread")
        if self.next_cursor is not None:
            _validate_id(self.next_cursor, "thread page next_cursor")


class MainAgentTurnHandle(Protocol):
    thread_id: str
    id: str

    def events(self) -> AsyncIterator[MainAgentEvent]: ...

    async def result(self) -> MainAgentTurn: ...

    async def interrupt(self) -> MainAgentTurn: ...


class MainAgentAdapter(Protocol):
    async def health(self) -> Mapping[str, JsonValue]: ...

    async def capabilities(self) -> Mapping[str, JsonValue]: ...

    async def list_threads(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> MainAgentThreadPage: ...

    async def create_thread(
        self,
        *,
        title: str,
        context_refs: Sequence[str],
    ) -> MainAgentThread: ...

    async def read_thread(self, thread_id: str) -> MainAgentThread: ...

    async def start_turn(
        self,
        thread_id: str,
        input_text: str,
    ) -> MainAgentTurnHandle: ...

    async def read_turn(self, thread_id: str, turn_id: str) -> MainAgentTurn: ...

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> MainAgentTurn: ...

    async def close(self) -> None: ...
