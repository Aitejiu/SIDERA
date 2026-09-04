"""Provider-free R6.3.3 notification gateway vertical."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

from sidera_control_plane.api.app import create_app
from sidera_control_plane.api.dependencies import get_notification_delivery_service
from sidera_control_plane.application.notification_delivery import (
    NotificationDeliveryService,
)
from sidera_control_plane.application.notification_reconciler import (
    NotificationReconciler,
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
from sidera_control_plane.persistence.models import (
    NotificationDeliveryIntentModel,
    NotificationFactModel,
    NotificationSourceBindingModel,
    NotificationThreadModel,
    PlanningRequestModel,
    ProjectionChangeModel,
    ProposalMaterializationModel,
    TaskModel,
    WorkspaceProjectionCursorModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.e2e.seed_r6_3_1_collaboration import seed_collaboration_plan

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("clean_database"),
]

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
DELIVERY_NOW = NOW + timedelta(seconds=600)
PRIVATE_CHAT_ID = "telegram-chat-private-42"
PRIVATE_SECRET = "telegram-secret-must-never-cross-boundary"


@pytest.fixture
def client(database_url: str, database) -> Iterator[TestClient]:
    settings = Settings(
        database_url=database_url,
        api_bind_host="127.0.0.1",
        local_actor_id="operator",
        web_origin="http://testserver",
    )
    app = create_app(settings)
    app.dependency_overrides[get_notification_delivery_service] = lambda: (
        NotificationDeliveryService(
            lambda: SqlAlchemyUnitOfWork(database),
            clock=lambda: DELIVERY_NOW,
        )
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _notification_channel(
    database, workspace_id: UUID
) -> tuple[UUID, UUID]:
    definition_id = uuid4()
    instance_id = uuid4()
    definition = ModuleDefinition(
        id=definition_id,
        workspace_id=workspace_id,
        module_type="notification_channel",
        provider="akashic",
        interface_version="sidera.notification.v1",
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
        id=instance_id,
        workspace_id=workspace_id,
        definition_id=definition_id,
        instance_name="r6-3-3-akashic-private",
        config_version=1,
        config_namespace="notification",
        config={"notification.mode": "gateway"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.modules.add_definition(definition)
        uow.modules.add_instance(instance)
    return definition_id, instance_id


def _command(
    workspace_id: UUID,
    command_type: str,
    target_type: str,
    *,
    payload: dict[str, object],
    target_id: UUID | None = None,
    expected_version: int | None = None,
) -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": target_type,
        "actor": {"type": "local", "id": "operator"},
        "target_id": str(target_id) if target_id else None,
        "expected_version": expected_version,
        "idempotency_key": f"r6-3-3-{uuid4().hex}",
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def _set_task_status(
    database,
    workspace_id: UUID,
    task_id: UUID,
    *,
    status: str,
    change_type: str,
) -> int:
    with database.session() as session:
        row = session.scalar(
            select(TaskModel).where(
                TaskModel.workspace_id == workspace_id,
                TaskModel.id == task_id,
            )
        )
        if row is None:
            raise AssertionError(f"task {task_id} is missing")
        next_version = row.version + 1
        session.execute(
            update(TaskModel)
            .where(
                TaskModel.workspace_id == workspace_id,
                TaskModel.id == task_id,
            )
            .values(status=status, version=next_version)
        )
        session.commit()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="task_overview",
            resource_id=task_id,
            change_type=change_type,
            resource_version=next_version,
            correlation_id=uuid4(),
        )
    return next_version


def _ledger_counts(database, workspace_id: UUID) -> tuple[int, int, int]:
    with database.session() as session:
        return (
            int(
                session.scalar(
                    select(func.count()).select_from(NotificationFactModel).where(
                        NotificationFactModel.workspace_id == workspace_id
                    )
                )
                or 0
            ),
            int(
                session.scalar(
                    select(func.count()).select_from(NotificationThreadModel).where(
                        NotificationThreadModel.workspace_id == workspace_id
                    )
                )
                or 0
            ),
            int(
                session.scalar(
                    select(func.count())
                    .select_from(NotificationDeliveryIntentModel)
                    .where(
                        NotificationDeliveryIntentModel.workspace_id == workspace_id
                    )
                )
                or 0
            ),
        )


def _assert_private_identity_absent(value: Any) -> None:
    serialized = json.dumps(value, default=str, ensure_ascii=False)
    assert PRIVATE_CHAT_ID not in serialized
    assert PRIVATE_SECRET not in serialized


def test_r6_3_3_provider_free_gateway_vertical_recovers_and_isolates_siblings(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _, module_instance_id = _notification_channel(database, workspace_id)
    seed = seed_collaboration_plan(database, workspace_id, now=NOW)
    source_ref = "src_" + uuid4().hex

    with database.session() as session:
        session.execute(
            update(PlanningRequestModel)
            .where(
                PlanningRequestModel.workspace_id == workspace_id,
                PlanningRequestModel.id == seed.planning_request_id,
            )
            .values(source_type="akashic_private", source_ref=source_ref)
        )
        session.add(
            ProposalMaterializationModel(
                id=uuid4(),
                workspace_id=workspace_id,
                planning_request_id=seed.planning_request_id,
                kind="create",
                proposal_revision_id=seed.proposal_id,
                root_task_id=seed.root_task_id,
                plan_snapshot_id=seed.plan_snapshot_id,
                plan_generation=1,
                subtask_task_mapping={
                    key: str(value) for key, value in seed.subtask_ids.items()
                },
                worker_item_mapping={
                    key: str(value) for key, value in seed.worker_item_ids.items()
                },
                execution_request_mapping={
                    key: str(value) for key, value in seed.request_ids.items()
                },
                deep_link=(
                    f"/workspaces/{workspace_id}/tasks/{seed.root_task_id}"
                ),
                confirmed_by="operator",
                created_at=NOW,
            )
        )
        session.commit()

    with database.session() as session:
        session.execute(
            delete(ProjectionChangeModel).where(
                ProjectionChangeModel.workspace_id == workspace_id
            )
        )
        session.execute(
            delete(WorkspaceProjectionCursorModel).where(
                WorkspaceProjectionCursorModel.workspace_id == workspace_id
            )
        )
        session.commit()

    registered = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "RegisterNotificationRoute",
            "notification_route",
            payload={
                "channel_module_instance_id": str(module_instance_id),
                "channel_type": "akashic_private",
                "recipient_type": "workspace_operator",
                "recipient_id": "local",
                "source_ref": source_ref,
            },
        ),
    )
    assert registered.status_code == 200, registered.text
    route_id = UUID(registered.json()["data"]["notification_route_id"])
    _assert_private_identity_absent(registered.json())

    with database.session() as session:
        request = session.scalar(
            select(PlanningRequestModel).where(
                PlanningRequestModel.id == seed.planning_request_id,
                PlanningRequestModel.workspace_id == workspace_id,
            )
        )
        assert request is not None
        assert request.status == "materialized"
        materialization = session.scalar(
            select(ProposalMaterializationModel).where(
                ProposalMaterializationModel.workspace_id == workspace_id,
                ProposalMaterializationModel.planning_request_id
                == seed.planning_request_id,
            )
        )
        assert materialization is not None
        assert materialization.root_task_id == seed.root_task_id
        bindings = session.scalars(
            select(NotificationSourceBindingModel).where(
                NotificationSourceBindingModel.workspace_id == workspace_id,
                NotificationSourceBindingModel.notification_route_id == route_id,
            )
        ).all()
        assert {
            (item.resource_type, item.resource_id)
            for item in bindings
        } == {
            ("planning_request", seed.planning_request_id),
            ("root_task", seed.root_task_id),
        }
        _assert_private_identity_absent(
            [
                {
                    "source_type": request.source_type,
                    "source_ref": request.source_ref,
                    "binding": item.resource_type,
                }
                for item in bindings
            ]
        )

    _set_task_status(
        database,
        workspace_id,
        seed.root_task_id,
        status="blocked",
        change_type="task_blocked",
    )
    reconciler = NotificationReconciler(
        uow_factory,
        clock=lambda: NOW,
        owner="r6-3-3-vertical-live",
        batch_size=200,
    )
    live = reconciler.reconcile_workspace(workspace_id)
    assert live.diagnostic_code is None
    assert live.acquired is True

    due = reconciler.scan_due(
        workspace_id,
        owner_id="r6-3-3-vertical-due",
        now=NOW + timedelta(seconds=600),
        limit=200,
    )
    assert due.diagnostic_code is None
    assert due.facts_created == 1
    assert due.intents_created == 1

    page = client.get(
        f"/api/v1/workspaces/{workspace_id}/notification-threads",
        params={"category": "action", "root_task_id": str(seed.root_task_id)},
    )
    assert page.status_code == 200, page.text
    assert len(page.json()["items"]) == 1
    thread = page.json()["items"][0]
    thread_id = UUID(thread["id"])
    assert thread["business_status"] == "open"
    assert thread["unread_count"] == 1
    assert thread["deep_link"] == (
        f"/workspaces/{workspace_id}/tasks/{seed.root_task_id}"
    )

    detail_response = client.get(
        f"/api/v1/workspaces/{workspace_id}/notification-threads/{thread_id}"
    )
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert detail["deep_link"] == thread["deep_link"]
    assert len(detail["deliveries"]) == 1
    _assert_private_identity_absent(detail)

    claim_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/notification-deliveries/claims",
        json={
            "module_instance_id": str(module_instance_id),
            "supported_contract_versions": [1],
            "batch_size": 10,
            "lease_seconds": 30,
        },
    )
    assert claim_response.status_code == 200, claim_response.text
    claimed = claim_response.json()["deliveries"]
    assert len(claimed) == 1
    envelope = claimed[0]
    assert envelope["source_ref"] == source_ref
    assert envelope["notification_ref"].startswith("NTF-")
    sink_receipts = [
        {
            "notification_ref": envelope["notification_ref"],
            "deep_link": envelope["deep_link"],
        }
    ]
    assert sink_receipts[0]["deep_link"] == detail["deep_link"]
    _assert_private_identity_absent(envelope)

    ack = client.post(
        f"/api/v1/workspaces/{workspace_id}/notification-deliveries/"
        f"{envelope['delivery_id']}/ack",
        json={
            "claim_token": envelope["claim_token"],
            "fencing_token": envelope["fencing_token"],
            "completed_at": (DELIVERY_NOW + timedelta(seconds=1)).isoformat(),
            "provider_message_ref": "fake-sink-message-1",
        },
    )
    assert ack.status_code == 200, ack.text
    assert ack.json()["status"] == "delivered"

    while_open = client.get(detail_response.request.url)
    assert while_open.status_code == 200, while_open.text
    assert while_open.json()["thread"]["business_status"] == "open"
    assert while_open.json()["thread"]["unread_count"] == 1
    assert while_open.json()["deliveries"][0]["status"] == "delivered"

    mark_read = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "MarkNotificationThreadRead",
            "notification_thread",
            target_id=thread_id,
            expected_version=while_open.json()["thread"]["version"],
            payload={
                "expected_latest_fact_id": while_open.json()["thread"]["latest_fact_id"]
            },
        ),
    )
    assert mark_read.status_code == 200, mark_read.text
    assert mark_read.json()["data"]["unread_count"] == 0

    sibling_id = seed.subtask_ids["upstream-a"]
    with database.session() as session:
        sibling_before = session.scalar(
            select(TaskModel).where(
                TaskModel.workspace_id == workspace_id,
                TaskModel.id == sibling_id,
            )
        )
        assert sibling_before is not None
        sibling_snapshot = (sibling_before.status, sibling_before.version)

    _set_task_status(
        database,
        workspace_id,
        seed.root_task_id,
        status="running",
        change_type="task_blocked_recovered",
    )
    recovery_reconciler = NotificationReconciler(
        uow_factory,
        clock=lambda: DELIVERY_NOW,
        owner="r6-3-3-vertical-recovery",
        batch_size=200,
    )
    recovered = recovery_reconciler.reconcile_workspace(workspace_id)
    assert recovered.diagnostic_code is None
    assert recovered.facts_created == 1
    assert recovered.intents_created == 1

    recovered_detail = client.get(detail_response.request.url)
    assert recovered_detail.status_code == 200, recovered_detail.text
    recovery_body = recovered_detail.json()
    assert recovery_body["thread"]["business_status"] == "resolved"
    assert recovery_body["thread"]["deep_link"] == detail["deep_link"]
    assert any(
        item["transition"] == "recovered"
        for item in recovery_body["facts"]["items"]
    )
    assert sum(
        item["status"] == "pending" for item in recovery_body["deliveries"]
    ) == 1

    with database.session() as session:
        recovery_intent = session.scalar(
            select(NotificationDeliveryIntentModel)
            .join(
                NotificationFactModel,
                NotificationFactModel.id
                == NotificationDeliveryIntentModel.notification_fact_id,
            )
            .where(
                NotificationDeliveryIntentModel.workspace_id == workspace_id,
                NotificationFactModel.transition == "recovered",
            )
        )
        assert recovery_intent is not None
        assert recovery_intent.notification_route_id == route_id

    recovery_claim = client.post(
        f"/api/v1/workspaces/{workspace_id}/notification-deliveries/claims",
        json={
            "module_instance_id": str(module_instance_id),
            "supported_contract_versions": [1],
            "batch_size": 10,
            "lease_seconds": 30,
        },
    )
    assert recovery_claim.status_code == 200, recovery_claim.text
    recovery_envelope = recovery_claim.json()["deliveries"]
    assert len(recovery_envelope) == 1
    assert recovery_envelope[0]["source_ref"] == source_ref
    _assert_private_identity_absent(recovery_envelope[0])

    before_replay = _ledger_counts(database, workspace_id)
    restarted = NotificationReconciler(
        uow_factory,
        clock=lambda: DELIVERY_NOW,
        owner="r6-3-3-vertical-restarted",
        batch_size=200,
    )
    assert restarted.reconcile_workspace(workspace_id).facts_created == 0
    assert restarted.reconcile_workspace(workspace_id).facts_created == 0
    assert _ledger_counts(database, workspace_id) == before_replay

    provider_failure = client.post(
        f"/api/v1/workspaces/{workspace_id}/notification-deliveries/"
        f"{recovery_envelope[0]['delivery_id']}/nack",
        json={
            "claim_token": recovery_envelope[0]["claim_token"],
            "fencing_token": recovery_envelope[0]["fencing_token"],
            "outcome": "retryable_failure",
            "stable_error_code": "provider_unavailable",
        },
    )
    assert provider_failure.status_code == 200, provider_failure.text
    assert provider_failure.json()["status"] == "retry_wait"

    with database.session() as session:
        sibling_after = session.scalar(
            select(TaskModel).where(
                TaskModel.workspace_id == workspace_id,
                TaskModel.id == sibling_id,
            )
        )
        assert sibling_after is not None
        assert (sibling_after.status, sibling_after.version) == sibling_snapshot

    route_threads = client.get(
        f"/api/v1/workspaces/{workspace_id}/notification-routes/{route_id}/threads"
    )
    assert route_threads.status_code == 200, route_threads.text
    assert thread_id in {UUID(item["id"]) for item in route_threads.json()["items"]}
    _assert_private_identity_absent(route_threads.json())
