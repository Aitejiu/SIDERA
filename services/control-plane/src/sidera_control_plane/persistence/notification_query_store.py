from __future__ import annotations

import base64
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, exists, or_, select
from sqlalchemy.orm import aliased

from sidera_control_plane.application.errors import InvalidCursor
from sidera_control_plane.application.notification_queries import (
    NotificationAttemptView,
    NotificationDeliveryView,
    NotificationFactView,
    NotificationPage,
    NotificationSummaryView,
    NotificationThreadDetailView,
    NotificationThreadView,
    notification_ref,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    NotificationDeliveryAttemptModel,
    NotificationDeliveryIntentModel,
    NotificationFactModel,
    NotificationRouteModel,
    NotificationThreadModel,
    TaskModel,
    WorkerItemModel,
)
from sidera_control_plane.persistence.projection_change_repository import (
    ProjectionChangeRepository,
)

_CURSOR_VERSION = 1
_MAX_CURSOR_BYTES = 4_096


class SqlAlchemyNotificationQueryStore:
    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._clock = clock or (lambda: datetime.now(UTC))

    def get_summary(
        self,
        workspace_id: UUID,
        *,
        recipient_type: str,
        recipient_id: str,
    ) -> NotificationSummaryView:
        with self._database.session() as session:
            rows = self._thread_rows(
                session,
                workspace_id,
                recipient_type=recipient_type,
                recipient_id=recipient_id,
            )
            today = self._clock().date()
            open_count = 0
            unread_count = 0
            progress_count = 0
            channel_issue_count = 0
            for thread, fact, _root, _subtask, _worker in rows:
                if (
                    thread.organization_status == "active"
                    and thread.business_status == "open"
                ):
                    open_count += 1
                if thread.organization_status == "active":
                    unread_count += thread.unread_count
                    if fact.priority == "operations":
                        channel_issue_count += 1
                if (
                    thread.organization_status == "active"
                    and thread.business_status == "resolved"
                    and fact.priority == "progress"
                    and _as_utc(fact.occurred_at).date() == today
                ):
                    progress_count += 1
            last_change_cursor = ProjectionChangeRepository(session).latest_cursor(
                workspace_id
            )
        return NotificationSummaryView(
            recipient_type=recipient_type,
            recipient_id=recipient_id,
            open_count=open_count,
            unread_count=unread_count,
            progress_count=progress_count,
            channel_issue_count=channel_issue_count,
            last_change_cursor=last_change_cursor,
        )

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
    ) -> NotificationPage[NotificationThreadView]:
        with self._database.session() as session:
            rows = self._list_thread_rows(
                session,
                workspace_id,
                recipient_type=recipient_type,
                recipient_id=recipient_id,
                category=category,
                root_task_id=root_task_id,
                subtask_id=subtask_id,
                unread_only=unread_only,
                cursor=cursor,
                limit=limit,
            )
            last_change_cursor = ProjectionChangeRepository(session).latest_cursor(
                workspace_id
            )
        page_rows = rows[:limit]
        return NotificationPage(
            items=tuple(_thread_view(row) for row in page_rows),
            next_cursor=(
                _encode_thread_cursor(page_rows[-1], category)
                if len(rows) > limit
                else None
            ),
            last_change_cursor=last_change_cursor,
        )

    def _thread_rows(
        self,
        session: Any,
        workspace_id: UUID,
        *,
        recipient_type: str,
        recipient_id: str,
    ) -> list[Any]:
        return list(
            session.execute(
                self._thread_statement(
                    session,
                    workspace_id,
                    recipient_type=recipient_type,
                    recipient_id=recipient_id,
                )
            ).all()
        )

    def get_thread_detail(
        self,
        workspace_id: UUID,
        thread_id: UUID,
        *,
        recipient_type: str,
        recipient_id: str,
        cursor: str | None,
        limit: int,
    ) -> NotificationThreadDetailView | None:
        with self._database.session() as session:
            row = session.execute(
                self._thread_statement(
                    session,
                    workspace_id,
                    recipient_type=recipient_type,
                    recipient_id=recipient_id,
                    extra_conditions=(NotificationThreadModel.id == thread_id,),
                )
            ).first()
            if row is None:
                return None
            thread_view = _thread_view(row)
            facts = self._list_facts(
                session,
                workspace_id,
                row[0],
                cursor=cursor,
                limit=limit,
            )
            deliveries = self._list_deliveries(session, workspace_id, row[0])
            last_change_cursor = ProjectionChangeRepository(session).latest_cursor(
                workspace_id
            )
        commands: list[str] = []
        if thread_view.unread_count > 0:
            commands.append("mark_read")
        if (
            thread_view.business_status == "resolved"
            and thread_view.organization_status == "active"
        ):
            commands.append("archive")
        return NotificationThreadDetailView(
            thread=thread_view,
            facts=facts,
            deliveries=deliveries,
            available_commands=tuple(commands),
            last_change_cursor=last_change_cursor,
        )

    def list_attempts(
        self,
        workspace_id: UUID,
        delivery_id: UUID,
        *,
        recipient_type: str,
        recipient_id: str,
        cursor: str | None,
        limit: int,
    ) -> NotificationPage[NotificationAttemptView] | None:
        with self._database.session() as session:
            intent = session.scalar(
                select(NotificationDeliveryIntentModel)
                .join(
                    NotificationFactModel,
                    and_(
                        NotificationFactModel.id
                        == NotificationDeliveryIntentModel.notification_fact_id,
                        NotificationFactModel.workspace_id
                        == NotificationDeliveryIntentModel.workspace_id,
                    ),
                )
                .where(
                    NotificationDeliveryIntentModel.workspace_id == workspace_id,
                    NotificationDeliveryIntentModel.id == delivery_id,
                    NotificationFactModel.recipient_type == recipient_type,
                    NotificationFactModel.recipient_id == recipient_id,
                )
            )
            if intent is None:
                return None
            start_after = _decode_attempt_cursor(cursor)
            statement = (
                select(NotificationDeliveryAttemptModel)
                .where(
                    NotificationDeliveryAttemptModel.delivery_intent_id
                    == delivery_id,
                )
                .order_by(NotificationDeliveryAttemptModel.attempt_number.asc())
            )
            if start_after is not None:
                statement = statement.where(
                    NotificationDeliveryAttemptModel.attempt_number > start_after
                )
            rows = session.scalars(statement.limit(limit + 1)).all()
        page_rows = rows[:limit]
        return NotificationPage(
            items=tuple(
                _attempt_view(delivery_id, row) for row in page_rows
            ),
            next_cursor=(
                _encode_attempt_cursor(page_rows[-1].attempt_number)
                if len(rows) > limit
                else None
            ),
        )

    def list_route_threads(
        self,
        workspace_id: UUID,
        route_id: UUID,
        *,
        recipient_type: str,
        recipient_id: str,
        cursor: str | None,
        limit: int,
    ) -> NotificationPage[NotificationThreadView] | None:
        with self._database.session() as session:
            route_exists = session.scalar(
                select(NotificationRouteModel.id).where(
                    NotificationRouteModel.workspace_id == workspace_id,
                    NotificationRouteModel.id == route_id,
                    NotificationRouteModel.recipient_type == recipient_type,
                    NotificationRouteModel.recipient_id == recipient_id,
                )
            )
            if route_exists is None:
                return None
            fact_for_thread = aliased(NotificationFactModel)
            route_intent_exists = exists(
                select(NotificationDeliveryIntentModel.id)
                .join(
                    fact_for_thread,
                    and_(
                        fact_for_thread.id
                        == NotificationDeliveryIntentModel.notification_fact_id,
                        fact_for_thread.workspace_id
                        == NotificationDeliveryIntentModel.workspace_id,
                    ),
                )
                .where(
                    NotificationDeliveryIntentModel.workspace_id
                    == NotificationThreadModel.workspace_id,
                    NotificationDeliveryIntentModel.notification_route_id
                    == route_id,
                    fact_for_thread.recipient_type
                    == NotificationThreadModel.recipient_type,
                    fact_for_thread.recipient_id == NotificationThreadModel.recipient_id,
                    fact_for_thread.root_task_id == NotificationThreadModel.root_task_id,
                    fact_for_thread.subtask_id.is_not_distinct_from(
                        NotificationThreadModel.subtask_id
                    ),
                    fact_for_thread.worker_item_id.is_not_distinct_from(
                        NotificationThreadModel.worker_item_id
                    ),
                    fact_for_thread.topic == NotificationThreadModel.topic,
                )
            )
            rows = self._list_thread_rows(
                session,
                workspace_id,
                recipient_type=recipient_type,
                recipient_id=recipient_id,
                category=None,
                root_task_id=None,
                subtask_id=None,
                unread_only=False,
                cursor=cursor,
                limit=limit,
                extra_conditions=(route_intent_exists,),
            )
            last_change_cursor = ProjectionChangeRepository(session).latest_cursor(
                workspace_id
            )
        page_rows = rows[:limit]
        return NotificationPage(
            items=tuple(_thread_view(row) for row in page_rows),
            next_cursor=(
                _encode_thread_cursor(page_rows[-1], None)
                if len(rows) > limit
                else None
            ),
            last_change_cursor=last_change_cursor,
        )

    def _list_thread_rows(
        self,
        session: Any,
        workspace_id: UUID,
        *,
        recipient_type: str,
        recipient_id: str,
        category: str | None,
        root_task_id: UUID | None,
        subtask_id: UUID | None,
        unread_only: bool,
        cursor: str | None,
        limit: int,
        extra_conditions: Sequence[Any] = (),
    ) -> list[Any]:
        conditions: list[Any] = list(extra_conditions)
        if category == "action":
            conditions.extend(
                (
                    NotificationThreadModel.organization_status == "active",
                    NotificationThreadModel.business_status == "open",
                )
            )
        elif category == "progress":
            conditions.extend(
                (
                    NotificationThreadModel.organization_status == "active",
                    NotificationThreadModel.business_status == "resolved",
                )
            )
        elif category == "archived":
            conditions.append(NotificationThreadModel.organization_status == "archived")
        if root_task_id is not None:
            conditions.append(NotificationThreadModel.root_task_id == root_task_id)
        if subtask_id is not None:
            conditions.append(NotificationThreadModel.subtask_id == subtask_id)
        if unread_only:
            conditions.append(NotificationThreadModel.unread_count > 0)

        statement = self._thread_statement(
            session,
            workspace_id,
            recipient_type=recipient_type,
            recipient_id=recipient_id,
            extra_conditions=tuple(conditions),
        )
        if category == "action":
            rank = _priority_rank(NotificationFactModel)
            decoded = _decode_thread_cursor(cursor, category)
            if decoded is not None:
                priority, opened_at, row_id = decoded
                statement = statement.where(
                    or_(
                        rank > priority,
                        and_(rank == priority, NotificationThreadModel.opened_at < opened_at),
                        and_(
                            rank == priority,
                            NotificationThreadModel.opened_at == opened_at,
                            NotificationThreadModel.id < row_id,
                        ),
                    )
                )
            statement = statement.order_by(
                rank.asc(),
                NotificationThreadModel.opened_at.desc(),
                NotificationThreadModel.id.desc(),
            )
        else:
            decoded = _decode_thread_cursor(cursor, category)
            if decoded is not None:
                _priority, occurred_at, row_id = decoded
                statement = statement.where(
                    or_(
                        NotificationFactModel.occurred_at < occurred_at,
                        and_(
                            NotificationFactModel.occurred_at == occurred_at,
                            NotificationThreadModel.id < row_id,
                        ),
                    )
                )
            statement = statement.order_by(
                NotificationFactModel.occurred_at.desc(),
                NotificationThreadModel.id.desc(),
            )
        return list(session.execute(statement.limit(limit + 1)).all())

    def _thread_statement(
        self,
        session: Any,
        workspace_id: UUID,
        *,
        recipient_type: str,
        recipient_id: str,
        extra_conditions: Sequence[Any] = (),
    ) -> Any:
        root_task = aliased(TaskModel)
        subtask = aliased(TaskModel)
        worker_item = aliased(WorkerItemModel)
        conditions = [
            NotificationThreadModel.workspace_id == workspace_id,
            NotificationThreadModel.recipient_type == recipient_type,
            NotificationThreadModel.recipient_id == recipient_id,
            *extra_conditions,
        ]
        return (
            select(
                NotificationThreadModel,
                NotificationFactModel,
                root_task,
                subtask,
                worker_item,
            )
            .join(
                NotificationFactModel,
                and_(
                    NotificationFactModel.id
                    == NotificationThreadModel.latest_fact_id,
                    NotificationFactModel.workspace_id
                    == NotificationThreadModel.workspace_id,
                ),
            )
            .join(
                root_task,
                and_(
                    root_task.id == NotificationThreadModel.root_task_id,
                    root_task.workspace_id == NotificationThreadModel.workspace_id,
                ),
            )
            .outerjoin(
                subtask,
                and_(
                    subtask.id == NotificationThreadModel.subtask_id,
                    subtask.workspace_id == NotificationThreadModel.workspace_id,
                ),
            )
            .outerjoin(
                worker_item,
                and_(
                    worker_item.id == NotificationThreadModel.worker_item_id,
                    worker_item.workspace_id == NotificationThreadModel.workspace_id,
                ),
            )
            .where(*conditions)
        )

    def _list_facts(
        self,
        session: Any,
        workspace_id: UUID,
        thread: Any,
        *,
        cursor: str | None,
        limit: int,
    ) -> NotificationPage[NotificationFactView]:
        conditions = [
            NotificationFactModel.workspace_id == workspace_id,
            NotificationFactModel.recipient_type == thread.recipient_type,
            NotificationFactModel.recipient_id == thread.recipient_id,
            NotificationFactModel.root_task_id == thread.root_task_id,
            NotificationFactModel.subtask_id.is_not_distinct_from(thread.subtask_id),
            NotificationFactModel.worker_item_id.is_not_distinct_from(
                thread.worker_item_id
            ),
            NotificationFactModel.topic == thread.topic,
        ]
        decoded = _decode_fact_cursor(cursor)
        if decoded is not None:
            occurred_at, row_id = decoded
            conditions.append(
                or_(
                    NotificationFactModel.occurred_at < occurred_at,
                    and_(
                        NotificationFactModel.occurred_at == occurred_at,
                        NotificationFactModel.id < row_id,
                    ),
                )
            )
        rows = session.scalars(
            select(NotificationFactModel)
            .where(*conditions)
            .order_by(
                NotificationFactModel.occurred_at.desc(),
                NotificationFactModel.id.desc(),
            )
            .limit(limit + 1)
        ).all()
        page_rows = rows[:limit]
        return NotificationPage(
            items=tuple(_fact_view(thread.id, row) for row in page_rows),
            next_cursor=(
                _encode_fact_cursor(page_rows[-1]) if len(rows) > limit else None
            ),
        )

    @staticmethod
    def _list_deliveries(
        session: Any, workspace_id: UUID, thread: Any
    ) -> tuple[NotificationDeliveryView, ...]:
        rows = session.execute(
            select(NotificationDeliveryIntentModel, NotificationRouteModel)
            .join(
                NotificationRouteModel,
                and_(
                    NotificationRouteModel.id
                    == NotificationDeliveryIntentModel.notification_route_id,
                    NotificationRouteModel.workspace_id
                    == NotificationDeliveryIntentModel.workspace_id,
                ),
            )
            .join(
                NotificationFactModel,
                and_(
                    NotificationFactModel.id
                    == NotificationDeliveryIntentModel.notification_fact_id,
                    NotificationFactModel.workspace_id
                    == NotificationDeliveryIntentModel.workspace_id,
                ),
            )
            .where(
                NotificationDeliveryIntentModel.workspace_id == workspace_id,
                NotificationFactModel.recipient_type == thread.recipient_type,
                NotificationFactModel.recipient_id == thread.recipient_id,
                NotificationFactModel.root_task_id == thread.root_task_id,
                NotificationFactModel.subtask_id.is_not_distinct_from(
                    thread.subtask_id
                ),
                NotificationFactModel.worker_item_id.is_not_distinct_from(
                    thread.worker_item_id
                ),
                NotificationFactModel.topic == thread.topic,
            )
            .order_by(
                case(
                    (NotificationFactModel.id == thread.latest_fact_id, 0),
                    else_=1,
                ).asc(),
                NotificationDeliveryIntentModel.created_at.desc(),
                NotificationDeliveryIntentModel.id.desc(),
            )
        ).all()
        latest_by_route: dict[UUID, Any] = {}
        for intent, route in rows:
            latest_by_route.setdefault(route.id, (intent, route))
        return tuple(
            _delivery_view(intent, route)
            for intent, route in latest_by_route.values()
        )


def _thread_view(row: Any) -> NotificationThreadView:
    thread, fact, root_task, subtask, worker_item = row
    category = (
        "archived"
        if thread.organization_status == "archived"
        else "action"
        if thread.business_status == "open"
        else "progress"
    )
    return NotificationThreadView(
        id=thread.id,
        workspace_id=thread.workspace_id,
        recipient_type=thread.recipient_type,
        recipient_id=thread.recipient_id,
        category=category,
        priority=fact.priority,
        topic=thread.topic,
        root_task_id=thread.root_task_id,
        root_task_title=root_task.title,
        subtask_id=thread.subtask_id,
        subtask_title=subtask.title if subtask is not None else None,
        worker_item_id=thread.worker_item_id,
        worker_item_title=worker_item.title if worker_item is not None else None,
        latest_fact_id=thread.latest_fact_id,
        latest_fact_occurred_at=_as_utc(fact.occurred_at),
        title=fact.title,
        summary=fact.summary,
        action=fact.action,
        deep_link=fact.deep_link,
        notification_ref=notification_ref(fact.id),
        business_status=thread.business_status,
        organization_status=thread.organization_status,
        last_read_fact_id=thread.last_read_fact_id,
        unread_count=thread.unread_count,
        version=thread.version,
        opened_at=_as_utc(thread.opened_at),
        resolved_at=_optional_utc(thread.resolved_at),
        archived_at=_optional_utc(thread.archived_at),
        updated_at=_as_utc(thread.updated_at),
    )


def _fact_view(thread_id: UUID, row: Any) -> NotificationFactView:
    return NotificationFactView(
        id=row.id,
        thread_id=thread_id,
        policy_version=row.policy_version,
        kind=row.kind,
        priority=row.priority,
        transition=row.transition,
        topic=row.topic,
        root_task_id=row.root_task_id,
        subtask_id=row.subtask_id,
        worker_item_id=row.worker_item_id,
        source_projection_cursor=row.source_projection_cursor,
        source_projection_type=row.source_projection_type,
        source_resource_id=row.source_resource_id,
        source_resource_version=row.source_resource_version,
        title=row.title,
        summary=row.summary,
        action=row.action,
        deep_link=row.deep_link,
        correlation_id=row.correlation_id,
        baseline_import=row.baseline_import,
        occurred_at=_as_utc(row.occurred_at),
        created_at=_as_utc(row.created_at),
    )


def _delivery_view(intent: Any, route: Any) -> NotificationDeliveryView:
    return NotificationDeliveryView(
        delivery_id=intent.id,
        route_id=route.id,
        channel_type=route.channel_type,
        contract_version=intent.contract_version,
        status=intent.status,
        attempts=intent.attempts,
        next_attempt_at=_as_utc(intent.next_attempt_at),
        last_error_code=intent.last_error_code,
        delivered_at=_optional_utc(intent.delivered_at),
        exhausted_at=_optional_utc(intent.exhausted_at),
        created_at=_as_utc(intent.created_at),
        updated_at=_as_utc(intent.updated_at),
    )


def _attempt_view(delivery_id: UUID, row: Any) -> NotificationAttemptView:
    return NotificationAttemptView(
        delivery_id=delivery_id,
        attempt_number=row.attempt_number,
        outcome=row.outcome,
        error_code=row.error_code,
        started_at=_as_utc(row.started_at),
        finished_at=_optional_utc(row.finished_at),
    )


def _priority_rank(model: Any) -> Any:
    return case(
        (model.priority == "action", 0),
        (model.priority == "operations", 1),
        else_=2,
    )


def _encode_thread_cursor(row: Any, category: str | None) -> str:
    thread, fact, _root, _subtask, _worker = row
    payload: dict[str, object] = {
        "v": _CURSOR_VERSION,
        "kind": "thread",
        "category": category,
        "id": str(thread.id),
    }
    if category == "action":
        payload["priority"] = _priority_value(fact.priority)
        payload["opened_at"] = _as_utc(thread.opened_at).isoformat()
    else:
        payload["priority"] = 0
        payload["occurred_at"] = _as_utc(fact.occurred_at).isoformat()
    return _encode_cursor(payload)


def _decode_thread_cursor(
    cursor: str | None, category: str | None
) -> tuple[int, datetime, UUID] | None:
    if cursor is None:
        return None
    payload = _decode_cursor(cursor, kind="thread")
    if payload.get("category") != category:
        raise InvalidCursor("notification thread cursor does not match category")
    try:
        row_id = UUID(str(payload["id"]))
        priority = int(payload.get("priority", 0))
        timestamp_key = "opened_at" if category == "action" else "occurred_at"
        timestamp = _parse_datetime(payload[timestamp_key])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursor("invalid notification thread cursor") from exc
    if priority < 0:
        raise InvalidCursor("invalid notification thread cursor")
    return priority, timestamp, row_id


def _encode_fact_cursor(row: Any) -> str:
    return _encode_cursor(
        {
            "v": _CURSOR_VERSION,
            "kind": "fact",
            "occurred_at": _as_utc(row.occurred_at).isoformat(),
            "id": str(row.id),
        }
    )


def _decode_fact_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if cursor is None:
        return None
    payload = _decode_cursor(cursor, kind="fact")
    try:
        return _parse_datetime(payload["occurred_at"]), UUID(str(payload["id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursor("invalid notification fact cursor") from exc


def _encode_attempt_cursor(attempt_number: int) -> str:
    return _encode_cursor(
        {"v": _CURSOR_VERSION, "kind": "attempt", "number": attempt_number}
    )


def _decode_attempt_cursor(cursor: str | None) -> int | None:
    if cursor is None:
        return None
    payload = _decode_cursor(cursor, kind="attempt")
    value = payload.get("number")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidCursor("invalid notification attempt cursor")
    return value


def _encode_cursor(payload: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, *, kind: str) -> dict[str, Any]:
    if not isinstance(cursor, str) or not cursor or len(cursor) > _MAX_CURSOR_BYTES:
        raise InvalidCursor("invalid notification cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidCursor("invalid notification cursor") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != _CURSOR_VERSION
        or payload.get("kind") != kind
    ):
        raise InvalidCursor("invalid notification cursor")
    return payload


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("cursor timestamp must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("cursor timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _priority_value(value: str) -> int:
    return {"action": 0, "operations": 1, "progress": 2}.get(value, 3)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("notification query timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None
