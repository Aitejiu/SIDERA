from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.notification_commands import (
    NotificationCommandHandler,
    NotificationRouteConflict,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import OperatorRequired, ValidationError
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
    NotificationBindingKind,
    NotificationRoute,
    NotificationRouteStatus,
    NotificationSourceBinding,
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
    def __init__(self, routes: tuple[NotificationRoute, ...] = ()) -> None:
        self.routes = list(routes)
        self.bindings: dict[tuple[UUID, str, UUID, str], NotificationSourceBinding] = {}

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
    extra_modules: tuple[tuple[ModuleDefinition, ModuleInstance], ...] = (),
    requests: tuple[object, ...] = (),
) -> SimpleNamespace:
    modules = ModuleRepository(
        (definition, *(item[0] for item in extra_modules)),
        (instance, *(item[1] for item in extra_modules)),
    )
    return SimpleNamespace(
        modules=modules,
        notifications=NotificationRepository(routes),
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
