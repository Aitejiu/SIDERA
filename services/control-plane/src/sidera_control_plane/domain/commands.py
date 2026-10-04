from dataclasses import dataclass, field
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
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StoredCommand:
    id: UUID
    workspace_id: UUID
    actor_type: str
    actor_id: str
    idempotency_key: str
    target_type: str
    request_hash: str
    status: str
    result: dict[str, Any]
    rejection_reason: str | None
    expected_version: int | None = None
