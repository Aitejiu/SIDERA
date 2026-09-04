from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.api.dependencies import get_notification_delivery_service
from sidera_control_plane.application.notification_delivery import (
    NotificationDeliveryEnvelope,
    NotificationDeliverySettlement,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.notifications import (
    NotificationAttemptOutcome,
    NotificationDeliveryStatus,
)

pytestmark = pytest.mark.integration

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
DELIVERY_ID = UUID("10000000-0000-0000-0000-000000000001")
NOTIFICATION_ID = UUID("20000000-0000-0000-0000-000000000001")
THREAD_ID = UUID("30000000-0000-0000-0000-000000000001")
MODULE_ID = UUID("40000000-0000-0000-0000-000000000001")
CLAIM_TOKEN = UUID("50000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 9, 4, 14, 0, tzinfo=UTC)


def _envelope() -> NotificationDeliveryEnvelope:
    return NotificationDeliveryEnvelope(
        delivery_id=DELIVERY_ID,
        notification_id=NOTIFICATION_ID,
        thread_id=THREAD_ID,
        contract_version=1,
        claim_token=CLAIM_TOKEN,
        fencing_token=2,
        idempotency_key=f"sidera:notification-delivery:{DELIVERY_ID}",
        channel_type="akashic_private",
        source_ref="src_" + "a" * 32,
        kind="execution_attention",
        priority="action",
        root_task_title="实现通知网关",
        subtask_path=(),
        worker_item_title=None,
        title="实现通知网关",
        summary="任务需要关注。",
        next_action="打开任务查看",
        deep_link=f"/workspaces/{WORKSPACE_ID}/tasks/{UUID('60000000-0000-0000-0000-000000000001')}",
        correlation_id="corr-api-test",
        notification_ref="NTF-2000",
        created_at=NOW,
    )


def _settlement(outcome: NotificationAttemptOutcome) -> NotificationDeliverySettlement:
    return NotificationDeliverySettlement(
        delivery_id=DELIVERY_ID,
        status=(
            NotificationDeliveryStatus.DELIVERED
            if outcome is NotificationAttemptOutcome.DELIVERED
            else NotificationDeliveryStatus.RETRY_WAIT
        ),
        attempt_number=1,
        outcome=outcome,
        provider_message_ref="msg_123" if outcome is NotificationAttemptOutcome.DELIVERED else None,
        next_attempt_at=None if outcome is NotificationAttemptOutcome.DELIVERED else NOW,
        fencing_token=2,
    )


class _FakeDeliveryService:
    def __init__(self) -> None:
        self.claim_calls: list[dict[str, object]] = []
        self.ack_calls: list[dict[str, object]] = []
        self.nack_calls: list[dict[str, object]] = []

    def claim(self, workspace_id: UUID, module_instance_id: UUID, **kwargs: object):
        self.claim_calls.append(
            {"workspace_id": workspace_id, "module_instance_id": module_instance_id, **kwargs}
        )
        return (_envelope(),)

    def ack(self, workspace_id: UUID, delivery_id: UUID, **kwargs: object):
        self.ack_calls.append(
            {"workspace_id": workspace_id, "delivery_id": delivery_id, **kwargs}
        )
        return _settlement(NotificationAttemptOutcome.DELIVERED)

    def nack(self, workspace_id: UUID, delivery_id: UUID, **kwargs: object):
        self.nack_calls.append(
            {"workspace_id": workspace_id, "delivery_id": delivery_id, **kwargs}
        )
        return _settlement(NotificationAttemptOutcome.RETRYABLE_FAILURE)


@pytest.fixture
def service() -> _FakeDeliveryService:
    return _FakeDeliveryService()


@pytest.fixture
def client(database_url: str, service: _FakeDeliveryService) -> Iterator[TestClient]:
    app = create_app(Settings(database_url=database_url, api_bind_host="127.0.0.1"))
    app.dependency_overrides[get_notification_delivery_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_claim_api_is_strict_and_returns_only_the_delivery_envelope(
    client: TestClient, service: _FakeDeliveryService
) -> None:
    response = client.post(
        f"/api/v1/workspaces/{WORKSPACE_ID}/notification-deliveries/claims",
        json={
            "module_instance_id": str(MODULE_ID),
            "supported_contract_versions": [1],
            "batch_size": 50,
            "lease_seconds": 30,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"deliveries": [_envelope().to_mapping()]}
    assert service.claim_calls[0]["module_instance_id"] == MODULE_ID

    extra = client.post(
        f"/api/v1/workspaces/{WORKSPACE_ID}/notification-deliveries/claims",
        json={
            "module_instance_id": str(MODULE_ID),
            "supported_contract_versions": [1],
            "batch_size": 50,
            "lease_seconds": 30,
            "provider_token": "must-not-be-accepted",
        },
    )
    assert extra.status_code == 422
    assert extra.json()["error"]["code"] == "validation_error"


def test_ack_and_nack_api_accept_only_bounded_typed_fields(
    client: TestClient, service: _FakeDeliveryService
) -> None:
    ack = client.post(
        f"/api/v1/workspaces/{WORKSPACE_ID}/notification-deliveries/{DELIVERY_ID}/ack",
        json={
            "claim_token": str(CLAIM_TOKEN),
            "fencing_token": 2,
            "completed_at": NOW.isoformat(),
            "provider_message_ref": "msg_123",
        },
    )
    assert ack.status_code == 200
    assert ack.json()["status"] == "delivered"
    assert service.ack_calls[0]["provider_message_ref"] == "msg_123"

    nack = client.post(
        f"/api/v1/workspaces/{WORKSPACE_ID}/notification-deliveries/{DELIVERY_ID}/nack",
        json={
            "claim_token": str(CLAIM_TOKEN),
            "fencing_token": 2,
            "outcome": "retryable_failure",
            "stable_error_code": "timeout",
            "retry_after_seconds": 10,
            "provider_message_ref": "provider_ref_1",
        },
    )
    assert nack.status_code == 200
    assert nack.json()["status"] == "retry_wait"
    assert service.nack_calls[0]["error_code"] == "timeout"

    raw = client.post(
        f"/api/v1/workspaces/{WORKSPACE_ID}/notification-deliveries/{DELIVERY_ID}/nack",
        json={
            "claim_token": str(CLAIM_TOKEN),
            "fencing_token": 2,
            "outcome": "retryable_failure",
            "stable_error_code": "timeout",
            "exception": "provider returned a secret body",
        },
    )
    assert raw.status_code == 422
    assert raw.json()["error"]["code"] == "validation_error"


def test_every_delivery_workflow_endpoint_fails_closed_on_non_loopback_bind_host(
    database_url: str, service: _FakeDeliveryService
) -> None:
    app = create_app(Settings(database_url=database_url, api_bind_host="0.0.0.0"))
    app.dependency_overrides[get_notification_delivery_service] = lambda: service
    with TestClient(app) as client:
        headers = {"X-Forwarded-For": "127.0.0.1", "X-Claim-Token": str(CLAIM_TOKEN)}
        claim = client.post(
            f"/api/v1/workspaces/{WORKSPACE_ID}/notification-deliveries/claims",
            headers=headers,
            json={
                "module_instance_id": str(MODULE_ID),
                "supported_contract_versions": [1],
                "batch_size": 50,
                "lease_seconds": 30,
            },
        )
        ack = client.post(
            f"/api/v1/workspaces/{WORKSPACE_ID}/notification-deliveries/{DELIVERY_ID}/ack",
            headers=headers,
            json={
                "claim_token": str(CLAIM_TOKEN),
                "fencing_token": 2,
                "completed_at": NOW.isoformat(),
            },
        )
        nack = client.post(
            f"/api/v1/workspaces/{WORKSPACE_ID}/notification-deliveries/{DELIVERY_ID}/nack",
            headers=headers,
            json={
                "claim_token": str(CLAIM_TOKEN),
                "fencing_token": 2,
                "outcome": "unknown_result",
                "stable_error_code": "unknown_result",
            },
        )

    for response in (claim, ack, nack):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "adapter_workflow_requires_loopback"
    assert service.claim_calls == []
    assert service.ack_calls == []
    assert service.nack_calls == []
    app.dependency_overrides.clear()
