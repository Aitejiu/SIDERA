from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import (
    DomainError,
    OperatorRequired,
    ValidationError,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.modules import (
    NOTIFICATION_CHANNEL_MODULE_TYPE,
    ModuleLifecycle,
    NotificationChannelCapabilityProfile,
)
from sidera_control_plane.domain.notifications import (
    NotificationBindingKind,
    NotificationRoute,
    NotificationRouteStatus,
    NotificationSourceBinding,
)
from sidera_control_plane.domain.planning import (
    PlanningRequestKind,
    PlanningRequestStatus,
)

_AKASHIC_PRIVATE_CHANNEL = "akashic_private"
_WORKSPACE_OPERATOR_RECIPIENT = "workspace_operator"
_LOCAL_RECIPIENT = "local"
_DELIVERY_CONTRACT_VERSION = 1
_MIN_SOURCE_REF_LENGTH = 32
_MAX_SOURCE_REF_LENGTH = 512
_SOURCE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:/+\-]*$")
_ROUTE_FIELDS = frozenset(
    {
        "channel_module_instance_id",
        "channel_type",
        "recipient_type",
        "recipient_id",
        "source_ref",
    }
)


class NotificationRouteConflict(DomainError):
    """The opaque source identity is already owned by another route."""

    code = "notification_route_conflict"

    def __init__(self, message: str = "notification route identity conflicts") -> None:
        super().__init__(message)


class NotificationCommandHandler:
    command_types = frozenset({"RegisterNotificationRoute"})

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        *,
        local_actor_type: str = "local",
        local_actor_id: str = "aitejiu",
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._local_actor_type = local_actor_type
        self._local_actor_id = local_actor_id

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.command_type != "RegisterNotificationRoute":
            raise ValidationError(
                f"unsupported notification command: {envelope.command_type}"
            )
        self._require_local_actor(envelope)
        route_payload = _parse_route_payload(envelope.payload)
        if envelope.target_type != "notification_route":
            raise ValidationError(
                "RegisterNotificationRoute requires target_type=notification_route"
            )
        if envelope.target_id is not None or envelope.expected_version is not None:
            raise ValidationError(
                "RegisterNotificationRoute cannot select an existing route target"
            )

        instance = uow.modules.get_instance(
            route_payload.channel_module_instance_id, envelope.workspace_id
        )
        if instance is None or instance.workspace_id != envelope.workspace_id:
            raise ValidationError("notification channel module instance is unavailable")
        definition = uow.modules.get_definition(
            instance.definition_id, envelope.workspace_id
        )
        if definition is None or definition.workspace_id != envelope.workspace_id:
            raise ValidationError(
                "notification channel module definition is unavailable"
            )
        if definition.module_type != NOTIFICATION_CHANNEL_MODULE_TYPE:
            raise ValidationError("module definition is not a notification channel")
        if instance.definition_id != definition.id:
            raise ValidationError("notification channel module binding is invalid")
        if not _is_active(definition.lifecycle) or not _is_active(instance.lifecycle):
            raise ValidationError(
                "notification channel module definition and instance must be active"
            )
        if not isinstance(
            definition.capabilities, NotificationChannelCapabilityProfile
        ):
            raise ValidationError("notification channel capabilities are unavailable")
        if route_payload.channel_type not in definition.capabilities.channel_types:
            raise ValidationError(
                "notification channel capability does not support channel_type"
            )
        if (
            _DELIVERY_CONTRACT_VERSION
            not in definition.capabilities.delivery_contract_versions
        ):
            raise ValidationError(
                "notification channel capability does not support delivery contract version 1"
            )

        existing_routes = uow.notifications.list_routes_for_source_ref(
            envelope.workspace_id, route_payload.source_ref, for_update=True
        )
        existing = _find_existing_route(
            existing_routes,
            module_instance_id=route_payload.channel_module_instance_id,
            channel_type=route_payload.channel_type,
            recipient_type=route_payload.recipient_type,
            recipient_id=route_payload.recipient_id,
        )
        if existing_routes and (len(existing_routes) != 1 or existing is None):
            raise NotificationRouteConflict()
        if not existing_routes and (
            route_payload.recipient_type != _WORKSPACE_OPERATOR_RECIPIENT
            or route_payload.recipient_id != _LOCAL_RECIPIENT
        ):
            raise ValidationError(
                "only the workspace_operator/local notification recipient is supported"
            )

        created = existing is None
        route = existing
        if route is None:
            registered_at = self._clock()
            route = NotificationRoute(
                id=uuid4(),
                workspace_id=envelope.workspace_id,
                recipient_type=route_payload.recipient_type,
                recipient_id=route_payload.recipient_id,
                channel_type=route_payload.channel_type,
                channel_module_instance_id=route_payload.channel_module_instance_id,
                source_ref=route_payload.source_ref,
                status=NotificationRouteStatus.ACTIVE,
                version=1,
                registered_at=registered_at,
                updated_at=registered_at,
            )
            uow.notifications.add_route(route)
            uow.flush()

        request_bindings_created, root_bindings_created = _repair_origin_bindings(
            uow,
            route=route,
            now=self._clock(),
        )
        event_ids: tuple[UUID, ...] = ()
        if created:
            event = _event(
                envelope,
                aggregate_id=route.id,
                aggregate_version=route.version,
                event_type="NotificationRouteRegistered",
                payload={
                    "notification_route_id": str(route.id),
                    "channel_module_instance_id": str(route.channel_module_instance_id),
                    "channel_type": route.channel_type,
                    "recipient_type": route.recipient_type,
                    "recipient_id": route.recipient_id,
                    "source_ref": route.source_ref,
                    "delivery_contract_version": _DELIVERY_CONTRACT_VERSION,
                },
            )
            uow.events.append_with_outbox(event)
            event_ids = (event.id,)
        if created or request_bindings_created or root_bindings_created:
            uow.projection_changes.changed(
                workspace_id=envelope.workspace_id,
                projection_type="notification_thread",
                resource_id=None,
                change_type="notification.route_registered",
                resource_version=route.version,
                correlation_id=envelope.correlation_id,
            )
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=route.id,
            aggregate_version=route.version,
            event_ids=event_ids,
            data={
                "notification_route_id": str(route.id),
                "channel_module_instance_id": str(route.channel_module_instance_id),
                "channel_type": route.channel_type,
                "recipient_type": route.recipient_type,
                "recipient_id": route.recipient_id,
                "source_ref": route.source_ref,
                "route_version": route.version,
                "delivery_contract_version": _DELIVERY_CONTRACT_VERSION,
                "request_bindings_created": request_bindings_created,
                "root_bindings_created": root_bindings_created,
            },
        )

    def _require_local_actor(self, envelope: CommandEnvelope) -> None:
        if (
            envelope.actor_type != self._local_actor_type
            or envelope.actor_id != self._local_actor_id
        ):
            raise OperatorRequired(
                "notification route registration requires the configured local actor"
            )


def bind_planning_request_origin(
    uow: ControlPlaneUnitOfWork,
    request: Any,
    *,
    now: datetime,
) -> bool:
    """Bind only a create request with an exact registered private route."""

    if _request_kind(request) != PlanningRequestKind.CREATE.value:
        return False
    if request.source_type != _AKASHIC_PRIVATE_CHANNEL or not request.source_ref:
        return False
    routes = tuple(
        route
        for route in uow.notifications.list_routes_for_source_ref(
            request.workspace_id, request.source_ref
        )
        if (
            route.channel_type == _AKASHIC_PRIVATE_CHANNEL
            and route.recipient_type == _WORKSPACE_OPERATOR_RECIPIENT
            and route.recipient_id == _LOCAL_RECIPIENT
        )
    )
    if not routes:
        return False
    if len(routes) != 1:
        raise NotificationRouteConflict()
    return _bind_request_to_route(uow, request, routes[0], now=now)


def copy_planning_request_origin_to_root(
    uow: ControlPlaneUnitOfWork,
    *,
    request_id: UUID,
    root_task_id: UUID,
    workspace_id: UUID,
) -> bool:
    """Copy an existing request origin without allowing a reroute."""

    source = uow.notifications.get_binding(
        workspace_id,
        "planning_request",
        request_id,
        NotificationBindingKind.ORIGIN.value,
    )
    if source is None:
        return False
    existing = uow.notifications.get_binding(
        source.workspace_id,
        "root_task",
        root_task_id,
        NotificationBindingKind.ORIGIN.value,
    )
    if existing is not None:
        if existing.notification_route_id != source.notification_route_id:
            raise NotificationRouteConflict()
        return False
    uow.notifications.copy_origin_binding(
        workspace_id,
        "planning_request",
        request_id,
        "root_task",
        root_task_id,
    )
    return True


def _repair_origin_bindings(
    uow: ControlPlaneUnitOfWork,
    *,
    route: NotificationRoute,
    now: datetime,
) -> tuple[int, int]:
    request_bindings_created = 0
    root_bindings_created = 0
    requests = uow.planning_requests.list_by_source_ref(
        route.workspace_id, _AKASHIC_PRIVATE_CHANNEL, route.source_ref
    )
    for request in requests:
        if _request_kind(request) != PlanningRequestKind.CREATE.value:
            continue
        if _request_status(request) in {
            PlanningRequestStatus.REJECTED.value,
            PlanningRequestStatus.CANCELLED.value,
            PlanningRequestStatus.CONFLICTED.value,
        }:
            continue
        if _bind_request_to_route(uow, request, route, now=now):
            request_bindings_created += 1
        materialization = uow.proposal_materializations.get_by_request(
            request.id, route.workspace_id
        )
        root_task_id = getattr(materialization, "root_task_id", None)
        if isinstance(root_task_id, UUID) and copy_planning_request_origin_to_root(
            uow,
            request_id=request.id,
            root_task_id=root_task_id,
            workspace_id=route.workspace_id,
        ):
            root_bindings_created += 1
    return request_bindings_created, root_bindings_created


def _bind_request_to_route(
    uow: ControlPlaneUnitOfWork,
    request: Any,
    route: NotificationRoute,
    *,
    now: datetime,
) -> bool:
    existing = uow.notifications.get_binding(
        request.workspace_id,
        "planning_request",
        request.id,
        NotificationBindingKind.ORIGIN.value,
    )
    if existing is not None:
        if existing.notification_route_id != route.id:
            raise NotificationRouteConflict()
        return False
    uow.notifications.add_binding_or_get_existing(
        NotificationSourceBinding(
            workspace_id=request.workspace_id,
            resource_type="planning_request",
            resource_id=request.id,
            notification_route_id=route.id,
            binding_kind=NotificationBindingKind.ORIGIN,
            bound_at=now,
        )
    )
    return True


def _parse_route_payload(payload: Mapping[str, object]) -> Any:
    if not isinstance(payload, Mapping):
        raise ValidationError("RegisterNotificationRoute payload must be an object")
    unknown = set(payload) - _ROUTE_FIELDS
    if unknown:
        raise ValidationError(
            "RegisterNotificationRoute payload contains unknown fields"
        )
    channel_module_instance_id = _uuid(payload.get("channel_module_instance_id"))
    channel_type = _required_text(payload.get("channel_type"), "channel_type")
    recipient_type = _required_text(payload.get("recipient_type"), "recipient_type")
    recipient_id = _required_text(payload.get("recipient_id"), "recipient_id")
    source_ref = _required_text(payload.get("source_ref"), "source_ref")
    if channel_type != _AKASHIC_PRIVATE_CHANNEL:
        raise ValidationError("only akashic_private notification routes are supported")
    if not _MIN_SOURCE_REF_LENGTH <= len(source_ref) <= _MAX_SOURCE_REF_LENGTH:
        raise ValidationError("source_ref length is outside the supported bounds")
    if _SOURCE_REF_PATTERN.fullmatch(source_ref) is None:
        raise ValidationError("source_ref must use bounded opaque syntax")
    return _RoutePayload(
        channel_module_instance_id=channel_module_instance_id,
        channel_type=channel_type,
        recipient_type=recipient_type,
        recipient_id=recipient_id,
        source_ref=source_ref,
    )


@dataclass(frozen=True, slots=True)
class _RoutePayload:
    channel_module_instance_id: UUID
    channel_type: str
    recipient_type: str
    recipient_id: str
    source_ref: str


def _find_existing_route(
    routes: tuple[NotificationRoute, ...],
    *,
    module_instance_id: UUID,
    channel_type: str,
    recipient_type: str,
    recipient_id: str,
) -> NotificationRoute | None:
    for route in routes:
        if (
            route.channel_module_instance_id == module_instance_id
            and route.channel_type == channel_type
            and route.recipient_type == recipient_type
            and route.recipient_id == recipient_id
        ):
            return route
    return None


def _request_kind(request: Any) -> str:
    value = getattr(request.kind, "value", request.kind)
    return str(value)


def _request_status(request: Any) -> str:
    value = getattr(request.status, "value", request.status)
    return str(value)


def _is_active(value: object) -> bool:
    return getattr(value, "value", value) == ModuleLifecycle.ACTIVE.value


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    return value.strip()


def _uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValidationError("channel_module_instance_id must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationError("channel_module_instance_id must be a UUID") from exc


def _event(
    envelope: CommandEnvelope,
    *,
    aggregate_id: UUID,
    aggregate_version: int,
    event_type: str,
    payload: dict[str, Any],
) -> EventData:
    return EventData(
        id=uuid4(),
        workspace_id=envelope.workspace_id,
        aggregate_type="notification_route",
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        event_type=event_type,
        payload=payload,
        actor_type=envelope.actor_type,
        actor_id=envelope.actor_id,
        causation_id=envelope.id,
        correlation_id=envelope.correlation_id,
    )
