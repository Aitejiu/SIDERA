from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.notification_delivery import (
    NotificationDeliveryService,
)
from sidera_control_plane.domain.errors import (
    InvalidTransition,
    StaleFencingToken,
    ValidationError,
)
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
    NotificationAttemptOutcome,
    NotificationDeliveryAttempt,
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

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")


@dataclass
class _Fixture:
    uow: _Uow
    module: ModuleInstance
    route: NotificationRoute
    fact: NotificationFact
    intent: NotificationDeliveryIntent


class _Modules:
    def __init__(self, definition: ModuleDefinition, instance: ModuleInstance) -> None:
        self.definition = definition
        self.instance = instance

    def get_instance(self, module_id: UUID, workspace_id: UUID):
        if module_id == self.instance.id and workspace_id == self.instance.workspace_id:
            return self.instance
        return None

    def get_definition(self, module_id: UUID, workspace_id: UUID):
        if module_id == self.definition.id and workspace_id == self.definition.workspace_id:
            return self.definition
        return None


class _Tasks:
    def __init__(self, tasks: tuple[Task, ...]) -> None:
        self._tasks = {task.id: task for task in tasks}

    def get(self, task_id: UUID, workspace_id: UUID, *, for_update: bool = False):
        del for_update
        task = self._tasks.get(task_id)
        return task if task is not None and task.workspace_id == workspace_id else None


class _WorkerItems:
    def get(self, item_id: UUID, workspace_id: UUID, *, for_update: bool = False):
        del item_id, workspace_id, for_update


class _Notifications:
    def __init__(
        self,
        fact: NotificationFact,
        thread: InboxThread,
        route: NotificationRoute,
        intent: NotificationDeliveryIntent,
    ) -> None:
        self.facts = {fact.id: fact}
        self.threads = {thread.id: thread}
        self.routes = {route.id: route}
        self.intents = {intent.id: intent}
        self.attempts: dict[tuple[UUID, int], NotificationDeliveryAttempt] = {}

    def get_fact(self, workspace_id: UUID, fact_id: UUID):
        fact = self.facts.get(fact_id)
        return fact if fact is not None and fact.workspace_id == workspace_id else None

    def get_thread(self, workspace_id: UUID, thread_id: UUID, *, for_update: bool = False):
        del for_update
        thread = self.threads.get(thread_id)
        return thread if thread is not None and thread.workspace_id == workspace_id else None

    def get_route(self, workspace_id: UUID, route_id: UUID, *, for_update: bool = False):
        del for_update
        route = self.routes.get(route_id)
        return route if route is not None and route.workspace_id == workspace_id else None

    def list_due_intents(
        self,
        workspace_id: UUID,
        channel_module_instance_id: UUID,
        now: datetime,
        limit: int,
        *,
        for_update_skip_locked: bool = False,
    ):
        del for_update_skip_locked
        due = [
            intent
            for intent in self.intents.values()
            if intent.workspace_id == workspace_id
            and intent.channel_module_instance_id == channel_module_instance_id
            and intent.status
            in {NotificationDeliveryStatus.PENDING, NotificationDeliveryStatus.RETRY_WAIT}
            and intent.next_attempt_at <= now
        ]
        due.sort(key=lambda intent: (intent.next_attempt_at, intent.created_at, intent.id))
        return tuple(due[:limit])

    def list_expired_claims(
        self,
        workspace_id: UUID,
        now: datetime,
        limit: int,
        *,
        for_update_skip_locked: bool = False,
    ):
        del for_update_skip_locked
        expired = [
            intent
            for intent in self.intents.values()
            if intent.workspace_id == workspace_id
            and intent.status is NotificationDeliveryStatus.CLAIMED
            and intent.claimed_until is not None
            and intent.claimed_until <= now
        ]
        expired.sort(key=lambda intent: (intent.claimed_until, intent.id))
        return tuple(expired[:limit])

    def get_intent(self, workspace_id: UUID, intent_id: UUID, *, for_update: bool = False):
        del for_update
        intent = self.intents.get(intent_id)
        return intent if intent is not None and intent.workspace_id == workspace_id else None

    def save_intent(
        self,
        intent: NotificationDeliveryIntent,
        previous_fencing_token: int,
        expected_status: NotificationDeliveryStatus | str,
    ) -> None:
        current = self.intents[intent.id]
        expected = (
            expected_status
            if isinstance(expected_status, NotificationDeliveryStatus)
            else NotificationDeliveryStatus(expected_status)
        )
        assert current.fencing_token == previous_fencing_token
        assert current.status is expected
        self.intents[intent.id] = intent

    def add_attempt(self, workspace_id: UUID, attempt: NotificationDeliveryAttempt) -> None:
        assert self.intents[attempt.delivery_intent_id].workspace_id == workspace_id
        self.attempts[(attempt.delivery_intent_id, attempt.attempt_number)] = attempt

    def list_attempts(self, workspace_id: UUID, intent_id: UUID):
        assert self.intents[intent_id].workspace_id == workspace_id
        return tuple(
            attempt
            for (delivery_id, _number), attempt in sorted(self.attempts.items())
            if delivery_id == intent_id
        )

    def finish_attempt(self, workspace_id: UUID, attempt: NotificationDeliveryAttempt) -> None:
        assert self.intents[attempt.delivery_intent_id].workspace_id == workspace_id
        key = (attempt.delivery_intent_id, attempt.attempt_number)
        current = self.attempts[key]
        assert current.outcome is None
        self.attempts[key] = attempt


class _Uow(AbstractContextManager["_Uow"]):
    def __init__(
        self,
        notifications: _Notifications,
        modules: _Modules,
        tasks: _Tasks,
        worker_items: _WorkerItems,
    ) -> None:
        self.notifications = notifications
        self.modules = modules
        self.tasks = tasks
        self.worker_items = worker_items

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback

    def flush(self) -> None:
        return None


def _fixture() -> _Fixture:
    root_task = Task.create(
        task_id=UUID("10000000-0000-0000-0000-000000000001"),
        workspace_id=WORKSPACE_ID,
        title="实现通知网关",
        description="验证通知投递",
        acceptance_criteria=("可安全投递",),
        priority=50,
        requested_by="test:delivery",
    )
    definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        module_type="notification_channel",
        provider="test-provider",
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
    module = ModuleInstance(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        definition_id=definition.id,
        instance_name="test-channel",
        config_version=1,
        config_namespace="notification",
        config={"notification.mode": "test"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    fact = NotificationFact.create(
        workspace_id=WORKSPACE_ID,
        policy_version=1,
        recipient_type="workspace_operator",
        recipient_id="local",
        kind="execution_attention",
        priority=NotificationPriority.ACTION,
        transition=NotificationTransition.OPENED,
        topic="execution_attention",
        root_task_id=root_task.id,
        subtask_id=None,
        worker_item_id=None,
        source_projection_cursor=10,
        source_projection_type="task_overview",
        source_resource_id=root_task.id,
        source_resource_version=1,
        authoritative_fingerprint="a" * 64,
        title=root_task.title,
        summary="任务需要关注。",
        action="打开任务查看",
        deep_link=f"/workspaces/{WORKSPACE_ID}/tasks/{root_task.id}",
        correlation_id="corr-delivery-test",
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
        workspace_id=WORKSPACE_ID,
        recipient_type="workspace_operator",
        recipient_id="local",
        channel_type="akashic_private",
        channel_module_instance_id=module.id,
        source_ref="src_" + "a" * 32,
        status=NotificationRouteStatus.ACTIVE,
        version=1,
        registered_at=NOW,
        updated_at=NOW,
    )
    intent = NotificationDeliveryIntent(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        notification_fact_id=fact.id,
        notification_route_id=route.id,
        channel_module_instance_id=module.id,
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
    notifications = _Notifications(fact, thread, route, intent)
    uow = _Uow(
        notifications,
        _Modules(definition, module),
        _Tasks((root_task,)),
        _WorkerItems(),
    )
    return _Fixture(uow, module, route, fact, intent)


def _service(fixture: _Fixture, **kwargs: object) -> NotificationDeliveryService:
    return NotificationDeliveryService(lambda: fixture.uow, clock=lambda: NOW, **kwargs)


def test_claim_returns_strict_envelope_and_increments_fencing_token() -> None:
    fixture = _fixture()

    deliveries = _service(fixture).claim(
        WORKSPACE_ID,
        fixture.module.id,
        supported_contract_versions=(1,),
        batch_size=50,
        lease_seconds=30,
        now=NOW,
    )

    assert len(deliveries) == 1
    envelope = deliveries[0]
    assert set(envelope.__dataclass_fields__) == {
        "delivery_id",
        "notification_id",
        "thread_id",
        "contract_version",
        "claim_token",
        "fencing_token",
        "idempotency_key",
        "channel_type",
        "source_ref",
        "kind",
        "priority",
        "root_task_title",
        "subtask_path",
        "worker_item_title",
        "title",
        "summary",
        "next_action",
        "deep_link",
        "correlation_id",
        "notification_ref",
        "created_at",
    }
    assert envelope.delivery_id == fixture.intent.id
    assert envelope.notification_id == fixture.fact.id
    assert envelope.thread_id in fixture.uow.notifications.threads
    assert envelope.fencing_token == 2
    assert envelope.claim_token is not None
    assert envelope.idempotency_key == f"sidera:notification-delivery:{fixture.intent.id}"
    assert envelope.root_task_title == "实现通知网关"
    assert envelope.subtask_path == ()
    assert envelope.worker_item_title is None
    assert "test-provider" not in repr(envelope)
    assert fixture.uow.notifications.intents[fixture.intent.id].attempts == 1
    assert len(fixture.uow.notifications.attempts) == 1


def test_claim_rejects_unsupported_contract_and_does_not_claim_draining_module() -> None:
    fixture = _fixture()
    service = _service(fixture)

    with pytest.raises(ValidationError):
        service.claim(
            WORKSPACE_ID,
            fixture.module.id,
            supported_contract_versions=(2,),
            batch_size=50,
            lease_seconds=30,
            now=NOW,
        )

    fixture.uow.modules.instance = replace(
        fixture.uow.modules.instance, lifecycle=ModuleLifecycle.DRAINING
    )
    assert service.claim(
        WORKSPACE_ID,
        fixture.module.id,
        supported_contract_versions=(1,),
        batch_size=50,
        lease_seconds=30,
        now=NOW,
    ) == ()
    assert fixture.uow.notifications.intents[fixture.intent.id].status is NotificationDeliveryStatus.PENDING


def test_duplicate_ack_is_idempotent_and_stale_claim_is_rejected() -> None:
    fixture = _fixture()
    service = _service(fixture)
    envelope = service.claim(
        WORKSPACE_ID,
        fixture.module.id,
        supported_contract_versions=(1,),
        batch_size=50,
        lease_seconds=30,
        now=NOW,
    )[0]

    first = service.ack(
        WORKSPACE_ID,
        fixture.intent.id,
        claim_token=envelope.claim_token,
        fencing_token=envelope.fencing_token,
        completed_at=NOW + timedelta(seconds=1),
        provider_message_ref="msg_123",
    )
    second = service.ack(
        WORKSPACE_ID,
        fixture.intent.id,
        claim_token=envelope.claim_token,
        fencing_token=envelope.fencing_token,
        completed_at=NOW + timedelta(seconds=2),
        provider_message_ref="msg_123",
    )

    assert first == second
    assert first.status is NotificationDeliveryStatus.DELIVERED
    assert first.outcome is NotificationAttemptOutcome.DELIVERED
    assert len(fixture.uow.notifications.attempts) == 1
    with pytest.raises((InvalidTransition, StaleFencingToken, ValidationError)):
        service.ack(
            WORKSPACE_ID,
            fixture.intent.id,
            claim_token=uuid4(),
            fencing_token=envelope.fencing_token,
            completed_at=NOW + timedelta(seconds=2),
            provider_message_ref="msg_123",
        )


def test_retryable_nack_uses_jitter_and_provider_retry_after_only_extends_delay() -> None:
    fixture = _fixture()
    service = _service(fixture, random_source=lambda: 0.5)
    envelope = service.claim(
        WORKSPACE_ID,
        fixture.module.id,
        supported_contract_versions=(1,),
        batch_size=50,
        lease_seconds=30,
        now=NOW,
    )[0]

    result = service.nack(
        WORKSPACE_ID,
        fixture.intent.id,
        claim_token=envelope.claim_token,
        fencing_token=envelope.fencing_token,
        outcome=NotificationAttemptOutcome.RETRYABLE_FAILURE,
        error_code=NotificationAttemptErrorCode.TIMEOUT,
        retry_after_seconds=10,
        provider_message_ref="provider_ref_1",
        now=NOW + timedelta(seconds=1),
    )

    assert result.status is NotificationDeliveryStatus.RETRY_WAIT
    assert result.next_attempt_at == NOW + timedelta(seconds=11)
    attempt = fixture.uow.notifications.attempts[(fixture.intent.id, 1)]
    assert attempt.outcome is NotificationAttemptOutcome.RETRYABLE_FAILURE
    assert attempt.error_code is NotificationAttemptErrorCode.TIMEOUT
    assert attempt.provider_message_ref == "provider_ref_1"


def test_retryable_nack_exhausts_when_attempt_limit_is_reached() -> None:
    fixture = _fixture()
    service = _service(fixture, max_attempts=1)
    envelope = service.claim(
        WORKSPACE_ID,
        fixture.module.id,
        supported_contract_versions=(1,),
        batch_size=50,
        lease_seconds=30,
        now=NOW,
    )[0]

    result = service.nack(
        WORKSPACE_ID,
        fixture.intent.id,
        claim_token=envelope.claim_token,
        fencing_token=envelope.fencing_token,
        outcome=NotificationAttemptOutcome.RETRYABLE_FAILURE,
        error_code=NotificationAttemptErrorCode.PROVIDER_UNAVAILABLE,
        now=NOW + timedelta(seconds=1),
    )

    assert result.status is NotificationDeliveryStatus.EXHAUSTED
    assert result.outcome is NotificationAttemptOutcome.RETRYABLE_FAILURE
    assert fixture.uow.notifications.intents[fixture.intent.id].exhausted_at == NOW + timedelta(seconds=1)


def test_expired_claim_settles_unknown_result_and_allows_reclaim_with_new_fence() -> None:
    fixture = _fixture()
    service = _service(fixture)
    first = service.claim(
        WORKSPACE_ID,
        fixture.module.id,
        supported_contract_versions=(1,),
        batch_size=50,
        lease_seconds=5,
        now=NOW,
    )[0]
    expired_at = NOW + timedelta(seconds=6)

    assert service.recover_expired_claims(WORKSPACE_ID, now=expired_at, limit=50) == 1
    current = fixture.uow.notifications.intents[fixture.intent.id]
    assert current.status is NotificationDeliveryStatus.RETRY_WAIT
    assert current.fencing_token == first.fencing_token + 1
    assert current.claim_token is None
    attempt = fixture.uow.notifications.attempts[(fixture.intent.id, 1)]
    assert attempt.outcome is NotificationAttemptOutcome.UNKNOWN_RESULT
    assert attempt.error_code is NotificationAttemptErrorCode.UNKNOWN_RESULT

    second = service.claim(
        WORKSPACE_ID,
        fixture.module.id,
        supported_contract_versions=(1,),
        batch_size=50,
        lease_seconds=30,
        now=expired_at,
    )[0]
    assert second.fencing_token == current.fencing_token + 1
    assert second.idempotency_key == first.idempotency_key
    assert second.notification_ref == first.notification_ref
