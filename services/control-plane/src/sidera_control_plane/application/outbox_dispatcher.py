import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    id: UUID
    event_id: UUID
    topic: str
    payload: dict[str, object]
    attempts: int
    claim_token: UUID


@dataclass(frozen=True, slots=True)
class DispatchReport:
    claimed: int
    published: int
    failed: int


class EventSink(Protocol):
    def publish(self, message: OutboxMessage) -> None: ...


class OutboxStore(Protocol):
    def claim(
        self,
        batch_size: int,
        *,
        topics: Collection[str] | None = None,
    ) -> tuple[OutboxMessage, ...]: ...

    def acknowledge(self, message_id: UUID, claim_token: UUID) -> bool: ...

    def renew_claim(self, message_id: UUID, claim_token: UUID) -> bool: ...

    def reschedule(
        self,
        message_id: UUID,
        claim_token: UUID,
        next_attempt_at: datetime,
        error: str,
    ) -> bool: ...


class OutboxDispatcher:
    def __init__(self, store: OutboxStore, sink: EventSink) -> None:
        self._store = store
        self._sink = sink

    def dispatch_once(self, batch_size: int = 100) -> DispatchReport:
        if not 1 <= batch_size <= 1_000:
            raise ValueError("batch_size must be between 1 and 1000")

        messages = self._store.claim(batch_size)
        published = 0
        failed = 0
        for message in messages:
            try:
                self._sink.publish(message)
            # Event sinks are plug-ins; every ordinary sink failure is retryable here.
            except Exception as exc:  # noqa: BLE001
                error = _bounded_error(exc)
                delay_seconds = min(300, 2 ** min(message.attempts, 8))
                retry_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
                if self._store.reschedule(
                    message.id,
                    message.claim_token,
                    retry_at,
                    error,
                ):
                    failed += 1
            else:
                if self._store.acknowledge(message.id, message.claim_token):
                    published += 1

        return DispatchReport(
            claimed=len(messages),
            published=published,
            failed=failed,
        )


def _bounded_error(exc: Exception) -> str:
    error = f"{type(exc).__name__}: {exc}"
    error = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@",
        r"\1***@",
        error,
    )
    error = re.sub(
        r"(?i)(password|passwd|token|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+",
        r"\1=***",
        error,
    )
    return error[:1_000]
