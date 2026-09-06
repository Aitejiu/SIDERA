from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sidera_control_plane.application.errors import InvalidCursor, NotFound

NOTIFICATION_CATEGORIES = frozenset({"action", "progress", "archived"})
NOTIFICATION_KIND_LABELS = {
    "result_inputs_blocked": "结果输入阻塞",
}
NOTIFICATION_KIND_SCOPES = {
    "result_inputs_blocked": "execution_request",
}


def notification_kind_label(kind: str) -> str:
    """Return a bounded UI label without exposing provider or payload details."""

    return NOTIFICATION_KIND_LABELS.get(kind, "任务通知")


def notification_kind_scope(kind: str) -> str | None:
    """Return the business scope for kinds with a non-default owner."""

    return NOTIFICATION_KIND_SCOPES.get(kind)


@dataclass(frozen=True, slots=True)
class NotificationSummaryView:
    recipient_type: str
    recipient_id: str
    open_count: int
    unread_count: int
    progress_count: int
    channel_issue_count: int
    last_change_cursor: int


@dataclass(frozen=True, slots=True)
class NotificationFactView:
    id: UUID
    thread_id: UUID
    policy_version: int
    kind: str
    priority: str
    transition: str
    topic: str
    root_task_id: UUID
    subtask_id: UUID | None
    worker_item_id: UUID | None
    source_projection_cursor: int
    source_projection_type: str
    source_resource_id: UUID
    source_resource_version: int
    title: str
    summary: str
    action: str
    deep_link: str
    correlation_id: str
    baseline_import: bool
    occurred_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationDeliveryView:
    delivery_id: UUID
    route_id: UUID
    channel_type: str
    contract_version: int
    status: str
    attempts: int
    next_attempt_at: datetime
    last_error_code: str | None
    delivered_at: datetime | None
    exhausted_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationAttemptView:
    delivery_id: UUID
    attempt_number: int
    outcome: str | None
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class NotificationThreadView:
    id: UUID
    workspace_id: UUID
    recipient_type: str
    recipient_id: str
    category: str
    priority: str
    topic: str
    root_task_id: UUID
    root_task_title: str
    subtask_id: UUID | None
    subtask_title: str | None
    worker_item_id: UUID | None
    worker_item_title: str | None
    latest_fact_id: UUID
    latest_fact_occurred_at: datetime
    title: str
    summary: str
    action: str
    deep_link: str
    notification_ref: str
    business_status: str
    organization_status: str
    last_read_fact_id: UUID | None
    unread_count: int
    version: int
    opened_at: datetime
    resolved_at: datetime | None
    archived_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationPage[T]:
    items: tuple[T, ...]
    next_cursor: str | None
    last_change_cursor: int = 0


@dataclass(frozen=True, slots=True)
class NotificationThreadDetailView:
    thread: NotificationThreadView
    facts: NotificationPage[NotificationFactView]
    deliveries: tuple[NotificationDeliveryView, ...]
    available_commands: tuple[str, ...]
    last_change_cursor: int


class NotificationQueryStore(Protocol):
    def get_summary(
        self,
        workspace_id: UUID,
        *,
        recipient_type: str,
        recipient_id: str,
    ) -> NotificationSummaryView: ...

    def list_threads(
        self,
        workspace_id: UUID,
        *,
        recipient_type: str,
        recipient_id: str,
        category: str,
        root_task_id: UUID | None,
        subtask_id: UUID | None,
        unread_only: bool,
        cursor: str | None,
        limit: int,
    ) -> NotificationPage[NotificationThreadView]: ...

    def get_thread_detail(
        self,
        workspace_id: UUID,
        thread_id: UUID,
        *,
        recipient_type: str,
        recipient_id: str,
        cursor: str | None,
        limit: int,
    ) -> NotificationThreadDetailView | None: ...

    def list_attempts(
        self,
        workspace_id: UUID,
        delivery_id: UUID,
        *,
        recipient_type: str,
        recipient_id: str,
        cursor: str | None,
        limit: int,
    ) -> NotificationPage[NotificationAttemptView] | None: ...

    def list_route_threads(
        self,
        workspace_id: UUID,
        route_id: UUID,
        *,
        recipient_type: str,
        recipient_id: str,
        cursor: str | None,
        limit: int,
    ) -> NotificationPage[NotificationThreadView] | None: ...


class NotificationQueryService:
    def __init__(
        self,
        store: NotificationQueryStore,
        *,
        recipient_type: str = "workspace_operator",
        recipient_id: str = "local",
    ) -> None:
        self._store = store
        self._recipient_type = recipient_type
        self._recipient_id = recipient_id

    def get_summary(self, workspace_id: UUID) -> NotificationSummaryView:
        return self._store.get_summary(
            workspace_id,
            recipient_type=self._recipient_type,
            recipient_id=self._recipient_id,
        )

    def list_threads(
        self,
        workspace_id: UUID,
        *,
        category: str = "action",
        root_task_id: UUID | None = None,
        subtask_id: UUID | None = None,
        unread_only: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> NotificationPage[NotificationThreadView]:
        _validate_category(category)
        _validate_limit(limit)
        return self._store.list_threads(
            workspace_id,
            recipient_type=self._recipient_type,
            recipient_id=self._recipient_id,
            category=category,
            root_task_id=root_task_id,
            subtask_id=subtask_id,
            unread_only=unread_only,
            cursor=cursor,
            limit=limit,
        )

    def get_thread_detail(
        self,
        workspace_id: UUID,
        thread_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> NotificationThreadDetailView:
        _validate_limit(limit)
        detail = self._store.get_thread_detail(
            workspace_id,
            thread_id,
            recipient_type=self._recipient_type,
            recipient_id=self._recipient_id,
            cursor=cursor,
            limit=limit,
        )
        if detail is None:
            raise NotFound("notification thread not found")
        return detail

    def list_attempts(
        self,
        workspace_id: UUID,
        delivery_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> NotificationPage[NotificationAttemptView]:
        _validate_limit(limit)
        page = self._store.list_attempts(
            workspace_id,
            delivery_id,
            recipient_type=self._recipient_type,
            recipient_id=self._recipient_id,
            cursor=cursor,
            limit=limit,
        )
        if page is None:
            raise NotFound("notification delivery not found")
        return page

    def list_route_threads(
        self,
        workspace_id: UUID,
        route_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> NotificationPage[NotificationThreadView]:
        _validate_limit(limit)
        page = self._store.list_route_threads(
            workspace_id,
            route_id,
            recipient_type=self._recipient_type,
            recipient_id=self._recipient_id,
            cursor=cursor,
            limit=limit,
        )
        if page is None:
            raise NotFound("notification route not found")
        return page


def notification_ref(notification_id: UUID) -> str:
    return f"NTF-{notification_id.hex[:4].upper()}"


def _validate_category(category: str) -> None:
    if category not in NOTIFICATION_CATEGORIES:
        raise InvalidCursor("notification category is invalid")


def _validate_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
