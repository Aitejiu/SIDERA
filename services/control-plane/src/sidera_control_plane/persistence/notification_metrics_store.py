from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select

from sidera_control_plane.application.notification_metrics import (
    NOTIFICATION_ATTEMPT_OUTCOMES,
    NOTIFICATION_BUSINESS_STATUSES,
    NOTIFICATION_CHANNEL_TYPES,
    NOTIFICATION_DELIVERY_STATUSES,
    NOTIFICATION_ERROR_CODES,
    NOTIFICATION_KINDS,
    NOTIFICATION_ORGANIZATION_STATUSES,
    NOTIFICATION_PRIORITIES,
    NOTIFICATION_RECONCILE_OUTCOMES,
    NOTIFICATION_RESYNC_REASONS,
    NotificationMetricSample,
    NotificationMetricsView,
    canonical_notification_channel_type,
    canonical_notification_error_code,
    canonical_notification_kind,
)
from sidera_control_plane.application.notification_policy import POLICY_VERSION
from sidera_control_plane.domain.notifications import (
    NotificationAttemptOutcome,
    NotificationDeliveryStatus,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    NotificationDeliveryAttemptModel,
    NotificationDeliveryIntentModel,
    NotificationFactModel,
    NotificationReconciliationCursorModel,
    NotificationRouteModel,
    NotificationThreadModel,
    ProjectionChangeModel,
    WorkspaceProjectionCursorModel,
)


class SqlAlchemyNotificationMetricsStore:
    """Read notification metrics from bounded SQL aggregates for one Workspace."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def get_metrics(self, workspace_id: UUID) -> NotificationMetricsView:
        now = datetime.now(UTC)
        values = _zero_metric_values()
        with self._database.session() as session:
            fact_rows = session.execute(
                select(
                    NotificationFactModel.kind,
                    NotificationFactModel.priority,
                    func.count(),
                )
                .where(NotificationFactModel.workspace_id == workspace_id)
                .group_by(NotificationFactModel.kind, NotificationFactModel.priority)
            ).all()
            thread_rows = session.execute(
                select(
                    NotificationThreadModel.business_status,
                    NotificationThreadModel.organization_status,
                    func.count(),
                )
                .where(NotificationThreadModel.workspace_id == workspace_id)
                .group_by(
                    NotificationThreadModel.business_status,
                    NotificationThreadModel.organization_status,
                )
            ).all()
            delivery_rows = session.execute(
                select(
                    NotificationRouteModel.channel_type,
                    NotificationDeliveryIntentModel.status,
                    func.count(),
                )
                .join(
                    NotificationRouteModel,
                    and_(
                        NotificationRouteModel.id
                        == NotificationDeliveryIntentModel.notification_route_id,
                        NotificationRouteModel.workspace_id
                        == NotificationDeliveryIntentModel.workspace_id,
                    ),
                )
                .where(NotificationDeliveryIntentModel.workspace_id == workspace_id)
                .group_by(
                    NotificationRouteModel.channel_type,
                    NotificationDeliveryIntentModel.status,
                )
            ).all()
            attempt_rows = session.execute(
                select(
                    NotificationRouteModel.channel_type,
                    NotificationDeliveryAttemptModel.outcome,
                    NotificationDeliveryAttemptModel.error_code,
                    func.count(),
                )
                .join(
                    NotificationDeliveryIntentModel,
                    NotificationDeliveryIntentModel.id
                    == NotificationDeliveryAttemptModel.delivery_intent_id,
                )
                .join(
                    NotificationRouteModel,
                    and_(
                        NotificationRouteModel.id
                        == NotificationDeliveryIntentModel.notification_route_id,
                        NotificationRouteModel.workspace_id
                        == NotificationDeliveryIntentModel.workspace_id,
                    ),
                )
                .where(NotificationDeliveryIntentModel.workspace_id == workspace_id)
                .group_by(
                    NotificationRouteModel.channel_type,
                    NotificationDeliveryAttemptModel.outcome,
                    NotificationDeliveryAttemptModel.error_code,
                )
            ).all()
            oldest_delivery_rows = session.execute(
                select(
                    NotificationRouteModel.channel_type,
                    func.min(NotificationDeliveryIntentModel.created_at),
                )
                .join(
                    NotificationRouteModel,
                    and_(
                        NotificationRouteModel.id
                        == NotificationDeliveryIntentModel.notification_route_id,
                        NotificationRouteModel.workspace_id
                        == NotificationDeliveryIntentModel.workspace_id,
                    ),
                )
                .where(
                    NotificationDeliveryIntentModel.workspace_id == workspace_id,
                    NotificationDeliveryIntentModel.status.in_(
                        (
                            NotificationDeliveryStatus.PENDING.value,
                            NotificationDeliveryStatus.CLAIMED.value,
                            NotificationDeliveryStatus.RETRY_WAIT.value,
                        )
                    ),
                )
                .group_by(NotificationRouteModel.channel_type)
            ).all()
            cursor = session.scalar(
                select(NotificationReconciliationCursorModel).where(
                    NotificationReconciliationCursorModel.workspace_id == workspace_id,
                    NotificationReconciliationCursorModel.policy_version
                    == POLICY_VERSION,
                )
            )
            latest_projection = int(
                session.scalar(
                    select(func.max(ProjectionChangeModel.cursor)).where(
                        ProjectionChangeModel.workspace_id == workspace_id
                    )
                )
                or 0
            )
            latest_workspace_cursor = int(
                session.scalar(
                    select(WorkspaceProjectionCursorModel.last_cursor).where(
                        WorkspaceProjectionCursorModel.workspace_id == workspace_id
                    )
                )
                or 0
            )
            baseline_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(NotificationFactModel)
                    .where(
                        NotificationFactModel.workspace_id == workspace_id,
                        NotificationFactModel.baseline_import.is_(True),
                    )
                )
                or 0
            )
            exhausted_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(NotificationDeliveryIntentModel)
                    .where(
                        NotificationDeliveryIntentModel.workspace_id == workspace_id,
                        NotificationDeliveryIntentModel.status
                        == NotificationDeliveryStatus.EXHAUSTED.value,
                    )
                )
                or 0
            )

            for kind, priority, count in fact_rows:
                _increment(
                    values,
                    "sidera_notification_fact_total",
                    {"kind": canonical_notification_kind(kind), "priority": priority},
                    count,
                )
            for business_status, organization_status, count in thread_rows:
                _increment(
                    values,
                    "sidera_notification_thread_count",
                    {
                        "business_status": business_status,
                        "organization_status": organization_status,
                    },
                    count,
                )
            for channel_type, status, count in delivery_rows:
                _increment(
                    values,
                    "sidera_notification_delivery_count",
                    {
                        "channel_type": canonical_notification_channel_type(
                            channel_type
                        ),
                        "status": status,
                    },
                    count,
                )
            for channel_type, outcome, error_code, count in attempt_rows:
                _increment(
                    values,
                    "sidera_notification_delivery_attempt_total",
                    {
                        "channel_type": canonical_notification_channel_type(
                            channel_type
                        ),
                        "outcome": _attempt_outcome(outcome),
                        "error_code": canonical_notification_error_code(error_code),
                    },
                    count,
                )
            for channel_type, oldest_created_at in oldest_delivery_rows:
                _set_max(
                    values,
                    "sidera_notification_delivery_oldest_seconds",
                    {"channel_type": canonical_notification_channel_type(channel_type)},
                    _age_seconds(now, oldest_created_at),
                )

            last_cursor = int(cursor.last_projection_cursor) if cursor else 0
            latest_cursor = max(latest_projection, latest_workspace_cursor)
            reconcile_lag = max(0, latest_cursor - last_cursor)
            _set_value(
                values,
                "sidera_notification_reconcile_lag",
                {},
                reconcile_lag,
            )
            oldest_reconcile_at = session.scalar(
                select(func.min(ProjectionChangeModel.occurred_at)).where(
                    ProjectionChangeModel.workspace_id == workspace_id,
                    ProjectionChangeModel.cursor > last_cursor,
                )
            )
            _set_value(
                values,
                "sidera_notification_reconcile_oldest_seconds",
                {},
                _age_seconds(now, oldest_reconcile_at),
            )
            if cursor is not None and cursor.last_reconciled_at is not None:
                _increment(
                    values,
                    "sidera_notification_reconcile_total",
                    {"outcome": "success"},
                    1,
                )
            if reconcile_lag > 0:
                _increment(
                    values,
                    "sidera_notification_resync_total",
                    {"reason": "cursor_gap"},
                    1,
                )
            if baseline_count:
                _increment(
                    values,
                    "sidera_notification_resync_total",
                    {"reason": "baseline"},
                    baseline_count,
                )
            if exhausted_count:
                _increment(
                    values,
                    "sidera_notification_reconcile_total",
                    {"outcome": "failure"},
                    exhausted_count,
                )

        return NotificationMetricsView(
            workspace_id=workspace_id,
            as_of=now,
            samples=tuple(
                NotificationMetricSample(name=name, labels=labels, value=value)
                for (name, labels), value in sorted(values.items())
            ),
        )


def _zero_metric_values() -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for outcome in NOTIFICATION_RECONCILE_OUTCOMES:
        _set_value(values, "sidera_notification_reconcile_total", {"outcome": outcome}, 0)
    _set_value(values, "sidera_notification_reconcile_lag", {}, 0)
    _set_value(values, "sidera_notification_reconcile_oldest_seconds", {}, 0)
    for kind in NOTIFICATION_KINDS:
        for priority in NOTIFICATION_PRIORITIES:
            _set_value(
                values,
                "sidera_notification_fact_total",
                {"kind": kind, "priority": priority},
                0,
            )
    for business_status in NOTIFICATION_BUSINESS_STATUSES:
        for organization_status in NOTIFICATION_ORGANIZATION_STATUSES:
            _set_value(
                values,
                "sidera_notification_thread_count",
                {
                    "business_status": business_status,
                    "organization_status": organization_status,
                },
                0,
            )
    for channel_type in NOTIFICATION_CHANNEL_TYPES:
        for status in NOTIFICATION_DELIVERY_STATUSES:
            _set_value(
                values,
                "sidera_notification_delivery_count",
                {"channel_type": channel_type, "status": status},
                0,
            )
        _set_value(
            values,
            "sidera_notification_delivery_oldest_seconds",
            {"channel_type": channel_type},
            0,
        )
        for outcome in NOTIFICATION_ATTEMPT_OUTCOMES:
            for error_code in NOTIFICATION_ERROR_CODES:
                _set_value(
                    values,
                    "sidera_notification_delivery_attempt_total",
                    {
                        "channel_type": channel_type,
                        "outcome": outcome,
                        "error_code": error_code,
                    },
                    0,
                )
    for reason in NOTIFICATION_RESYNC_REASONS:
        _set_value(values, "sidera_notification_resync_total", {"reason": reason}, 0)
    return values


def _set_value(
    values: dict[tuple[str, tuple[tuple[str, str], ...]], float],
    name: str,
    labels: dict[str, object],
    value: float,
) -> None:
    label_tuple = tuple(sorted((key, str(value)) for key, value in labels.items()))
    values[(name, label_tuple)] = float(value)


def _increment(
    values: dict[tuple[str, tuple[tuple[str, str], ...]], float],
    name: str,
    labels: dict[str, object],
    value: float,
) -> None:
    label_tuple = tuple(sorted((key, str(value)) for key, value in labels.items()))
    values[(name, label_tuple)] = values.get((name, label_tuple), 0.0) + float(value)


def _set_max(
    values: dict[tuple[str, tuple[tuple[str, str], ...]], float],
    name: str,
    labels: dict[str, object],
    value: float,
) -> None:
    label_tuple = tuple(sorted((key, str(value)) for key, value in labels.items()))
    values[(name, label_tuple)] = max(values.get((name, label_tuple), 0.0), value)


def _attempt_outcome(value: object | None) -> str:
    if value is None:
        return "open"
    normalized = str(getattr(value, "value", value))
    return (
        normalized
        if normalized in {item.value for item in NotificationAttemptOutcome}
        else "open"
    )


def _age_seconds(now: datetime, value: Any) -> float:
    if not isinstance(value, datetime):
        return 0.0
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return max(0.0, (now - value.astimezone(UTC)).total_seconds())
