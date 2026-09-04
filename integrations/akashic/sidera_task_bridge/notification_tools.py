from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, cast
from uuid import UUID

from .client import (
    CommandExecution,
    CommandIntent,
    CommandStatus,
    ControlPlaneRejected,
    DispatchResult,
    TransportUnavailable,
)
from .config import TaskBridgeConfig
from .contracts import (
    JsonValue,
    NotificationThread,
    NotificationThreadDetail,
    NotificationThreadPage,
    ToolEnvelope,
)
from .source_routes import MissingToolContext, SourceContextResolver, SourceResolution
from .state import BridgeStateStore, PrivateRoute, StateConflict
from .tools import resolve_source_context

_MAX_LIST_LIMIT = 20
_MAX_FACT_LIMIT = 50
_MAX_CANDIDATES = 10
_MAX_SEARCH_PAGES = 5
_PRIVATE_CHANNEL_TYPE = "akashic_private"
_RECIPIENT_TYPE = "workspace_operator"
_RECIPIENT_ID = "local"
_NOTIFICATION_ACTIONS = frozenset({"read", "archive", "requeue"})
_CONFLICT_CODES = frozenset(
    {
        "notification_route_conflict",
        "version_conflict",
        "invalid_transition",
        "resource_busy",
        "command_identity_conflict",
    }
)
_UNAVAILABLE_CODES = frozenset(
    {
        "service_unavailable",
        "command_status_unavailable",
        "notification_principal_unavailable",
        "notification_route_unavailable",
        "adapter_workflow_requires_loopback",
    }
)
_SECRET_MARKER_PATTERN = re.compile(
    r"(?:api[_ -]?key|authorization|bearer|password|secret|token)", re.IGNORECASE
)


class _NotificationFailure(RuntimeError):
    def __init__(self, envelope: ToolEnvelope) -> None:
        self.envelope = envelope
        super().__init__(envelope.error_code or envelope.summary)


class _DispatchFailure(RuntimeError):
    def __init__(self, result: DispatchResult, operation: str) -> None:
        self.result = result
        self.operation = operation
        super().__init__(operation)


class SideraNotificationTools:
    """Provider-neutral Main Agent tools for the SIDERA notification gateway."""

    def __init__(
        self,
        config: TaskBridgeConfig,
        *,
        client: Any,
        dispatcher: Any,
        state: BridgeStateStore,
        source_resolver: SourceContextResolver,
        session_metadata_provider: Callable[[str], Mapping[str, object]] | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._dispatcher = dispatcher
        self._state = state
        self._source_resolver = source_resolver
        self._session_metadata_provider = session_metadata_provider

    async def sidera_list_inbox(
        self,
        event: object | None = None,
        category: str = "action",
        unread_only: bool = False,
        cursor: str | None = None,
        limit: int = _MAX_LIST_LIMIT,
    ) -> str:
        """List either the Web operator inbox or the current private Route inbox."""

        del event
        invalid = _validate_list_inputs(category, unread_only, cursor, limit)
        if invalid is not None:
            return _encode(invalid)
        try:
            source = await self._resolve_source()
            if source.source_type == "akashic_private":
                route = await self._ensure_private_route(source)
                route_id = _required_route_id(route)
                data = await self._list_private_inbox(
                    route_id,
                    category=category,
                    unread_only=unread_only,
                    cursor=cursor,
                    limit=limit,
                )
                data["scope"] = "origin_route"
                data["category"] = category
                data["unread_only"] = unread_only
                return _encode(
                    ToolEnvelope(
                        outcome="ok",
                        summary="The current private source notification inbox is loaded.",
                        data=data,
                    )
                )

            summary = await self._client.get_notification_summary()
            page = await self._client.list_notification_threads(
                category=category,
                unread_only=unread_only,
                cursor=cursor,
                limit=limit,
            )
            data = _page_data(page)
            data["scope"] = "workspace"
            data["category"] = category
            data["unread_only"] = unread_only
            data["summary"] = cast(dict[str, JsonValue], summary.model_dump(mode="json"))
            return _encode(
                ToolEnvelope(
                    outcome="ok",
                    summary="The workspace notification inbox is loaded.",
                    data=data,
                )
            )
        except (
            _NotificationFailure,
            _DispatchFailure,
            MissingToolContext,
            TransportUnavailable,
            ControlPlaneRejected,
            StateConflict,
            ValueError,
            TypeError,
        ) as exc:
            return _encode(self._exception_envelope(exc, "load the notification inbox"))

    async def sidera_get_notification_thread(
        self,
        event: object | None = None,
        notification_ref: str | None = None,
        thread_id: str | None = None,
        fact_cursor: str | None = None,
        fact_limit: int = _MAX_FACT_LIMIT,
    ) -> str:
        """Resolve one exact notification reference and return its bounded detail."""

        del event
        invalid = _validate_detail_inputs(
            notification_ref, thread_id, fact_cursor, fact_limit
        )
        if invalid is not None:
            return _encode(invalid)
        try:
            parsed_thread_id = _parse_uuid(thread_id)
            source = await self._resolve_source()
            selected = await self._resolve_thread_candidate(
                source,
                notification_ref=notification_ref,
                thread_id=parsed_thread_id,
            )
            if isinstance(selected, ToolEnvelope):
                return _encode(selected)
            detail = await self._client.get_notification_thread(
                selected.id,
                fact_cursor=fact_cursor,
                fact_limit=fact_limit,
            )
            return _encode(_detail_envelope(detail))
        except (
            _NotificationFailure,
            _DispatchFailure,
            MissingToolContext,
            TransportUnavailable,
            ControlPlaneRejected,
            StateConflict,
            ValueError,
            TypeError,
        ) as exc:
            return _encode(self._exception_envelope(exc, "load the notification thread"))

    async def sidera_manage_notification(
        self,
        event: object | None = None,
        action: str = "",
        thread_id: str | None = None,
        delivery_intent_id: str | None = None,
        expected_version: int | None = None,
        expected_status: str | None = None,
        expected_route_version: int | None = None,
        expected_latest_fact_id: str | None = None,
        reason: str | None = None,
    ) -> str:
        """Apply only explicit notification read/archive/requeue commands."""

        del event
        invalid = _validate_manage_inputs(
            action=action,
            thread_id=thread_id,
            delivery_intent_id=delivery_intent_id,
            expected_version=expected_version,
            expected_status=expected_status,
            expected_route_version=expected_route_version,
            expected_latest_fact_id=expected_latest_fact_id,
            reason=reason,
        )
        if invalid is not None:
            return _encode(invalid)
        try:
            source = await self._resolve_source()
            route: PrivateRoute | None = None
            if source.source_type == "akashic_private":
                route = await self._ensure_private_route(source)

            if action in {"read", "archive"}:
                parsed_thread_id = _parse_uuid(thread_id)
                if parsed_thread_id is None:
                    raise AssertionError("validated thread id is missing")
                detail = await self._load_owned_thread_detail(
                    source,
                    route,
                    parsed_thread_id,
                    fact_limit=_MAX_FACT_LIMIT,
                )
                if isinstance(detail, ToolEnvelope):
                    return _encode(detail)
                command = _thread_command(
                    action,
                    detail,
                    expected_version=cast(int, expected_version),
                    expected_latest_fact_id=expected_latest_fact_id,
                )
                result = await self._dispatcher.execute(
                    f"notification:thread:{parsed_thread_id}", command
                )
                if result.status != "applied":
                    raise _DispatchFailure(result, f"{action} notification thread")
                return _encode(
                    _mutation_envelope(
                        action,
                        resource_ref=f"notification-thread:{parsed_thread_id}",
                        web_url=detail.deep_link,
                        data={
                            "thread_id": str(parsed_thread_id),
                            "expected_version": cast(int, expected_version),
                        },
                    )
                )

            parsed_delivery_id = _parse_uuid(delivery_intent_id)
            if parsed_delivery_id is None:
                raise AssertionError("validated delivery id is missing")
            delivery_detail = await self._load_delivery_detail(
                source,
                route,
                parsed_delivery_id,
            )
            if isinstance(delivery_detail, ToolEnvelope):
                return _encode(delivery_detail)
            command = CommandIntent(
                command_type="RequeueNotificationDelivery",
                target_type="notification_delivery",
                target_id=parsed_delivery_id,
                expected_version=None,
                payload={
                    "expected_status": "exhausted",
                    "expected_route_version": cast(int, expected_route_version),
                    "reason": cast(str, reason).strip(),
                },
            )
            result = await self._dispatcher.execute(
                f"notification:delivery:{parsed_delivery_id}", command
            )
            if result.status != "applied":
                raise _DispatchFailure(result, "requeue notification delivery")
            return _encode(
                _mutation_envelope(
                    "requeue",
                    resource_ref=f"notification-delivery:{parsed_delivery_id}",
                    web_url=delivery_detail.deep_link,
                    data={"delivery_intent_id": str(parsed_delivery_id)},
                )
            )
        except (
            _NotificationFailure,
            _DispatchFailure,
            MissingToolContext,
            TransportUnavailable,
            ControlPlaneRejected,
            StateConflict,
            ValueError,
            TypeError,
        ) as exc:
            return _encode(
                self._exception_envelope(exc, "manage the notification")
            )

    async def _resolve_source(self) -> SourceResolution:
        return await resolve_source_context(
            self._state,
            self._source_resolver,
            self._session_metadata_provider,
        )

    async def _ensure_private_route(self, source: SourceResolution) -> PrivateRoute:
        route = source.private_route
        if route is None:
            raise _NotificationFailure(
                ToolEnvelope(
                    outcome="unavailable",
                    summary="The current private notification route is unavailable.",
                    error_code="notification_route_unavailable",
                )
            )
        if route.control_plane_route_id is not None:
            return route
        module_id = self._config.notification_channel_module_instance_id
        if module_id is None:
            raise _NotificationFailure(
                ToolEnvelope(
                    outcome="unavailable",
                    summary="Private notification routing is not configured.",
                    error_code="notification_route_unavailable",
                )
            )

        command = CommandIntent(
            command_type="RegisterNotificationRoute",
            target_type="notification_route",
            target_id=None,
            expected_version=None,
            payload={
                "channel_module_instance_id": str(module_id),
                "channel_type": _PRIVATE_CHANNEL_TYPE,
                "recipient_type": _RECIPIENT_TYPE,
                "recipient_id": _RECIPIENT_ID,
                "source_ref": source.source_ref,
            },
        )
        result = await self._dispatcher.execute(
            f"notification:route:{source.source_ref}", command
        )
        if result.status != "applied":
            raise _DispatchFailure(result, "register the notification route")
        route_id = _result_uuid(result, "notification_route_id")
        if route_id is None:
            raise _NotificationFailure(
                ToolEnvelope(
                    outcome="failed",
                    summary="The control plane accepted the route without a route reference.",
                    error_code="invalid_control_plane_response",
                )
            )
        enriched = route.model_copy(update={"control_plane_route_id": route_id})
        await self._state.save_route(source.source_ref, enriched)
        return enriched

    async def _list_private_inbox(
        self,
        route_id: UUID,
        *,
        category: str,
        unread_only: bool,
        cursor: str | None,
        limit: int,
    ) -> dict[str, JsonValue]:
        """Apply list filters locally because the route endpoint is unfiltered."""

        selected: list[NotificationThread] = []
        current_cursor = cursor
        last_change_cursor = 0
        next_cursor: str | None = None
        for _ in range(_MAX_SEARCH_PAGES):
            page = await self._client.list_notification_route_threads(
                route_id, cursor=current_cursor, limit=limit
            )
            last_change_cursor = max(last_change_cursor, page.last_change_cursor)
            selected.extend(
                item
                for item in page.items
                if item.category == category
                and (not unread_only or item.unread_count > 0)
            )
            next_cursor = page.next_cursor
            if len(selected) >= limit or next_cursor is None:
                break
            current_cursor = next_cursor
        return {
            "items": [item.model_dump(mode="json") for item in selected[:limit]],
            "next_cursor": next_cursor,
            "last_change_cursor": last_change_cursor,
        }

    async def _resolve_thread_candidate(
        self,
        source: SourceResolution,
        *,
        notification_ref: str | None,
        thread_id: UUID | None,
    ) -> NotificationThread | ToolEnvelope:
        route: PrivateRoute | None = None
        if source.source_type == "akashic_private":
            route = await self._ensure_private_route(source)
        candidates = await self._list_candidates(source, route)
        if thread_id is not None:
            matched = [candidate for candidate in candidates if candidate.id == thread_id]
        else:
            matched = [
                candidate
                for candidate in candidates
                if candidate.notification_ref == notification_ref
            ]
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            return ToolEnvelope(
                outcome="needs_user",
                summary="The notification reference matches multiple task locations.",
                error_code="notification_ref_ambiguous",
                data={"candidates": _candidate_data(matched)},
            )
        return ToolEnvelope(
            outcome="needs_user",
            summary="The notification reference is not present in the current source inbox.",
            error_code="notification_not_found",
            data={"candidates": []},
        )

    async def _list_candidates(
        self,
        source: SourceResolution,
        route: PrivateRoute | None,
    ) -> list[NotificationThread]:
        candidates: list[NotificationThread] = []
        if source.source_type == "akashic_private":
            route_id = _required_route_id(route)
            cursor: str | None = None
            for _ in range(_MAX_SEARCH_PAGES):
                page = await self._client.list_notification_route_threads(
                    route_id, cursor=cursor, limit=200
                )
                candidates.extend(page.items)
                cursor = page.next_cursor
                if cursor is None:
                    break
            return candidates

        for category in ("action", "progress", "archived"):
            cursor = None
            for _ in range(_MAX_SEARCH_PAGES):
                page = await self._client.list_notification_threads(
                    category=category,
                    unread_only=False,
                    cursor=cursor,
                    limit=200,
                )
                candidates.extend(page.items)
                cursor = page.next_cursor
                if cursor is None:
                    break
        return candidates

    async def _load_owned_thread_detail(
        self,
        source: SourceResolution,
        route: PrivateRoute | None,
        thread_id: UUID,
        *,
        fact_limit: int,
    ) -> NotificationThreadDetail | ToolEnvelope:
        if source.source_type == "akashic_private":
            _required_route_id(route)
            candidates = await self._list_candidates(source, route)
            if not any(candidate.id == thread_id for candidate in candidates):
                return ToolEnvelope(
                    outcome="needs_user",
                    summary="That notification does not belong to the current private source.",
                    error_code="notification_not_found",
                )
        try:
            return await self._client.get_notification_thread(
                thread_id, fact_cursor=None, fact_limit=fact_limit
            )
        except ControlPlaneRejected as exc:
            return _rejected_envelope(exc.code, "load the notification thread")

    async def _load_delivery_detail(
        self,
        source: SourceResolution,
        route: PrivateRoute | None,
        delivery_id: UUID,
    ) -> NotificationThreadDetail | ToolEnvelope:
        if source.source_type == "akashic_private":
            _required_route_id(route)
        for candidate in await self._list_candidates(source, route):
            detail = await self._client.get_notification_thread(
                candidate.id, fact_cursor=None, fact_limit=_MAX_FACT_LIMIT
            )
            if any(delivery.delivery_id == delivery_id for delivery in detail.deliveries):
                delivery = next(
                    item for item in detail.deliveries if item.delivery_id == delivery_id
                )
                if delivery.status != "exhausted":
                    return ToolEnvelope(
                        outcome="conflict",
                        summary="Only an exhausted notification delivery can be requeued.",
                        error_code="invalid_transition",
                    )
                return detail
        return ToolEnvelope(
            outcome="needs_user",
            summary="The notification delivery is not present in the current source.",
            error_code="notification_not_found",
        )

    def _exception_envelope(self, exc: Exception, operation: str) -> ToolEnvelope:
        if isinstance(exc, _NotificationFailure):
            return exc.envelope
        if isinstance(exc, _DispatchFailure):
            return _dispatch_envelope(exc.result, exc.operation)
        if isinstance(exc, MissingToolContext):
            return ToolEnvelope(
                outcome="failed",
                summary="The source context for this notification request is missing.",
                error_code="missing_tool_context",
            )
        if isinstance(exc, TransportUnavailable):
            return ToolEnvelope(
                outcome="unavailable",
                summary=f"The SIDERA control plane is unavailable while trying to {operation}.",
                error_code="service_unavailable",
            )
        if isinstance(exc, ControlPlaneRejected):
            return _rejected_envelope(exc.code, operation)
        if isinstance(exc, StateConflict):
            return ToolEnvelope(
                outcome="conflict",
                summary="The local notification source binding conflicts with current state.",
                error_code="source_binding_conflict",
            )
        if isinstance(exc, ValueError | TypeError):
            return ToolEnvelope(
                outcome="failed",
                summary=f"The notification request could not {operation} because its input is invalid.",
                error_code="invalid_input",
            )
        return ToolEnvelope(
            outcome="failed",
            summary=f"The notification request could not {operation}.",
            error_code="notification_operation_failed",
        )


def _validate_list_inputs(
    category: str,
    unread_only: bool,
    cursor: str | None,
    limit: int,
) -> ToolEnvelope | None:
    if category not in {"action", "progress", "archived"}:
        return _invalid_input("category must be action, progress, or archived")
    if not isinstance(unread_only, bool):
        return _invalid_input("unread_only must be a boolean")
    if _invalid_cursor(cursor) or not _valid_limit(limit, _MAX_LIST_LIMIT):
        return _invalid_input("cursor or limit is outside the supported bounds")
    return None


def _validate_detail_inputs(
    notification_ref: str | None,
    thread_id: str | None,
    fact_cursor: str | None,
    fact_limit: int,
) -> ToolEnvelope | None:
    if (notification_ref is None) == (thread_id is None):
        return _invalid_input("provide exactly one notification_ref or thread_id")
    if notification_ref is not None and not _valid_text(notification_ref, 128):
        return _invalid_input("notification_ref is invalid")
    if thread_id is not None and _parse_uuid(thread_id) is None:
        return _invalid_input("thread_id is invalid")
    if _invalid_cursor(fact_cursor) or not _valid_limit(fact_limit, _MAX_FACT_LIMIT):
        return _invalid_input("fact_cursor or fact_limit is outside the supported bounds")
    return None


def _validate_manage_inputs(
    *,
    action: str,
    thread_id: str | None,
    delivery_intent_id: str | None,
    expected_version: int | None,
    expected_status: str | None,
    expected_route_version: int | None,
    expected_latest_fact_id: str | None,
    reason: str | None,
) -> ToolEnvelope | None:
    if action not in _NOTIFICATION_ACTIONS:
        return ToolEnvelope(
            outcome="failed",
            summary="Only read, archive, and requeue notification actions are supported.",
            error_code="unsupported_notification_action",
        )
    if action in {"read", "archive"}:
        if (
            _parse_uuid(thread_id) is None
            or delivery_intent_id is not None
            or expected_version is None
            or not _valid_positive_int(expected_version)
            or expected_status is not None
            or expected_route_version is not None
            or reason is not None
        ):
            return _invalid_input("thread notification command arguments are invalid")
        if action == "read":
            if _parse_uuid(expected_latest_fact_id) is None:
                return _invalid_input("read requires expected_latest_fact_id")
        elif expected_latest_fact_id is not None:
            return _invalid_input("archive does not accept expected_latest_fact_id")
        return None

    if (
        _parse_uuid(delivery_intent_id) is None
        or thread_id is not None
        or expected_version is not None
        or expected_status != "exhausted"
        or not _valid_positive_int(expected_route_version)
        or not isinstance(reason, str)
        or not _valid_reason(reason)
        or expected_latest_fact_id is not None
    ):
        return _invalid_input("requeue notification command arguments are invalid")
    return None


def _thread_command(
    action: str,
    detail: NotificationThreadDetail,
    *,
    expected_version: int,
    expected_latest_fact_id: str | None,
) -> CommandIntent:
    if detail.thread.version != expected_version:
        raise _NotificationFailure(
            ToolEnvelope(
                outcome="conflict",
                summary="The notification thread changed before this command was sent.",
                resource_ref=f"notification-thread:{detail.thread.id}",
                web_url=detail.deep_link,
                error_code="version_conflict",
                data={"current_version": detail.thread.version},
            )
        )
    if action == "read":
        latest_fact_id = _parse_uuid(expected_latest_fact_id)
        if latest_fact_id != detail.thread.latest_fact_id:
            raise _NotificationFailure(
                ToolEnvelope(
                    outcome="conflict",
                    summary="The notification thread has a newer fact than the requested read fence.",
                    resource_ref=f"notification-thread:{detail.thread.id}",
                    web_url=detail.deep_link,
                    error_code="version_conflict",
                    data={"current_latest_fact_id": str(detail.thread.latest_fact_id)},
                )
            )
        if "mark_read" not in detail.available_commands:
            raise _NotificationFailure(
                ToolEnvelope(
                    outcome="conflict",
                    summary="This notification thread is already read or no longer readable.",
                    resource_ref=f"notification-thread:{detail.thread.id}",
                    web_url=detail.deep_link,
                    error_code="invalid_transition",
                )
            )
        return CommandIntent(
            command_type="MarkNotificationThreadRead",
            target_type="notification_thread",
            target_id=detail.thread.id,
            expected_version=expected_version,
            payload={"expected_latest_fact_id": str(latest_fact_id)},
        )

    if "archive" not in detail.available_commands:
        raise _NotificationFailure(
            ToolEnvelope(
                outcome="conflict",
                summary="Only a resolved active notification thread can be archived.",
                resource_ref=f"notification-thread:{detail.thread.id}",
                web_url=detail.deep_link,
                error_code="invalid_transition",
            )
        )
    return CommandIntent(
        command_type="ArchiveNotificationThread",
        target_type="notification_thread",
        target_id=detail.thread.id,
        expected_version=expected_version,
        payload={},
    )


def _detail_envelope(detail: NotificationThreadDetail) -> ToolEnvelope:
    data = cast(dict[str, JsonValue], detail.model_dump(mode="json"))
    handoff = _handoff_data(detail)
    if handoff is not None:
        data["handoff"] = handoff
    return ToolEnvelope(
        outcome="ok",
        summary="The notification thread and current handoff are loaded.",
        resource_ref=f"notification-thread:{detail.thread.id}",
        web_url=detail.deep_link,
        data=data,
    )


def _handoff_data(detail: NotificationThreadDetail) -> dict[str, JsonValue] | None:
    fact = next(
        (item for item in detail.facts.items if item.id == detail.thread.latest_fact_id),
        detail.facts.items[0] if detail.facts.items else None,
    )
    if fact is None:
        return None
    projection = fact.source_projection_type.lower()
    if fact.worker_item_id is not None or projection in {
        "collaboration_governance",
        "handoff",
        "worker_item",
    }:
        resource_id = fact.worker_item_id or fact.source_resource_id
        return {
            "resource_ref": f"worker-item:{resource_id}",
            "deep_link": fact.deep_link,
            "suggested_tool": "sidera_get_collaboration_governance",
        }
    if "planning" in projection or projection in {"proposal", "clarification"}:
        return {
            "resource_ref": f"planning-request:{fact.source_resource_id}",
            "deep_link": fact.deep_link,
            "suggested_tool": "sidera_get_planning_status",
        }
    if "transition" in projection:
        return {
            "resource_ref": f"plan-transition:{fact.source_resource_id}",
            "deep_link": fact.deep_link,
            "suggested_tool": "sidera_get_plan_transition",
        }
    if "task" in projection or "milestone" in projection:
        return {
            "resource_ref": f"task:{fact.source_resource_id}",
            "deep_link": fact.deep_link,
            "suggested_tool": "sidera_get_task_status",
        }
    return None


def _page_data(page: NotificationThreadPage) -> dict[str, JsonValue]:
    return {
        "items": [item.model_dump(mode="json") for item in page.items],
        "next_cursor": page.next_cursor,
        "last_change_cursor": page.last_change_cursor,
    }


def _candidate_data(
    candidates: list[NotificationThread],
) -> list[JsonValue]:
    return [
        {
            "thread_id": str(item.id),
            "notification_ref": item.notification_ref,
            "root_task_id": str(item.root_task_id),
            "root_task_title": item.root_task_title,
            "subtask_id": str(item.subtask_id) if item.subtask_id is not None else None,
            "subtask_title": item.subtask_title,
            "worker_item_id": (
                str(item.worker_item_id) if item.worker_item_id is not None else None
            ),
            "worker_item_title": item.worker_item_title,
            "deep_link": item.deep_link,
            "title": item.title,
        }
        for item in candidates[:_MAX_CANDIDATES]
    ]


def _mutation_envelope(
    action: str,
    *,
    resource_ref: str,
    web_url: str,
    data: dict[str, JsonValue],
) -> ToolEnvelope:
    return ToolEnvelope(
        outcome="ok",
        summary=f"The notification {action} command was accepted.",
        resource_ref=resource_ref,
        web_url=web_url,
        data=data,
    )


def _dispatch_envelope(result: DispatchResult, operation: str) -> ToolEnvelope:
    code = result.error_code or "notification_command_rejected"
    if result.status == "unavailable" or code in _UNAVAILABLE_CODES:
        return ToolEnvelope(
            outcome="unavailable",
            summary=f"The SIDERA control plane is unavailable while trying to {operation}.",
            error_code=code,
        )
    if result.status == "result_unknown":
        return ToolEnvelope(
            outcome="result_unknown",
            summary=f"The result of {operation} is pending confirmation.",
            error_code=code,
        )
    return _rejected_envelope(code, operation)


def _rejected_envelope(code: str, operation: str) -> ToolEnvelope:
    if code in _CONFLICT_CODES:
        return ToolEnvelope(
            outcome="conflict",
            summary=f"The notification state changed while trying to {operation}.",
            error_code=code,
        )
    if code in _UNAVAILABLE_CODES:
        return ToolEnvelope(
            outcome="unavailable",
            summary=f"The SIDERA control plane is unavailable while trying to {operation}.",
            error_code=code,
        )
    return ToolEnvelope(
        outcome="failed",
        summary=f"The notification operation was rejected while trying to {operation}.",
        error_code=code,
    )


def _invalid_input(summary: str) -> ToolEnvelope:
    return ToolEnvelope(outcome="failed", summary=summary, error_code="invalid_input")


def _valid_text(value: str, maximum: int) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        return False
    return not any(ord(character) < 32 or ord(character) == 127 for character in normalized)


def _valid_reason(value: str) -> bool:
    return _valid_text(value, 256) and _SECRET_MARKER_PATTERN.search(value) is None


def _invalid_cursor(value: str | None) -> bool:
    return value is not None and not _valid_text(value, 512)


def _valid_limit(value: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= maximum


def _valid_positive_int(value: int | None) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _parse_uuid(value: object) -> UUID | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return UUID(value.strip())
    except ValueError:
        return None


def _required_route_id(route: PrivateRoute | None) -> UUID:
    if route is None or route.control_plane_route_id is None:
        raise _NotificationFailure(
            ToolEnvelope(
                outcome="unavailable",
                summary="The current private notification route is unavailable.",
                error_code="notification_route_unavailable",
            )
        )
    return route.control_plane_route_id


def _result_uuid(result: DispatchResult, key: str) -> UUID | None:
    data = _result_data(result)
    parsed = _parse_uuid(data.get(key))
    if parsed is not None:
        return parsed
    execution = result.execution
    if isinstance(execution, CommandExecution) and execution.result is not None:
        return execution.result.aggregate_id
    if isinstance(execution, CommandStatus) and execution.result is not None:
        return execution.result.aggregate_id
    return None


def _result_data(result: DispatchResult) -> dict[str, JsonValue]:
    execution = result.execution
    if isinstance(execution, CommandExecution) and execution.result is not None:
        return dict(execution.result.data)
    if isinstance(execution, CommandStatus) and execution.result is not None:
        return dict(execution.result.data)
    return {}


def _encode(envelope: ToolEnvelope) -> str:
    return envelope.model_dump_json()


__all__ = ["SideraNotificationTools"]
