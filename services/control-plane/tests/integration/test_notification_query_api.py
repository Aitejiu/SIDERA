from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.api.dependencies import get_notification_query_service
from sidera_control_plane.application.notification_queries import (
    NotificationQueryService,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.modules import (
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    NotificationChannelCapabilityProfile,
    NotificationReceiptCapability,
    StabilityLevel,
)
from sidera_control_plane.domain.notifications import (
    InboxThread,
    NotificationAttemptErrorCode,
    NotificationDeliveryAttempt,
    NotificationDeliveryIntent,
    NotificationDeliveryStatus,
    NotificationFact,
    NotificationPriority,
    NotificationRoute,
    NotificationRouteStatus,
    NotificationTransition,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.notification_query_store import (
    SqlAlchemyNotificationQueryStore,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 4, 14, 0, tzinfo=UTC)


@pytest.fixture
def client(database_url: str, database: Database) -> Iterator[TestClient]:
    app = create_app(Settings(database_url=database_url, local_actor_id="operator"))
    app.dependency_overrides[get_notification_query_service] = lambda: NotificationQueryService(
        SqlAlchemyNotificationQueryStore(database, clock=lambda: NOW)
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _task(workspace_id: UUID, title: str) -> Task:
    return Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title=title,
        description="notification query fixture",
        acceptance_criteria=("query is scoped",),
        priority=50,
        requested_by="test:notification-query",
    )


def _fact(
    workspace_id: UUID,
    root_task_id: UUID,
    *,
    priority: NotificationPriority,
    transition: NotificationTransition,
    kind: str,
    topic: str,
    occurred_at: datetime,
    fingerprint: str,
    recipient_id: str = "local",
) -> NotificationFact:
    return NotificationFact.create(
        workspace_id=workspace_id,
        policy_version=1,
        recipient_type="workspace_operator",
        recipient_id=recipient_id,
        kind=kind,
        priority=priority,
        transition=transition,
        topic=topic,
        root_task_id=root_task_id,
        subtask_id=None,
        worker_item_id=None,
        source_projection_cursor=12,
        source_projection_type="task_overview",
        source_resource_id=root_task_id,
        source_resource_version=1,
        authoritative_fingerprint=fingerprint,
        title="查询测试通知",
        summary="查询测试事实。",
        action="打开任务",
        deep_link=f"/tasks/{root_task_id}",
        correlation_id="notification-query-test",
        baseline_import=False,
        occurred_at=occurred_at,
        created_at=occurred_at,
    )


def _route(workspace_id: UUID, module_instance_id: UUID) -> NotificationRoute:
    return NotificationRoute(
        id=uuid4(),
        workspace_id=workspace_id,
        recipient_type="workspace_operator",
        recipient_id="local",
        channel_type="akashic_private",
        channel_module_instance_id=module_instance_id,
        source_ref="src_" + "q" * 32,
        status=NotificationRouteStatus.ACTIVE,
        version=1,
        registered_at=NOW,
        updated_at=NOW,
    )


def _intent(
    workspace_id: UUID,
    fact_id: UUID,
    route_id: UUID,
    module_instance_id: UUID,
) -> NotificationDeliveryIntent:
    return NotificationDeliveryIntent(
        id=uuid4(),
        workspace_id=workspace_id,
        notification_fact_id=fact_id,
        notification_route_id=route_id,
        channel_module_instance_id=module_instance_id,
        delivery_round=1,
        contract_version=1,
        status=NotificationDeliveryStatus.PENDING,
        attempts=1,
        next_attempt_at=NOW,
        claimed_until=None,
        claim_token=None,
        fencing_token=1,
        provider_message_ref=None,
        last_error_code=NotificationAttemptErrorCode.TIMEOUT,
        delivered_at=None,
        exhausted_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _seed_notifications(
    database: Database, workspace_id: UUID
) -> dict[str, UUID]:
    action_a = _task(workspace_id, "Action A")
    action_b = _task(workspace_id, "Action B")
    progress = _task(workspace_id, "Progress")
    archived = _task(workspace_id, "Archived")
    operations = _task(workspace_id, "Channel operations")
    foreign_recipient = _task(workspace_id, "Other recipient")
    definition_id = uuid4()
    module_instance_id = uuid4()
    definition = ModuleDefinition(
        id=definition_id,
        workspace_id=workspace_id,
        module_type="notification_channel",
        provider="query-test-channel",
        interface_version="notification.v1",
        config_schema_version=1,
        capabilities=NotificationChannelCapabilityProfile(
            channel_types=("akashic_private",),
            delivery_contract_versions=(1,),
            idempotency=IdempotencyCapability.OPERATION_ID,
            receipt=NotificationReceiptCapability.PROVIDER_REF,
            max_message_bytes=16_384,
            supports_deep_link=True,
            stability=StabilityLevel.EXPERIMENTAL,
        ),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    instance = ModuleInstance(
        id=module_instance_id,
        workspace_id=workspace_id,
        definition_id=definition_id,
        instance_name="query-test-channel",
        config_version=1,
        config_namespace="notification",
        config={"notification.mode": "test"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )

    action_fact_a = _fact(
        workspace_id,
        action_a.id,
        priority=NotificationPriority.ACTION,
        transition=NotificationTransition.OPENED,
        kind="execution_attention",
        topic="execution_attention",
        occurred_at=NOW - timedelta(hours=3),
        fingerprint="a" * 64,
    )
    action_fact_b = _fact(
        workspace_id,
        action_b.id,
        priority=NotificationPriority.ACTION,
        transition=NotificationTransition.OPENED,
        kind="execution_attention",
        topic="execution_attention",
        occurred_at=NOW - timedelta(hours=1),
        fingerprint="b" * 64,
    )
    progress_fact = _fact(
        workspace_id,
        progress.id,
        priority=NotificationPriority.PROGRESS,
        transition=NotificationTransition.COMPLETED,
        kind="task_milestone_completed",
        topic="task_milestone",
        occurred_at=NOW - timedelta(minutes=30),
        fingerprint="c" * 64,
    )
    archived_fact = _fact(
        workspace_id,
        archived.id,
        priority=NotificationPriority.PROGRESS,
        transition=NotificationTransition.COMPLETED,
        kind="task_milestone_completed",
        topic="task_milestone_archived",
        occurred_at=NOW - timedelta(minutes=20),
        fingerprint="d" * 64,
    )
    foreign_fact = _fact(
        workspace_id,
        foreign_recipient.id,
        priority=NotificationPriority.ACTION,
        transition=NotificationTransition.OPENED,
        kind="execution_attention",
        topic="execution_attention_foreign",
        occurred_at=NOW,
        fingerprint="e" * 64,
        recipient_id="other",
    )
    operations_fact = _fact(
        workspace_id,
        operations.id,
        priority=NotificationPriority.OPERATIONS,
        transition=NotificationTransition.DEGRADED,
        kind="notification_channel_health",
        topic="notification_channel_health",
        occurred_at=NOW - timedelta(minutes=10),
        fingerprint="f" * 64,
    )
    action_thread_a = InboxThread.open_from_fact(
        action_fact_a, thread_id=uuid4(), now=action_fact_a.created_at
    )
    action_thread_b = InboxThread.open_from_fact(
        action_fact_b, thread_id=uuid4(), now=action_fact_b.created_at
    )
    progress_thread = InboxThread.open_from_fact(
        progress_fact, thread_id=uuid4(), now=progress_fact.created_at
    )
    archived_thread = InboxThread.open_from_fact(
        archived_fact, thread_id=uuid4(), now=archived_fact.created_at
    ).archive(now=NOW)
    foreign_thread = InboxThread.open_from_fact(
        foreign_fact, thread_id=uuid4(), now=foreign_fact.created_at
    )
    operations_thread = InboxThread.open_from_fact(
        operations_fact, thread_id=uuid4(), now=operations_fact.created_at
    )
    route = _route(workspace_id, module_instance_id)
    intent = _intent(
        workspace_id, action_fact_b.id, route.id, module_instance_id
    )
    attempt = NotificationDeliveryAttempt.open(
        delivery_intent_id=intent.id,
        attempt_number=1,
        claim_token=uuid4(),
        fencing_token=1,
        started_at=NOW,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        for item in (
            action_a,
            action_b,
            progress,
            archived,
            operations,
            foreign_recipient,
        ):
            uow.tasks.add(item)
        uow.modules.add_definition(definition)
        uow.modules.add_instance(instance)
        for fact in (
            action_fact_a,
            action_fact_b,
            progress_fact,
            archived_fact,
            foreign_fact,
            operations_fact,
        ):
            uow.notifications.add_fact_or_get_existing(fact)
        for thread in (
            action_thread_a,
            action_thread_b,
            progress_thread,
            archived_thread,
            foreign_thread,
            operations_thread,
        ):
            uow.notifications.add_thread(thread)
        uow.notifications.add_route(route)
        uow.notifications.add_intent_or_get_existing(intent)
        uow.notifications.add_attempt(workspace_id, attempt)
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="notification_thread",
            resource_id=action_thread_b.id,
            change_type="notification.created",
            resource_version=action_thread_b.version,
            correlation_id=uuid4(),
        )
    return {
        "action_a": action_thread_a.id,
        "action_b": action_thread_b.id,
        "progress": progress_thread.id,
        "archived": archived_thread.id,
        "operations": operations_thread.id,
        "route": route.id,
        "delivery": intent.id,
    }


def test_notification_inbox_is_scoped_and_paginates_stably(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_notifications(database, workspace_id)

    summary = client.get(f"/api/v1/workspaces/{workspace_id}/notification-summary")
    assert summary.status_code == 200, summary.text
    assert summary.json()["recipient_type"] == "workspace_operator"
    assert summary.json()["recipient_id"] == "local"
    assert summary.json()["open_count"] == 2
    assert summary.json()["unread_count"] == 4
    assert summary.json()["progress_count"] == 1
    assert summary.json()["channel_issue_count"] == 1
    assert summary.json()["last_change_cursor"] >= 1

    first = client.get(
        f"/api/v1/workspaces/{workspace_id}/notification-threads",
        params={"category": "action", "limit": 1},
    )
    assert first.status_code == 200, first.text
    assert first.json()["items"][0]["id"] == str(ids["action_b"])
    cursor = first.json()["next_cursor"]
    assert cursor
    second = client.get(
        f"/api/v1/workspaces/{workspace_id}/notification-threads",
        params={"category": "action", "limit": 1, "cursor": cursor},
    )
    assert second.status_code == 200, second.text
    assert second.json()["items"][0]["id"] == str(ids["action_a"])

    archived = client.get(
        f"/api/v1/workspaces/{workspace_id}/notification-threads",
        params={"category": "archived"},
    )
    assert archived.status_code == 200, archived.text
    assert [item["id"] for item in archived.json()["items"]] == [
        str(ids["archived"])
    ]

    unread = client.get(
        f"/api/v1/workspaces/{workspace_id}/notification-threads",
        params={"category": "action", "unread_only": "true"},
    )
    assert unread.status_code == 200, unread.text
    assert len(unread.json()["items"]) == 2


def test_notification_thread_detail_and_attempts_are_safe_and_route_scoped(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_notifications(database, workspace_id)

    detail = client.get(
        f"/api/v1/workspaces/{workspace_id}/notification-threads/{ids['action_b']}"
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["thread"]["id"] == str(ids["action_b"])
    assert body["facts"]["items"]
    assert body["deliveries"][0]["status"] == "pending"
    assert body["notification_ref"].startswith("NTF-")
    assert "mark_read" in body["available_commands"]
    assert body["deep_link"].startswith("/tasks/")
    assert "source_ref" not in str(body)
    assert "claim_token" not in str(body)
    assert "provider_message_ref" not in str(body)

    attempts = client.get(
        f"/api/v1/workspaces/{workspace_id}/notification-deliveries/"
        f"{ids['delivery']}/attempts"
    )
    assert attempts.status_code == 200, attempts.text
    assert attempts.json()["items"][0]["attempt_number"] == 1
    assert "source_ref" not in str(attempts.json())

    route_threads = client.get(
        f"/api/v1/workspaces/{workspace_id}/notification-routes/{ids['route']}/threads"
    )
    assert route_threads.status_code == 200, route_threads.text
    assert [item["id"] for item in route_threads.json()["items"]] == [
        str(ids["action_b"])
    ]
    assert "source_ref" not in str(route_threads.json())

    missing_route = client.get(
        f"/api/v1/workspaces/{workspace_id}/notification-routes/{uuid4()}/threads"
    )
    assert missing_route.status_code == 404, missing_route.text


def test_notification_route_lookup_fails_closed_when_api_is_not_loopback(
    database_url: str,
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_notifications(database, workspace_id)
    with TestClient(
        create_app(
            Settings(
                database_url=database_url,
                local_actor_id="operator",
                api_bind_host="0.0.0.0",
            )
        )
    ) as client:
        response = client.get(
            f"/api/v1/workspaces/{workspace_id}/notification-routes/"
            f"{ids['route']}/threads",
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "adapter_workflow_requires_loopback"


def test_notification_inbox_rejects_invalid_limits_and_cursors(
    client: TestClient,
    workspace_id: UUID,
) -> None:
    invalid_limit = client.get(
        f"/api/v1/workspaces/{workspace_id}/notification-threads",
        params={"limit": 201},
    )
    assert invalid_limit.status_code == 422
    invalid_cursor = client.get(
        f"/api/v1/workspaces/{workspace_id}/notification-threads",
        params={"cursor": "not-a-cursor"},
    )
    assert invalid_cursor.status_code == 422
