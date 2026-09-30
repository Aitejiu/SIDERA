from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.outbox_store import SqlAlchemyOutboxStore

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str):
    settings = Settings(database_url=database_url)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _seed_outbox(client: TestClient, workspace_id: UUID) -> None:
    response = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "CreateTask",
            "target_type": "task",
            "actor": {"type": "user", "id": "claim-renewal-test"},
            "target_id": None,
            "expected_version": None,
            "idempotency_key": f"claim-renewal-{uuid4()}",
            "correlation_id": str(uuid4()),
            "payload": {
                "title": "Claim renewal fixture",
                "description": "Prove a live claim remains fenced across TTL boundaries.",
                "acceptance_criteria": ["The durable claim is not duplicated."],
                "priority": 50,
            },
        },
    )
    assert response.status_code == 201, response.text


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def test_live_claim_renews_across_multiple_ttls_without_duplicate_claim(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    _seed_outbox(client, workspace_id)
    now = datetime.now(UTC) + timedelta(seconds=1)
    clock = _Clock(now)
    first_store = SqlAlchemyOutboxStore(database, claim_seconds=3, clock=clock)
    second_store = SqlAlchemyOutboxStore(database, claim_seconds=3, clock=clock)

    claimed = first_store.claim(1)
    assert len(claimed) == 1
    message = claimed[0]

    for offset in (1, 2, 3, 4):
        clock.value = now + timedelta(seconds=offset)
        assert first_store.renew_claim(message.id, message.claim_token) is True
        assert second_store.claim(1) == ()

    assert first_store.acknowledge(message.id, message.claim_token) is True
    assert second_store.claim(1) == ()


def test_old_token_cannot_ack_after_an_expired_claim_is_reclaimed(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    _seed_outbox(client, workspace_id)
    now = datetime.now(UTC) + timedelta(seconds=1)
    clock = _Clock(now)
    first_store = SqlAlchemyOutboxStore(database, claim_seconds=3, clock=clock)
    second_store = SqlAlchemyOutboxStore(database, claim_seconds=3, clock=clock)

    old_message = first_store.claim(1)[0]
    clock.value = now + timedelta(seconds=4)
    reclaimed_message = second_store.claim(1)[0]

    assert first_store.acknowledge(old_message.id, old_message.claim_token) is False
    assert second_store.acknowledge(
        reclaimed_message.id, reclaimed_message.claim_token
    ) is True
