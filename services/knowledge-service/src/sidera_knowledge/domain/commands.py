from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sidera_knowledge.domain.common import (
    DomainValidationError,
    canonical_json_bytes,
    require_text,
    sha256_hex,
)


class PayloadSafetyError(DomainValidationError):
    """Raised when a durable event payload attempts to carry knowledge body data."""


def _validate_event_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PayloadSafetyError("event payload keys must be strings")
            lowered = key.lower()
            if any(fragment in lowered for fragment in ("body", "content", "markdown", "snapshot")):
                raise PayloadSafetyError(f"event payload key is not allowed: {key}")
            result[key] = _validate_event_payload(item)
        return result
    if isinstance(value, list):
        return [_validate_event_payload(item) for item in value]
    return value


def _pascal_to_snake(value: str) -> str:
    first = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", first).lower()


@dataclass(frozen=True)
class CommandEnvelope:
    command_id: UUID
    workspace_id: UUID
    command_type: str
    actor_type: str
    actor_id: str
    target_type: str
    target_id: UUID | None
    payload: dict[str, Any]
    expected_version: int | None
    idempotency_key: str
    correlation_id: UUID

    def __post_init__(self) -> None:
        for field_name in (
            "command_type",
            "actor_type",
            "actor_id",
            "target_type",
            "idempotency_key",
        ):
            object.__setattr__(
                self,
                field_name,
                require_text(getattr(self, field_name), field_name),
            )
        if self.expected_version is not None and self.expected_version < 1:
            raise DomainValidationError("expected_version must be positive")
        object.__setattr__(self, "payload", dict(self.payload))
        canonical_json_bytes(self.payload)

    @property
    def request_hash(self) -> str:
        return sha256_hex(
            canonical_json_bytes(
                {
                    "workspace_id": self.workspace_id,
                    "command_type": self.command_type,
                    "actor_type": self.actor_type,
                    "actor_id": self.actor_id,
                    "target_type": self.target_type,
                    "target_id": self.target_id,
                    "payload": self.payload,
                    "expected_version": self.expected_version,
                    "idempotency_key": self.idempotency_key,
                }
            )
        )


@dataclass(frozen=True)
class EventEnvelope:
    event_id: UUID
    workspace_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime
    correlation_id: UUID
    causation_id: UUID | None = None

    def __post_init__(self) -> None:
        for field_name in ("aggregate_type", "event_type"):
            object.__setattr__(
                self,
                field_name,
                require_text(getattr(self, field_name), field_name),
            )
        if re.fullmatch(r"[A-Z][A-Za-z0-9]*", self.event_type) is None:
            raise DomainValidationError("event_type must use PascalCase")
        if self.aggregate_version < 1:
            raise DomainValidationError("aggregate_version must be positive")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise DomainValidationError("occurred_at must be timezone aware")
        object.__setattr__(self, "payload", _validate_event_payload(dict(self.payload)))
        canonical_json_bytes(self.payload)


def topic_for_event(event_type: str) -> str:
    if re.fullmatch(r"[A-Z][A-Za-z0-9]*", event_type) is None:
        raise DomainValidationError("event_type must use PascalCase")
    if event_type.startswith("Knowledge"):
        event_type = event_type.removeprefix("Knowledge")
    return f"knowledge.{_pascal_to_snake(event_type)}"
