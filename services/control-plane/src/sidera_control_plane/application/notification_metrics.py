from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Protocol
from uuid import UUID

from sidera_control_plane.domain.notifications import (
    NotificationAttemptErrorCode,
    NotificationAttemptOutcome,
    NotificationDeliveryStatus,
    NotificationKind,
    NotificationPriority,
)

NOTIFICATION_METRIC_NAMES = frozenset(
    {
        "sidera_notification_reconcile_total",
        "sidera_notification_reconcile_lag",
        "sidera_notification_reconcile_oldest_seconds",
        "sidera_notification_fact_total",
        "sidera_notification_thread_count",
        "sidera_notification_delivery_count",
        "sidera_notification_delivery_attempt_total",
        "sidera_notification_delivery_oldest_seconds",
        "sidera_notification_resync_total",
    }
)

NOTIFICATION_METRIC_LABELS = {
    "sidera_notification_reconcile_total": frozenset({"outcome"}),
    "sidera_notification_reconcile_lag": frozenset(),
    "sidera_notification_reconcile_oldest_seconds": frozenset(),
    "sidera_notification_fact_total": frozenset({"kind", "priority"}),
    "sidera_notification_thread_count": frozenset(
        {"business_status", "organization_status"}
    ),
    "sidera_notification_delivery_count": frozenset({"channel_type", "status"}),
    "sidera_notification_delivery_attempt_total": frozenset(
        {"channel_type", "outcome", "error_code"}
    ),
    "sidera_notification_delivery_oldest_seconds": frozenset({"channel_type"}),
    "sidera_notification_resync_total": frozenset({"reason"}),
}

NOTIFICATION_RECONCILE_OUTCOMES = (
    "success",
    "failure",
    "busy",
    "unknown",
)
NOTIFICATION_CHANNEL_TYPES = (
    "akashic_private",
    "telegram",
    "web",
    "other",
)
NOTIFICATION_DELIVERY_STATUSES = tuple(item.value for item in NotificationDeliveryStatus)
NOTIFICATION_ATTEMPT_OUTCOMES = tuple(item.value for item in NotificationAttemptOutcome) + (
    "open",
)
NOTIFICATION_ERROR_CODES = tuple(item.value for item in NotificationAttemptErrorCode) + (
    "none",
    "other",
)
NOTIFICATION_PRIORITIES = tuple(item.value for item in NotificationPriority)
NOTIFICATION_BUSINESS_STATUSES = ("open", "resolved")
NOTIFICATION_ORGANIZATION_STATUSES = ("active", "archived")
NOTIFICATION_RESYNC_REASONS = (
    "baseline",
    "cursor_gap",
    "observation_invalid",
    "unknown",
)

_KNOWN_NOTIFICATION_KINDS = tuple(item.value for item in NotificationKind) + (
    "task_recovered",
    "plan_generation_converged",
    "planning_materialized",
    "handoff_converged",
    "proposal_ready",
    "proposal_stale",
    "generation_published",
    "subtask_converged",
    "transition_degraded",
    "worker_blocked_unknown",
    "all_converged",
    "handoff_attention_required",
    "handoff_target_took_over",
)
NOTIFICATION_KINDS = _KNOWN_NOTIFICATION_KINDS + ("other",)

_ALLOWED_LABEL_VALUES = {
    "outcome": frozenset(
        {*NOTIFICATION_RECONCILE_OUTCOMES, *NOTIFICATION_ATTEMPT_OUTCOMES}
    ),
    "kind": frozenset(NOTIFICATION_KINDS),
    "priority": frozenset(NOTIFICATION_PRIORITIES),
    "business_status": frozenset(NOTIFICATION_BUSINESS_STATUSES),
    "organization_status": frozenset(NOTIFICATION_ORGANIZATION_STATUSES),
    "channel_type": frozenset(NOTIFICATION_CHANNEL_TYPES),
    "status": frozenset(NOTIFICATION_DELIVERY_STATUSES),
    "error_code": frozenset(NOTIFICATION_ERROR_CODES),
    "reason": frozenset(NOTIFICATION_RESYNC_REASONS),
}


@dataclass(frozen=True, slots=True)
class NotificationMetricSample:
    name: str
    labels: tuple[tuple[str, str], ...]
    value: float

    def __post_init__(self) -> None:
        expected = NOTIFICATION_METRIC_LABELS.get(self.name)
        if expected is None:
            raise ValueError(f"unsupported notification metric: {self.name}")
        if {key for key, _ in self.labels} != expected:
            raise ValueError(f"invalid labels for notification metric: {self.name}")
        if self.labels != tuple(sorted(self.labels)):
            raise ValueError("notification metric labels must be sorted")
        if any(not key or not value for key, value in self.labels):
            raise ValueError("notification metric labels cannot be empty")
        for key, value in self.labels:
            if value not in _ALLOWED_LABEL_VALUES[key]:
                raise ValueError(f"unsupported notification metric label: {value}")
        if self.value < 0 or not isfinite(self.value):
            raise ValueError(
                "notification metric values must be finite and non-negative"
            )


@dataclass(frozen=True, slots=True)
class NotificationMetricsView:
    workspace_id: UUID
    as_of: datetime
    samples: tuple[NotificationMetricSample, ...]


class NotificationMetricsStore(Protocol):
    def get_metrics(self, workspace_id: UUID) -> NotificationMetricsView: ...


class NotificationMetricsService:
    def __init__(self, store: NotificationMetricsStore) -> None:
        self._store = store

    def get_metrics(self, workspace_id: UUID) -> NotificationMetricsView:
        return self._store.get_metrics(workspace_id)


def canonical_notification_kind(value: object) -> str:
    normalized = str(getattr(value, "value", value))
    return normalized if normalized in _ALLOWED_LABEL_VALUES["kind"] else "other"


def canonical_notification_channel_type(value: object) -> str:
    normalized = str(getattr(value, "value", value))
    return (
        normalized
        if normalized in _ALLOWED_LABEL_VALUES["channel_type"]
        else "other"
    )


def canonical_notification_error_code(value: object | None) -> str:
    if value is None:
        return "none"
    normalized = str(getattr(value, "value", value))
    return (
        normalized
        if normalized in _ALLOWED_LABEL_VALUES["error_code"]
        else "other"
    )


def canonical_notification_labels(
    labels: dict[str, object],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (key, str(getattr(value, "value", value)))
            for key, value in labels.items()
        )
    )
