from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    id: UUID
    workspace_id: UUID
    command_type: str
    target_type: str
    actor_type: str
    actor_id: str
    target_id: UUID | None
    payload: dict[str, Any]
    expected_version: int | None
    idempotency_key: str
    correlation_id: UUID


@dataclass(frozen=True, slots=True)
class CommandResult:
    command_id: UUID
    aggregate_id: UUID
    aggregate_version: int
    event_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class StoredCommand:
    id: UUID
    request_hash: str
    status: str
    result: dict[str, Any]
    rejection_reason: str | None
