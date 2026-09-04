from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.api.schemas import CommandRequest
from sidera_control_plane.application.notification_commands import (
    NotificationCommandHandler,
    NotificationRouteConflict,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import (
    InvalidTransition,
    OperatorRequired,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.modules import (
    NOTIFICATION_CHANNEL_MODULE_TYPE,
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
    NotificationBindingKind,
    NotificationDeliveryIntent,
    NotificationDeliveryStatus,
    NotificationFact,
    NotificationPriority,
    NotificationRoute,
    NotificationRouteStatus,
    NotificationSourceBinding,
    NotificationTransition,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("70000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE_ID = UUID("70000000-0000-0000-0000-000000000002")


def channel_capabilities(
    *,
    channel_types: tuple[str, ...] = ("akashic_private",),
    delivery_contract_versions: tuple[int, ...] = (1,),
) -> NotificationChannelCapabilityProfile:
    return NotificationChannelCapabilityProfile(
        channel_types=channel_types,
        delivery_contract_versions=delivery_contract_versions,
        idempotency=IdempotencyCapability.OPERATION_ID,
        receipt=NotificationReceiptCapability.PROVIDER_REF,
        max_message_bytes=16_384,
        supports_deep_link=True,
        stability=StabilityLevel.EXPERIMENTAL,
    )


def module_pair(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    lifecycle: ModuleLifecycle = ModuleLifecycle.ACTIVE,
    definition_lifecycle: ModuleLifecycle | None = None,
    capabilities: NotificationChannelCapabilityProfile | None = None,
) -> tuple[ModuleDefinition, ModuleInstance]:
    definition_id = uuid4()
    definition = ModuleDefinition(
        id=definition_id,
        workspace_id=workspace_id,
        module_type=NOTIFICATION_CHANNEL_MODULE_TYPE,
        provider="akashic",
        interface_version="sidera.notification.v1",
        config_schema_version=1,
        capabilities=capabilities or channel_capabilities(),
        lifecycle=definition_lifecycle or lifecycle,
    )
    instance = ModuleInstance(
        id=uuid4(),
        workspace_id=workspace_id,
        definition_id=definition_id,
        instance_name="akashic-private",
        config_version=1,
        config_namespace="notification",
        config={"notification.mode": "test"},
        secret_refs=(),
        lifecycle=lifecycle,
    )
    return definition, instance


def route(
    module_instance_id: UUID,
    *,
    workspace_id: UUID = WORKSPACE_ID,
    source_ref: str = "src_" + "a" * 32,
    recipient_type: str = "workspace_operator",
    recipient_id: str = "local",
) -> NotificationRoute:
    return NotificationRoute(
        id=uuid4(),
        workspace_id=workspace_id,
        recipient_type=recipient_type,
        recipient_id=recipient_id,
        channel_type="akashic_private",
        channel_module_instance_id=module_instance_id,
        source_ref=source_ref,
        status=NotificationRouteStatus.ACTIVE,
        version=1,
        registered_at=NOW,
        updated_at=NOW,
    )


class ModuleRepository:
    def __init__(
        self,
        definitions: tuple[ModuleDefinition, ...],
        instances: tuple[ModuleInstance, ...],
    ) -> None:
        self.definitions = {item.id: item for item in definitions}
        self.instances = {item.id: item for item in instances}

    def get_definition(self, module_id: UUID, workspace_id: UUID):
        item = self.definitions.get(module_id)
        return item if item is not None and item.workspace_id == workspace_id else None

    def get_instance(self, module_id: UUID, workspace_id: UUID):
        item = self.instances.get(module_id)
        return item if item is not None and item.workspace_id == workspace_id else None


class NotificationRepository:
    def __init__(
        self,
        routes: tuple[NotificationRoute, ...] = (),
        threads: tuple[InboxThread, ...] = (),
        facts: tuple[NotificationFact, ...] = (),
        intents: tuple[NotificationDeliveryIntent, ...] = (),
    ) -> None:
        self.routes = list(routes)
        self.bindings: dict[tuple[UUID, str, UUID, str], NotificationSourceBinding] = {}
        self.threads = {item.id: item for item in threads}
        self.facts = {item.id: item for item in facts}
        self.intents = {item.id: item for item in intents}

    def get_route_by_source_ref(
        self,
        workspace_id: UUID,
        channel_module_instance_id: UUID,
        source_ref: str,
        *,
        for_update: bool = False,
    ) -> NotificationRoute | None:
        del for_update
        return next(
            (
                item
                for item in self.routes
                if item.workspace_id == workspace_id
                and item.channel_module_instance_id == channel_module_instance_id
                and item.source_ref == source_ref
            ),
            None,
        )

    def list_routes_for_source_ref(
        self, workspace_id: UUID, source_ref: str, *, for_update: bool = False
    ) -> tuple[NotificationRoute, ...]:
        del for_update
        return tuple(
            item
            for item in self.routes
            if item.workspace_id == workspace_id and item.source_ref == source_ref
        )

    def add_route(self, item: NotificationRoute) -> None:
        self.routes.append(item)

    def get_route(
        self, workspace_id: UUID, route_id: UUID, *, for_update: bool = False
    ) -> NotificationRoute | None:
        del for_update
        return next(
            (
                item
                for item in self.routes
                if item.id == route_id and item.workspace_id == workspace_id
            ),
            None,
        )

    def get_thread(
        self, workspace_id: UUID, thread_id: UUID, *, for_update: bool = False
    ) -> InboxThread | None:
        del for_update
        item = self.threads.get(thread_id)
        return item if item is not None and item.workspace_id == workspace_id else None

    def get_fact(self, workspace_id: UUID, fact_id: UUID) -> NotificationFact | None:
        item = self.facts.get(fact_id)
        return item if item is not None and item.workspace_id == workspace_id else None

    def add_fact_or_get_existing(self, fact: NotificationFact) -> NotificationFact:
        existing = self.facts.get(fact.id)
        if existing is not None and existing != fact:
            raise AssertionError("fact identity conflict")
        self.facts[fact.id] = fact
        return fact

    def save_thread(self, item: InboxThread, previous_version: int) -> None:
        current = self.threads.get(item.id)
        if current is None:
            raise AssertionError("thread is missing")
        if current.version != previous_version:
            raise VersionConflict(previous_version, current.version)
        self.threads[item.id] = item

    def get_thread_by_key(
        self,
        workspace_id: UUID,
        recipient_type: str,
        recipient_id: str,
        thread_key: str,
        *,
        for_update: bool = False,
    ) -> InboxThread | None:
        del for_update
        return next(
            (
                item
                for item in self.threads.values()
                if item.workspace_id == workspace_id
                and item.recipient_type == recipient_type
                and item.recipient_id == recipient_id
                and item.thread_key == thread_key
            ),
            None,
        )

    def add_thread(self, item: InboxThread) -> None:
        self.threads[item.id] = item

    def get_intent(
        self, workspace_id: UUID, intent_id: UUID, *, for_update: bool = False
    ) -> NotificationDeliveryIntent | None:
        del for_update
        item = self.intents.get(intent_id)
        return item if item is not None and item.workspace_id == workspace_id else None

    def add_intent_or_get_existing(
        self, item: NotificationDeliveryIntent
    ) -> NotificationDeliveryIntent:
        existing = self.intents.get(item.id)
        if existing is not None and existing != item:
            raise AssertionError("intent identity conflict")
        self.intents[item.id] = item
        return item

    def next_delivery_round(
        self, item: NotificationDeliveryIntent, *, now: datetime
    ) -> NotificationDeliveryIntent:
        next_item = item.next_delivery_round(now=now)
        return self.add_intent_or_get_existing(next_item)

    def get_binding(
        self,
        workspace_id: UUID,
        resource_type: str,
        resource_id: UUID,
        binding_kind: str,
    ) -> NotificationSourceBinding | None:
        return self.bindings.get(
            (workspace_id, resource_type, resource_id, binding_kind)
        )

    def add_binding_or_get_existing(
        self, item: NotificationSourceBinding
    ) -> NotificationSourceBinding:
        key = (
            item.workspace_id,
            item.resource_type,
            item.resource_id,
            item.binding_kind.value,
        )
        existing = self.bindings.get(key)
        if existing is not None:
            if existing != item:
                raise ValidationError("notification source binding conflicts")
            return existing
        self.bindings[key] = item
        return item

    def copy_origin_binding(
        self,
        workspace_id: UUID,
        source_resource_type: str,
        source_resource_id: UUID,
        target_resource_type: str,
        target_resource_id: UUID,
    ) -> NotificationSourceBinding | None:
        source = self.get_binding(
            workspace_id,
            source_resource_type,
            source_resource_id,
            NotificationBindingKind.ORIGIN.value,
        )
        if source is None:
            return None
        return self.add_binding_or_get_existing(
            NotificationSourceBinding(
                workspace_id=workspace_id,
                resource_type=target_resource_type,
                resource_id=target_resource_id,
                notification_route_id=source.notification_route_id,
                binding_kind=source.binding_kind,
                bound_at=source.bound_at,
            )
        )


class EventRepository:
    def __init__(self) -> None:
        self.events: list[EventData] = []

    def append_with_outbox(self, event: EventData) -> None:
        self.events.append(event)


class ProjectionPublisher:
    def __init__(self) -> None:
        self.changes: list[dict[str, object]] = []

    def changed(self, **kwargs: object) -> SimpleNamespace:
        self.changes.append(kwargs)
        return SimpleNamespace()


def make_uow(
    definition: ModuleDefinition,
    instance: ModuleInstance,
    *,
    routes: tuple[NotificationRoute, ...] = (),
    threads: tuple[InboxThread, ...] = (),
    facts: tuple[NotificationFact, ...] = (),
    intents: tuple[NotificationDeliveryIntent, ...] = (),
    extra_modules: tuple[tuple[ModuleDefinition, ModuleInstance], ...] = (),
    requests: tuple[object, ...] = (),
) -> SimpleNamespace:
    modules = ModuleRepository(
        (definition, *(item[0] for item in extra_modules)),
        (instance, *(item[1] for item in extra_modules)),
    )
    return SimpleNamespace(
        modules=modules,
        notifications=NotificationRepository(
            routes, threads=threads, facts=facts, intents=intents
        ),
        planning_requests=SimpleNamespace(
            list_by_source_ref=lambda workspace_id, source_type, source_ref: tuple(
                item
                for item in requests
                if item.workspace_id == workspace_id
                and item.source_type == source_type
                and item.source_ref == source_ref
            )
        ),
        proposal_materializations=SimpleNamespace(
            get_by_request=lambda request_id, workspace_id: None
        ),
        events=EventRepository(),
        projection_changes=ProjectionPublisher(),
        flush=lambda: None,
    )


def command(
    instance_id: UUID,
    *,
    source_ref: str = "src_" + "a" * 32,
    workspace_id: UUID = WORKSPACE_ID,
    **overrides: object,
) -> CommandEnvelope:
    values: dict[str, object] = {
        "id": uuid4(),
        "workspace_id": workspace_id,
        "command_type": "RegisterNotificationRoute",
        "target_type": "notification_route",
        "actor_type": "local",
        "actor_id": "operator",
        "target_id": None,
        "payload": {
            "channel_module_instance_id": str(instance_id),
            "channel_type": "akashic_private",
            "recipient_type": "workspace_operator",
            "recipient_id": "local",
            "source_ref": source_ref,
        },
        "expected_version": None,
        "idempotency_key": uuid4().hex,
        "correlation_id": uuid4(),
    }
    values.update(overrides)
    return CommandEnvelope(**values)  # type: ignore[arg-type]


def thread_command(
    command_type: str,
    thread: InboxThread,
    *,
    payload: dict[str, object] | None = None,
    expected_version: int | None = None,
    **overrides: object,
) -> CommandEnvelope:
    values: dict[str, object] = {
        "id": uuid4(),
        "workspace_id": thread.workspace_id,
        "command_type": command_type,
        "target_type": "notification_thread",
        "actor_type": "local",
        "actor_id": "operator",
        "target_id": thread.id,
        "payload": payload or {},
        "expected_version": (
            thread.version if expected_version is None else expected_version
        ),
        "idempotency_key": uuid4().hex,
        "correlation_id": uuid4(),
    }
    values.update(overrides)
    return CommandEnvelope(**values)  # type: ignore[arg-type]


def notification_fact(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    root_task_id: UUID | None = None,
    priority: NotificationPriority = NotificationPriority.ACTION,
    transition: NotificationTransition = NotificationTransition.OPENED,
    fingerprint: str = "b" * 64,
) -> NotificationFact:
    root_task_id = root_task_id or uuid4()
    return NotificationFact.create(
        workspace_id=workspace_id,
        policy_version=1,
        recipient_type="workspace_operator",
        recipient_id="local",
        kind="execution_attention",
        priority=priority,
        transition=transition,
        topic="execution_attention",
        root_task_id=root_task_id,
        subtask_id=None,
        worker_item_id=None,
        source_projection_cursor=1,
        source_projection_type="task_overview",
        source_resource_id=root_task_id,
        source_resource_version=1,
        authoritative_fingerprint=fingerprint,
        title="需要关注的任务",
        summary="任务状态发生变化。",
        action="打开任务",
        deep_link=f"/tasks/{root_task_id}",
        correlation_id="notification-command-test",
        baseline_import=False,
        occurred_at=NOW,
        created_at=NOW,
    )


def notification_intent(
    fact: NotificationFact,
    route_value: NotificationRoute,
    *,
    status: NotificationDeliveryStatus = NotificationDeliveryStatus.EXHAUSTED,
    delivery_round: int = 1,
) -> NotificationDeliveryIntent:
    return NotificationDeliveryIntent(
        id=uuid4(),
        workspace_id=fact.workspace_id,
        notification_fact_id=fact.id,
        notification_route_id=route_value.id,
        channel_module_instance_id=route_value.channel_module_instance_id,
        delivery_round=delivery_round,
        contract_version=1,
        status=status,
        attempts=1,
        next_attempt_at=NOW,
        claimed_until=None,
        claim_token=None,
        fencing_token=1,
        provider_message_ref=None,
        last_error_code=NotificationAttemptErrorCode.PAYLOAD_REJECTED,
        delivered_at=None,
        exhausted_at=NOW
        if status is NotificationDeliveryStatus.EXHAUSTED
        else None,
        created_at=NOW,
        updated_at=NOW,
    )


def requeue_command(
    intent: NotificationDeliveryIntent,
    route_value: NotificationRoute,
    **overrides: object,
) -> CommandEnvelope:
    values: dict[str, object] = {
        "id": uuid4(),
        "workspace_id": intent.workspace_id,
        "command_type": "RequeueNotificationDelivery",
        "target_type": "notification_delivery",
        "actor_type": "local",
        "actor_id": "operator",
        "target_id": intent.id,
        "payload": {
            "expected_status": "exhausted",
            "expected_route_version": route_value.version,
            "reason": "provider configuration was repaired",
        },
        "expected_version": None,
        "idempotency_key": uuid4().hex,
        "correlation_id": uuid4(),
    }
    values.update(overrides)
    return CommandEnvelope(**values)  # type: ignore[arg-type]


def test_requeue_notification_delivery_api_request_is_explicitly_scoped() -> None:
    delivery_id = uuid4()
    workspace_id = uuid4()
    request = CommandRequest.model_validate(
        {
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "RequeueNotificationDelivery",
            "target_type": "notification_delivery",
            "actor": {"type": "local", "id": "operator"},
            "target_id": str(delivery_id),
            "expected_version": None,
            "idempotency_key": "requeue-command-1",
            "correlation_id": str(uuid4()),
            "payload": {
                "expected_status": "exhausted",
                "expected_route_version": 3,
                "reason": "route configuration repaired",
            },
        }
    )

    assert request.target_type == "notification_delivery"
    assert request.target_id == delivery_id
    assert request.expected_version is None
    assert request.payload["expected_status"] == "exhausted"
    assert request.payload["expected_route_version"] == 3

    with pytest.raises(ValueError):
        CommandRequest.model_validate(
            {
                "command_id": str(uuid4()),
                "workspace_id": str(workspace_id),
                "command_type": "RequeueNotificationDelivery",
                "target_type": "task",
                "actor": {"type": "local", "id": "operator"},
                "target_id": str(delivery_id),
                "expected_version": None,
                "idempotency_key": "requeue-command-2",
                "correlation_id": str(uuid4()),
                "payload": {
                    "expected_status": "exhausted",
                    "expected_route_version": 3,
                    "reason": "route configuration repaired",
                },
            }
        )


@pytest.mark.parametrize(
    ("command_type", "target_type", "target_id", "expected_version", "payload"),
    (
        ("RegisterNotificationRoute", "notification_route", None, None, {}),
        ("MarkNotificationThreadRead", "notification_thread", uuid4(), 1, {}),
    ),
)
def test_existing_notification_commands_have_public_target_types(
    command_type: str,
    target_type: str,
    target_id: UUID | None,
    expected_version: int | None,
    payload: dict[str, object],
) -> None:
    request = CommandRequest.model_validate(
        {
            "command_id": str(uuid4()),
            "workspace_id": str(uuid4()),
            "command_type": command_type,
            "target_type": target_type,
            "actor": {"type": "local", "id": "operator"},
            "target_id": str(target_id) if target_id else None,
            "expected_version": expected_version,
            "idempotency_key": f"{command_type}-1",
            "correlation_id": str(uuid4()),
            "payload": payload,
        }
    )

    assert request.target_type == target_type


def test_register_notification_route_is_idempotent_and_repairs_exact_requests() -> None:
    definition, instance = module_pair()
    root_task_id = uuid4()
    request = SimpleNamespace(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        source_type="akashic_private",
        source_ref="src_" + "a" * 32,
        root_task_id=None,
        status=SimpleNamespace(value="submitted"),
        kind=SimpleNamespace(value="create"),
    )
    uow = make_uow(definition, instance, requests=(request,))
    uow.proposal_materializations.get_by_request = lambda request_id, workspace_id: (
        SimpleNamespace(
            planning_request_id=request.id,
            root_task_id=root_task_id,
        )
    )
    handler = NotificationCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    first = handler.handle(uow, command(instance.id))
    second = handler.handle(uow, command(instance.id))

    stable_fields = (
        "notification_route_id",
        "channel_module_instance_id",
        "channel_type",
        "recipient_type",
        "recipient_id",
        "source_ref",
        "route_version",
        "delivery_contract_version",
    )
    assert {key: first.data[key] for key in stable_fields} == {
        key: second.data[key] for key in stable_fields
    }
    assert first.data["notification_route_id"] == str(first.aggregate_id)
    assert first.data["source_ref"] == "src_" + "a" * 32
    assert first.data["request_bindings_created"] == 1
    assert first.data["root_bindings_created"] == 1
    assert second.data["request_bindings_created"] == 0
    assert second.data["root_bindings_created"] == 0
    assert len(uow.notifications.routes) == 1
    assert len(uow.events.events) == 1
    assert len(uow.projection_changes.changes) == 1


def test_notification_thread_read_and_archive_commands_are_scoped_and_versioned() -> None:
    definition, instance = module_pair()
    fact = notification_fact()
    thread = InboxThread.open_from_fact(fact, thread_id=uuid4(), now=NOW)
    uow = make_uow(definition, instance, threads=(thread,), facts=(fact,))
    handler = NotificationCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    read_result = handler.handle(
        uow,
        thread_command(
            "MarkNotificationThreadRead",
            thread,
            payload={"expected_latest_fact_id": str(fact.id)},
        ),
    )

    read_thread = uow.notifications.threads[thread.id]
    assert read_result.aggregate_id == thread.id
    assert read_thread.unread_count == 0
    assert read_thread.business_status.value == "open"
    assert read_thread.version == 2
    assert uow.projection_changes.changes[-1]["projection_type"] == (
        "notification_thread"
    )

    with pytest.raises(InvalidTransition, match="open thread cannot be archived"):
        handler.handle(
            uow,
            thread_command("ArchiveNotificationThread", read_thread),
        )

    completed = notification_fact(
        root_task_id=fact.root_task_id,
        priority=NotificationPriority.PROGRESS,
        transition=NotificationTransition.COMPLETED,
        fingerprint="c" * 64,
    )
    resolved = thread.apply_fact(completed, now=NOW)
    resolved_uow = make_uow(
        definition,
        instance,
        threads=(resolved,),
        facts=(fact, completed),
    )
    archive_result = handler.handle(
        resolved_uow,
        thread_command("ArchiveNotificationThread", resolved),
    )

    archived = resolved_uow.notifications.threads[thread.id]
    assert archive_result.aggregate_id == thread.id
    assert archived.organization_status.value == "archived"
    assert archived.business_status.value == "resolved"
    assert archived.version == resolved.version + 1


def test_requeue_notification_delivery_creates_one_new_round_and_recovers_operations_thread() -> None:
    definition, instance = module_pair()
    fact = notification_fact()
    thread = InboxThread.open_from_fact(fact, thread_id=uuid4(), now=NOW)
    route_value = route(instance.id)
    intent = notification_intent(fact, route_value)
    uow = make_uow(
        definition,
        instance,
        routes=(route_value,),
        threads=(thread,),
        facts=(fact,),
        intents=(intent,),
    )
    handler = NotificationCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    first = handler.handle(uow, requeue_command(intent, route_value))

    next_intent = next(
        item for item in uow.notifications.intents.values() if item.id != intent.id
    )
    assert next_intent.delivery_round == 2
    assert next_intent.status is NotificationDeliveryStatus.PENDING
    assert uow.notifications.intents[intent.id] == intent
    assert first.data["delivery_round"] == 2
    assert first.data["previous_delivery_id"] == str(intent.id)
    assert first.aggregate_id == next_intent.id

    operation_facts = [
        item
        for item in uow.notifications.facts.values()
        if item.priority.value == "operations"
    ]
    assert len(operation_facts) == 1
    assert operation_facts[0].transition is NotificationTransition.RECOVERED
    assert len(uow.notifications.intents) == 2

    second = handler.handle(uow, requeue_command(intent, route_value))
    assert second.data == first.data
    assert len(uow.notifications.intents) == 2
    assert len(
        [
            item
            for item in uow.notifications.facts.values()
            if item.priority.value == "operations"
        ]
    ) == 1
    operation_thread = next(
        item
        for item in uow.notifications.threads.values()
        if item.topic.startswith("notification_channel_health:")
    )
    assert operation_thread.business_status.value == "resolved"


def test_requeue_notification_delivery_requires_current_active_route_and_local_actor() -> None:
    definition, instance = module_pair()
    fact = notification_fact()
    thread = InboxThread.open_from_fact(fact, thread_id=uuid4(), now=NOW)
    route_value = route(instance.id)
    intent = notification_intent(fact, route_value)
    handler = NotificationCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    foreign_actor_uow = make_uow(
        definition, instance, routes=(route_value,), threads=(thread,), facts=(fact,), intents=(intent,)
    )
    with pytest.raises(OperatorRequired):
        handler.handle(
            foreign_actor_uow,
            requeue_command(intent, route_value, actor_type="remote"),
        )

    stale_route_uow = make_uow(
        definition, instance, routes=(route_value,), threads=(thread,), facts=(fact,), intents=(intent,)
    )
    with pytest.raises(VersionConflict):
        handler.handle(
            stale_route_uow,
            requeue_command(
                intent,
                route_value,
                payload={
                    "expected_status": "exhausted",
                    "expected_route_version": route_value.version + 1,
                    "reason": "provider configuration was repaired",
                },
            ),
        )

    inactive_route = route_value.with_status("disabled", now=NOW)
    inactive_uow = make_uow(
        definition, instance, routes=(inactive_route,), threads=(thread,), facts=(fact,), intents=(intent,)
    )
    with pytest.raises(InvalidTransition):
        handler.handle(
            inactive_uow,
            requeue_command(intent, inactive_route),
        )


@pytest.mark.parametrize(
    ("command_type", "payload"),
    (
        ("MarkNotificationThreadRead", {}),
        (
            "MarkNotificationThreadRead",
            {"expected_latest_fact_id": str(uuid4()), "actor": "operator"},
        ),
        ("ArchiveNotificationThread", {"expected_latest_fact_id": str(uuid4())}),
    ),
)
def test_notification_thread_commands_reject_ambiguous_payloads(
    command_type: str, payload: dict[str, object]
) -> None:
    definition, instance = module_pair()
    fact = notification_fact()
    thread = InboxThread.open_from_fact(fact, thread_id=uuid4(), now=NOW)
    uow = make_uow(definition, instance, threads=(thread,), facts=(fact,))
    handler = NotificationCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    with pytest.raises(ValidationError):
        handler.handle(
            uow,
            thread_command(command_type, thread, payload=payload),
        )


def test_notification_thread_read_requires_current_fact_and_version() -> None:
    definition, instance = module_pair()
    fact = notification_fact()
    thread = InboxThread.open_from_fact(fact, thread_id=uuid4(), now=NOW)
    uow = make_uow(definition, instance, threads=(thread,), facts=(fact,))
    handler = NotificationCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    with pytest.raises(VersionConflict):
        handler.handle(
            uow,
            thread_command(
                "MarkNotificationThreadRead",
                thread,
                expected_version=thread.version + 1,
                payload={"expected_latest_fact_id": str(fact.id)},
            ),
        )
    with pytest.raises(InvalidTransition, match="expected latest fact"):
        handler.handle(
            uow,
            thread_command(
                "MarkNotificationThreadRead",
                thread,
                payload={"expected_latest_fact_id": str(uuid4())},
            ),
        )


def test_register_notification_route_requires_trusted_local_actor() -> None:
    definition, instance = module_pair()
    uow = make_uow(definition, instance)
    handler = NotificationCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    with pytest.raises(OperatorRequired):
        handler.handle(
            uow,
            command(instance.id, actor_type="remote", actor_id="adapter"),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("recipient_type", "team"),
        ("recipient_id", "another-user"),
        ("source_ref", "too-short"),
        ("channel_type", "unknown-channel"),
    ),
)
def test_register_notification_route_rejects_unsupported_route_payload(
    field: str, value: str
) -> None:
    definition, instance = module_pair()
    uow = make_uow(definition, instance)
    handler = NotificationCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )
    payload = dict(command(instance.id).payload)
    payload[field] = value

    with pytest.raises(ValidationError):
        handler.handle(uow, command(instance.id, payload=payload))


def test_register_notification_route_rejects_inactive_or_incompatible_modules() -> None:
    definition, instance = module_pair(lifecycle=ModuleLifecycle.DRAINING)
    uow = make_uow(definition, instance)
    handler = NotificationCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    with pytest.raises(ValidationError):
        handler.handle(uow, command(instance.id))

    active_definition, active_instance = module_pair(
        capabilities=channel_capabilities(delivery_contract_versions=(2,))
    )
    incompatible_uow = make_uow(active_definition, active_instance)
    with pytest.raises(ValidationError):
        incompatible_uow_handler = NotificationCommandHandler(
            local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
        )
        incompatible_uow_handler.handle(incompatible_uow, command(active_instance.id))


def test_register_notification_route_conflicts_across_module_or_recipient_identity() -> (
    None
):
    first_definition, first_instance = module_pair()
    second_definition, second_instance = module_pair()
    existing = route(first_instance.id)
    uow = make_uow(
        second_definition,
        second_instance,
        routes=(existing,),
        extra_modules=((first_definition, first_instance),),
    )
    handler = NotificationCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    with pytest.raises(NotificationRouteConflict) as conflict:
        handler.handle(uow, command(second_instance.id))
    assert conflict.value.code == "notification_route_conflict"

    same_module_uow = make_uow(first_definition, first_instance, routes=(existing,))
    payload = dict(command(first_instance.id).payload)
    payload["recipient_id"] = "another-recipient"
    with pytest.raises(NotificationRouteConflict):
        handler.handle(same_module_uow, command(first_instance.id, payload=payload))


def test_register_notification_route_rejects_foreign_module_instance() -> None:
    definition, instance = module_pair(workspace_id=OTHER_WORKSPACE_ID)
    uow = make_uow(definition, instance)
    handler = NotificationCommandHandler(
        local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
    )

    with pytest.raises(ValidationError):
        handler.handle(uow, command(instance.id))
