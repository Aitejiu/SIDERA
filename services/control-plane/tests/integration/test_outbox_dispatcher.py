import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.outbox_dispatcher import (
    DispatchReport,
    EventSink,
    OutboxDispatcher,
    OutboxMessage,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import OutboxMessageModel
from sidera_control_plane.persistence.outbox_store import SqlAlchemyOutboxStore

pytestmark = pytest.mark.integration


def make_dispatcher(database: Database, sink: EventSink) -> OutboxDispatcher:
    return OutboxDispatcher(SqlAlchemyOutboxStore(database), sink)


class RecordingSink:
    def __init__(self) -> None:
        self.event_ids: list[str] = []

    def publish(self, message: OutboxMessage) -> None:
        self.event_ids.append(str(message.event_id))


class FailingSink:
    def publish(self, message: OutboxMessage) -> None:
        raise ConnectionError(
            f"sink unavailable for {message.event_id} password=hunter2: " + "x" * 1_200
        )


class BlockingSink:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def publish(self, message: OutboxMessage) -> None:
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError(f"release timed out for {message.event_id}")


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def seed_outbox(client: TestClient, workspace_id: UUID, number: int = 1) -> None:
    for index in range(number):
        response = client.post(
            "/api/commands",
            json={
                "command_id": str(uuid4()),
                "workspace_id": str(workspace_id),
                "command_type": "CreateTask",
                "actor": {"type": "user", "id": "user:test"},
                "target_id": None,
                "expected_version": None,
                "idempotency_key": f"outbox-task-{index}",
                "correlation_id": str(uuid4()),
                "payload": {
                    "title": f"Outbox task {index}",
                    "description": "Exercise outbox delivery",
                    "acceptance_criteria": ["Message is delivered"],
                    "priority": 50,
                },
            },
        )
        assert response.status_code == 201, response.text


def test_successful_delivery_persists_fenced_acknowledgment(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    seed_outbox(client, workspace_id)
    with database.session() as session:
        original_payload = dict(
            session.scalars(select(OutboxMessageModel)).one().payload
        )
    sink = RecordingSink()

    report = make_dispatcher(database, sink).dispatch_once(batch_size=10)

    with database.session() as session:
        stored = session.scalars(select(OutboxMessageModel)).one()
    assert report == DispatchReport(claimed=1, published=1, failed=0)
    assert sink.event_ids == [str(stored.event_id)]
    assert stored.published_at is not None
    assert stored.attempts == 1
    assert stored.claim_token is None
    assert stored.claimed_until is None
    assert stored.payload == original_payload


def test_failed_delivery_is_retained_and_scheduled_for_retry(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    seed_outbox(client, workspace_id)
    before = datetime.now(UTC)

    report = make_dispatcher(database, FailingSink()).dispatch_once()

    with database.session() as session:
        stored = session.scalars(select(OutboxMessageModel)).one()
    assert report == DispatchReport(claimed=1, published=0, failed=1)
    assert stored.published_at is None
    assert stored.attempts == 1
    assert stored.claim_token is None
    assert stored.claimed_until is None
    assert stored.next_attempt_at > before
    assert stored.last_error is not None
    assert stored.last_error.startswith("ConnectionError: sink unavailable")
    assert "hunter2" not in stored.last_error
    assert "password=***" in stored.last_error
    assert len(stored.last_error) == 1_000


def test_two_dispatchers_claim_each_due_message_once(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    seed_outbox(client, workspace_id, number=10)
    sink = RecordingSink()
    dispatchers = (
        make_dispatcher(database, sink),
        make_dispatcher(database, sink),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(
            executor.map(lambda item: item.dispatch_once(batch_size=5), dispatchers)
        )

    assert sum(report.claimed for report in reports) == 10
    assert sum(report.published for report in reports) == 10
    assert sum(report.failed for report in reports) == 0
    assert len(sink.event_ids) == 10
    assert len(set(sink.event_ids)) == 10
    with database.session() as session:
        stored = session.scalars(select(OutboxMessageModel)).all()
    assert all(message.published_at is not None for message in stored)
    assert all(message.attempts == 1 for message in stored)


def test_expired_claim_cannot_be_acknowledged_by_stale_dispatcher(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    seed_outbox(client, workspace_id)
    blocking_sink = BlockingSink()
    stale_dispatcher = make_dispatcher(database, blocking_sink)
    current_sink = RecordingSink()
    current_dispatcher = make_dispatcher(database, current_sink)

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_future = executor.submit(stale_dispatcher.dispatch_once)
        assert blocking_sink.entered.wait(timeout=5)
        with database.session() as session:
            session.execute(
                update(OutboxMessageModel)
                .where(OutboxMessageModel.published_at.is_(None))
                .values(claimed_until=datetime.now(UTC) - timedelta(seconds=1))
            )
            session.commit()

        current_report = current_dispatcher.dispatch_once()
        blocking_sink.release.set()
        stale_report = stale_future.result(timeout=10)

    assert current_report == DispatchReport(claimed=1, published=1, failed=0)
    assert stale_report == DispatchReport(claimed=1, published=0, failed=0)
    with database.session() as session:
        stored = session.scalars(select(OutboxMessageModel)).one()
    assert stored.published_at is not None
    assert stored.attempts == 2
    assert stored.claim_token is None
