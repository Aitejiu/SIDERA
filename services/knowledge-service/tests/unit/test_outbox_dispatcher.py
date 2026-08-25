from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sidera_knowledge.application.outbox_dispatcher import (
    OutboxDispatcher,
    OutboxMessage,
)
from sidera_knowledge.knowledge_outbox_worker import JsonLineSink

NOW = datetime(2026, 1, 2, tzinfo=UTC)


def _message(
    *,
    created_at: datetime,
    payload: dict[str, object] | None = None,
    attempt: int = 0,
) -> OutboxMessage:
    event_id = uuid4()
    return OutboxMessage(
        message_id=uuid4(),
        event_id=event_id,
        workspace_id=uuid4(),
        topic="knowledge.test_event",
        payload=payload or {"event_id": str(event_id), "status": "ready"},
        attempt=attempt,
        created_at=created_at,
        lease_id=None,
    )


@dataclass
class FakeStore:
    messages: list[OutboxMessage]
    published: list[UUID]
    failed: list[tuple[UUID, str, datetime, datetime | None]]

    def claim_batch(self, *, now, lease_id, lease_until, limit):
        selected = sorted(
            self.messages, key=lambda message: (message.created_at, message.message_id)
        )[:limit]
        for message in selected:
            index = self.messages.index(message)
            self.messages[index] = replace(
                message, attempt=message.attempt + 1, lease_id=lease_id
            )
        return tuple(
            replace(message, attempt=message.attempt + 1, lease_id=lease_id)
            for message in selected
        )

    def mark_published(self, *, message_id, lease_id, published_at):
        self.published.append(message_id)
        self.messages = [message for message in self.messages if message.message_id != message_id]

    def mark_failed(
        self,
        *,
        message_id,
        lease_id,
        available_at,
        error_code,
        dead_lettered_at,
    ):
        self.failed.append((message_id, error_code, available_at, dead_lettered_at))
        if dead_lettered_at is not None:
            self.messages = [
                message for message in self.messages if message.message_id != message_id
            ]


class RecordingSink:
    def __init__(self, failing_event_ids: set[UUID] | None = None) -> None:
        self.messages: list[OutboxMessage] = []
        self.failing_event_ids = failing_event_ids or set()

    def publish(self, message: OutboxMessage) -> None:
        self.messages.append(message)
        if message.event_id in self.failing_event_ids:
            raise RuntimeError("sink failed")


def _dispatcher(store: FakeStore, sink: RecordingSink, **kwargs) -> OutboxDispatcher:
    return OutboxDispatcher(
        store,
        sink,
        lease_duration=timedelta(seconds=30),
        base_backoff=timedelta(seconds=5),
        max_backoff=timedelta(seconds=30),
        **kwargs,
    )


def test_dispatcher_claims_bounded_created_order_and_continues_after_failure() -> None:
    first = _message(created_at=NOW + timedelta(seconds=2))
    second = _message(created_at=NOW)
    third = _message(created_at=NOW + timedelta(seconds=3))
    store = FakeStore([first, second, third], [], [])
    sink = RecordingSink({second.event_id})

    report = _dispatcher(store, sink, batch_size=2).dispatch_once(now=NOW)

    assert report.claimed == 2
    assert report.published == 1
    assert report.failed == 1
    assert report.dead_lettered == 0
    assert [message.event_id for message in sink.messages] == [
        second.event_id,
        first.event_id,
    ]
    assert store.failed == [
        (
            second.message_id,
            "sink_publish_failed",
            NOW + timedelta(seconds=5),
            None,
        )
    ]
    assert store.published == [first.message_id]
    assert third.message_id in {message.message_id for message in store.messages}


def test_dispatcher_rejects_nested_body_payload_without_publishing() -> None:
    message = _message(
        created_at=NOW,
        payload={"nested": [{"snapshot": "must not leave the database"}]},
    )
    store = FakeStore([message], [], [])
    sink = RecordingSink()

    report = _dispatcher(store, sink).dispatch_once(now=NOW)

    assert report.failed == 1
    assert report.published == 0
    assert sink.messages == []
    assert store.failed[0][1] == "payload_unsafe"


def test_dispatcher_dead_letters_after_bounded_attempts() -> None:
    message = _message(created_at=NOW)
    store = FakeStore([message], [], [])
    sink = RecordingSink({message.event_id})
    dispatcher = _dispatcher(store, sink, batch_size=1, max_attempts=2)

    first = dispatcher.dispatch_once(now=NOW)
    second = dispatcher.dispatch_once(now=NOW + timedelta(seconds=6))

    assert first.dead_lettered == 0
    assert second.failed == 1
    assert second.dead_lettered == 1
    assert store.failed[-1][1] == "sink_publish_failed"
    assert store.failed[-1][3] == NOW + timedelta(seconds=6)


def test_json_line_sink_emits_only_safe_identifiers_and_hashes(capsys) -> None:
    message = _message(
        created_at=NOW,
        payload={
            "candidate_id": str(uuid4()),
            "candidate_hash": "a" * 64,
            "status": "pending",
            "body_markdown": "secret body must not be printed",
        },
    )

    JsonLineSink().publish(message)

    output = capsys.readouterr().out
    assert "secret body must not be printed" not in output
    parsed = json.loads(output)
    assert parsed["message_id"] == str(message.message_id)
    assert parsed["event_id"] == str(message.event_id)
    assert parsed["payload"] == {
        "candidate_hash": "a" * 64,
        "candidate_id": str(message.payload["candidate_id"]),
        "status": "pending",
    }
