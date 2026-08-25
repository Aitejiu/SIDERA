from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sidera_knowledge.application.ports import (
    OutboxMessageStorePort,
    OutboxSinkPort,
)
from sidera_knowledge.domain.common import DomainValidationError


class OutboxPayloadError(DomainValidationError):
    """Raised when an outbox payload contains knowledge body material."""


@dataclass(frozen=True)
class OutboxMessage:
    message_id: UUID
    event_id: UUID
    workspace_id: UUID
    topic: str
    payload: dict[str, object]
    attempt: int
    created_at: datetime
    lease_id: UUID | None


@dataclass(frozen=True)
class DispatchReport:
    claimed: int
    published: int
    failed: int
    dead_lettered: int


_FORBIDDEN_PAYLOAD_KEY_PARTS = ("body", "content", "markdown", "snapshot")


def validate_outbox_payload(value: Any) -> None:
    """Reject body-bearing fields even if a legacy row bypassed domain validation."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise OutboxPayloadError("outbox payload keys must be strings")
            lowered = key.lower()
            if any(part in lowered for part in _FORBIDDEN_PAYLOAD_KEY_PARTS):
                raise OutboxPayloadError("outbox payload contains forbidden content")
            validate_outbox_payload(item)
    elif isinstance(value, list):
        for item in value:
            validate_outbox_payload(item)


class OutboxDispatcher:
    def __init__(
        self,
        store: OutboxMessageStorePort,
        sink: OutboxSinkPort,
        *,
        batch_size: int = 100,
        max_attempts: int = 5,
        lease_duration: timedelta = timedelta(seconds=30),
        base_backoff: timedelta = timedelta(seconds=5),
        max_backoff: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if base_backoff <= timedelta(0):
            raise ValueError("base_backoff must be positive")
        if max_backoff < base_backoff:
            raise ValueError("max_backoff must not be less than base_backoff")
        self._store = store
        self._sink = sink
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._lease_duration = lease_duration
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._clock = clock or (lambda: datetime.now(UTC))

    def dispatch_once(self, *, now: datetime | None = None) -> DispatchReport:
        current_time = now or self._clock()
        lease_id = uuid4()
        messages = self._store.claim_batch(
            now=current_time,
            lease_id=lease_id,
            lease_until=current_time + self._lease_duration,
            limit=self._batch_size,
        )
        published = 0
        failed = 0
        dead_lettered = 0
        for message in messages:
            try:
                validate_outbox_payload(message.payload)
                self._sink.publish(message)
            except OutboxPayloadError:
                error_code = "payload_unsafe"
            except Exception:  # noqa: BLE001 - sink adapters are provider-defined
                error_code = "sink_publish_failed"
            else:
                try:
                    self._store.mark_published(
                        message_id=message.message_id,
                        lease_id=message.lease_id or lease_id,
                        published_at=current_time,
                    )
                except Exception:  # noqa: BLE001 - lease update is retried after expiry
                    failed += 1
                else:
                    published += 1
                continue

            failed += 1
            is_dead_letter = message.attempt >= self._max_attempts
            if is_dead_letter:
                dead_lettered += 1
            try:
                self._store.mark_failed(
                    message_id=message.message_id,
                    lease_id=message.lease_id or lease_id,
                    available_at=current_time + self._backoff(message.attempt),
                    error_code=error_code,
                    dead_lettered_at=current_time if is_dead_letter else None,
                )
            except Exception:  # noqa: BLE001, S110 - lease expiry preserves recoverability
                # The lease will expire and the message remains recoverable.
                pass
        return DispatchReport(
            claimed=len(messages),
            published=published,
            failed=failed,
            dead_lettered=dead_lettered,
        )

    def _backoff(self, attempt: int) -> timedelta:
        multiplier = 2 ** max(attempt - 1, 0)
        delay = self._base_backoff * multiplier
        return min(delay, self._max_backoff)
