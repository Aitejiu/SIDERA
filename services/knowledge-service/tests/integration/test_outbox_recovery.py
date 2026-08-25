from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from sidera_knowledge.application.outbox_dispatcher import OutboxDispatcher
from sidera_knowledge.domain.commands import EventEnvelope
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.models import EventModel, OutboxMessageModel
from sidera_knowledge.persistence.outbox_store import PostgresOutboxStore
from sidera_knowledge.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


def _event(
    *, workspace_id=None, payload: dict[str, object] | None = None
) -> EventEnvelope:
    resolved_workspace_id = workspace_id or uuid4()
    return EventEnvelope(
        event_id=uuid4(),
        workspace_id=resolved_workspace_id,
        aggregate_type="knowledge_test",
        aggregate_id=uuid4(),
        aggregate_version=1,
        event_type="KnowledgeTestEvent",
        payload=payload or {"status": "ready"},
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        correlation_id=uuid4(),
    )


def _append(database: Database, event: EventEnvelope) -> None:
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.events.append_with_outbox(event)
        uow.commit()


def test_concurrent_claims_are_exclusive_and_ordered(database: Database) -> None:
    workspace_id = uuid4()
    events = [_event(workspace_id=workspace_id) for _ in range(3)]
    for event in events:
        _append(database, event)
    now = datetime(2026, 1, 2, tzinfo=UTC)

    def claim() -> tuple:
        store = PostgresOutboxStore(database, workspace_id)
        return store.claim_batch(
            now=now,
            lease_id=uuid4(),
            lease_until=now + timedelta(minutes=5),
            limit=2,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(claim)
        second_future = executor.submit(claim)
        first = first_future.result()
        second = second_future.result()

    claimed = first + second
    assert len(claimed) == 3
    assert len({message.message_id for message in claimed}) == 3
    assert [message.created_at for message in first] == sorted(
        message.created_at for message in first
    )


def test_expired_lease_redelivers_same_message_id_and_event_id(
    database: Database,
) -> None:
    event = _event()
    _append(database, event)
    store = PostgresOutboxStore(database, event.workspace_id)
    first_now = datetime(2026, 1, 2, tzinfo=UTC)
    first = store.claim_batch(
        now=first_now,
        lease_id=uuid4(),
        lease_until=first_now + timedelta(seconds=30),
        limit=1,
    )
    assert len(first) == 1

    second = store.claim_batch(
        now=first_now + timedelta(minutes=1),
        lease_id=uuid4(),
        lease_until=first_now + timedelta(minutes=2),
        limit=1,
    )
    assert len(second) == 1
    assert second[0].message_id == first[0].message_id
    assert second[0].event_id == event.event_id
    assert second[0].attempt == 2

    store.mark_published(
        message_id=second[0].message_id,
        lease_id=second[0].lease_id,
        published_at=first_now + timedelta(minutes=1),
    )
    with database.session() as session:
        row = session.scalar(
            select(OutboxMessageModel).where(OutboxMessageModel.id == second[0].message_id)
        )
        assert row is not None
        assert row.published_at is not None
        assert row.dead_lettered_at is None


class AlwaysFailSink:
    def publish(self, message) -> None:
        raise RuntimeError("sink unavailable")


def test_dispatcher_dead_letters_without_deleting_event(
    database: Database,
) -> None:
    event = _event()
    _append(database, event)
    store = PostgresOutboxStore(database, event.workspace_id)
    dispatcher = OutboxDispatcher(
        store,
        AlwaysFailSink(),
        batch_size=1,
        max_attempts=2,
        lease_duration=timedelta(seconds=30),
        base_backoff=timedelta(seconds=5),
        max_backoff=timedelta(seconds=30),
    )
    first_now = datetime(2026, 1, 2, tzinfo=UTC)

    first = dispatcher.dispatch_once(now=first_now)
    second = dispatcher.dispatch_once(now=first_now + timedelta(seconds=6))

    assert first.failed == 1
    assert first.dead_lettered == 0
    assert second.dead_lettered == 1
    with database.session() as session:
        message = session.scalar(
            select(OutboxMessageModel).where(OutboxMessageModel.event_id == event.event_id)
        )
        stored_event = session.scalar(
            select(EventModel).where(EventModel.id == event.event_id)
        )
        assert message is not None
        assert message.dead_lettered_at is not None
        assert message.published_at is None
        assert message.publish_attempts == 2
        assert stored_event is not None
