from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.notification_delivery import (
    NotificationDeliveryService,
)
from sidera_control_plane.domain.errors import StaleFencingToken
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
    NotificationDeliveryIntent,
    NotificationDeliveryStatus,
    NotificationFact,
    NotificationPriority,
    NotificationRoute,
    NotificationRouteStatus,
    NotificationTransition,
    notification_thread_id,
    notification_thread_key,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 4, 14, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Seed:
    module_id: UUID
    route_id: UUID
    fact_id: UUID
    intent_id: UUID


def _seed_delivery(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork], workspace_id: UUID
) -> _Seed:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="并发投递测试",
        description="验证 claim fencing",
        acceptance_criteria=("每个 intent 只被一个 adapter claim",),
        priority=50,
        requested_by="test:delivery-concurrency",
    )
    definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=workspace_id,
        module_type="notification_channel",
        provider="concurrency-test-channel",
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
        id=uuid4(),
        workspace_id=workspace_id,
        definition_id=definition.id,
        instance_name="concurrency-test-channel",
        config_version=1,
        config_namespace="notification",
        config={"notification.mode": "test"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    fact = NotificationFact.create(
        workspace_id=workspace_id,
        policy_version=1,
        recipient_type="workspace_operator",
        recipient_id="local",
        kind="execution_attention",
        priority=NotificationPriority.ACTION,
        transition=NotificationTransition.OPENED,
        topic="execution_attention",
        root_task_id=task.id,
        subtask_id=None,
        worker_item_id=None,
        source_projection_cursor=1,
        source_projection_type="task_overview",
        source_resource_id=task.id,
        source_resource_version=1,
        authoritative_fingerprint="a" * 64,
        title=task.title,
        summary="任务需要关注。",
        action="打开任务查看",
        deep_link=f"/workspaces/{workspace_id}/tasks/{task.id}",
        correlation_id="corr-concurrency-test",
        baseline_import=False,
        occurred_at=NOW,
        created_at=NOW,
    )
    thread_key = notification_thread_key(
        recipient_type=fact.recipient_type,
        recipient_id=fact.recipient_id,
        root_task_id=fact.root_task_id,
        subtask_id=fact.subtask_id,
        worker_item_id=fact.worker_item_id,
        topic=fact.topic,
    )
    thread = InboxThread.open_from_fact(
        fact, thread_id=notification_thread_id(thread_key), now=NOW
    )
    route = NotificationRoute(
        id=uuid4(),
        workspace_id=workspace_id,
        recipient_type="workspace_operator",
        recipient_id="local",
        channel_type="akashic_private",
        channel_module_instance_id=instance.id,
        source_ref="src_" + "c" * 32,
        status=NotificationRouteStatus.ACTIVE,
        version=1,
        registered_at=NOW,
        updated_at=NOW,
    )
    intent = NotificationDeliveryIntent(
        id=uuid4(),
        workspace_id=workspace_id,
        notification_fact_id=fact.id,
        notification_route_id=route.id,
        channel_module_instance_id=instance.id,
        delivery_round=1,
        contract_version=1,
        status=NotificationDeliveryStatus.PENDING,
        attempts=0,
        next_attempt_at=NOW,
        claimed_until=None,
        claim_token=None,
        fencing_token=1,
        provider_message_ref=None,
        last_error_code=None,
        delivered_at=None,
        exhausted_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    with uow_factory() as uow:
        uow.tasks.add(task)
        uow.modules.add_definition(definition)
        uow.modules.add_instance(instance)
        uow.notifications.add_fact_or_get_existing(fact)
        uow.notifications.add_thread(thread)
        uow.notifications.add_route(route)
        uow.notifications.add_intent_or_get_existing(intent)
    return _Seed(instance.id, route.id, fact.id, intent.id)


def _claim(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork], workspace_id: UUID, module_id: UUID
):
    return NotificationDeliveryService(uow_factory, clock=lambda: NOW).claim(
        workspace_id,
        module_id,
        supported_contract_versions=(1,),
        batch_size=50,
        lease_seconds=30,
    )


def test_concurrent_adapters_claim_disjoint_rows(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
    clean_database: None,
) -> None:
    del clean_database
    seed = _seed_delivery(uow_factory, workspace_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_claim, uow_factory, workspace_id, seed.module_id),
            executor.submit(_claim, uow_factory, workspace_id, seed.module_id),
        ]
        results = [future.result() for future in futures]

    claimed = [item for result in results for item in result]
    assert len(claimed) == 1
    assert claimed[0].delivery_id == seed.intent_id


def test_expiry_reclaim_rejects_old_ack_and_preserves_at_least_once_identity(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
    clean_database: None,
) -> None:
    del clean_database
    seed = _seed_delivery(uow_factory, workspace_id)
    service = NotificationDeliveryService(uow_factory, clock=lambda: NOW)
    first = service.claim(
        workspace_id,
        seed.module_id,
        supported_contract_versions=(1,),
        batch_size=50,
        lease_seconds=5,
    )[0]

    service.recover_expired_claims(
        workspace_id, now=NOW + timedelta(seconds=6), limit=50
    )
    with pytest.raises(StaleFencingToken):
        service.ack(
            workspace_id,
            seed.intent_id,
            claim_token=first.claim_token,
            fencing_token=first.fencing_token,
            completed_at=NOW + timedelta(seconds=6),
            provider_message_ref="provider_msg_1",
        )

    second = service.claim(
        workspace_id,
        seed.module_id,
        supported_contract_versions=(1,),
        batch_size=50,
        lease_seconds=30,
        now=NOW + timedelta(seconds=6),
    )[0]
    assert second.idempotency_key == first.idempotency_key
    assert second.notification_ref == first.notification_ref
    assert second.title == first.title
    assert second.summary == first.summary
    assert second.fencing_token > first.fencing_token


def test_disabled_route_does_not_create_a_retry_attempt(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
    clean_database: None,
) -> None:
    del clean_database
    seed = _seed_delivery(uow_factory, workspace_id)
    service = NotificationDeliveryService(uow_factory, clock=lambda: NOW)
    envelope = service.claim(
        workspace_id,
        seed.module_id,
        supported_contract_versions=(1,),
        batch_size=50,
        lease_seconds=30,
    )[0]
    with uow_factory() as uow:
        route = uow.notifications.get_route(workspace_id, seed.route_id)
        assert route is not None
        uow.notifications.save_route(
            route.with_status(NotificationRouteStatus.DISABLED, now=NOW),
            route.version,
        )

    result = service.nack(
        workspace_id,
        seed.intent_id,
        claim_token=envelope.claim_token,
        fencing_token=envelope.fencing_token,
        outcome="retryable_failure",
        error_code="timeout",
        now=NOW + timedelta(seconds=1),
    )
    assert result.status is NotificationDeliveryStatus.SUPPRESSED
