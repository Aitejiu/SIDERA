from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

COLLABORATION_METRIC_NAMES = frozenset(
    {
        "sidera_collaboration_evaluations_total",
        "sidera_collaboration_evaluation_duration_seconds",
        "sidera_collaboration_evaluation_queue_age_seconds",
        "sidera_collaboration_runs",
        "sidera_collaboration_decisions_total",
        "sidera_collaboration_intents_total",
        "sidera_collaboration_releases_total",
        "sidera_collaboration_retries_total",
    }
)

_METRIC_LABELS = {
    "sidera_collaboration_evaluations_total": frozenset({"outcome"}),
    "sidera_collaboration_evaluation_duration_seconds": frozenset({"outcome"}),
    "sidera_collaboration_evaluation_queue_age_seconds": frozenset({"outcome"}),
    "sidera_collaboration_runs": frozenset({"status"}),
    "sidera_collaboration_decisions_total": frozenset({"outcome"}),
    "sidera_collaboration_intents_total": frozenset({"outcome"}),
    "sidera_collaboration_releases_total": frozenset({"outcome"}),
    "sidera_collaboration_retries_total": frozenset({"reason"}),
}

_ALLOWED_LABEL_VALUES = {
    "outcome": frozenset(
        {
            "pending",
            "evaluating",
            "retry_wait",
            "completed",
            "blocked",
            "intents",
            "no_change",
            "applied",
            "rejected",
            "stale",
            "duplicate",
            "issued",
            "none",
        }
    ),
    "status": frozenset(
        {"pending", "active", "blocked", "completed", "superseded", "none"}
    ),
    "reason": frozenset(
        {
            "evaluation_lease_expired",
            "runtime_timeout",
            "runtime_unavailable",
            "runtime_output_invalid",
            "projection_too_large",
            "unsupported_runtime_capability",
            "runtime_config_mismatch",
            "worker_phase_failed",
            "unknown",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class CollaborationMetricSample:
    name: str
    labels: tuple[tuple[str, str], ...]
    value: float

    def __post_init__(self) -> None:
        expected = _METRIC_LABELS.get(self.name)
        if expected is None:
            raise ValueError(f"unsupported collaboration metric: {self.name}")
        if {key for key, _ in self.labels} != expected:
            raise ValueError(f"invalid labels for collaboration metric: {self.name}")
        if self.labels != tuple(sorted(self.labels)):
            raise ValueError("collaboration metric labels must be sorted")
        if any(not key or not value for key, value in self.labels):
            raise ValueError("collaboration metric labels cannot be empty")
        for key, value in self.labels:
            if value not in _ALLOWED_LABEL_VALUES[key]:
                raise ValueError(f"unsupported collaboration metric label: {value}")
        if self.value < 0:
            raise ValueError("collaboration metric values cannot be negative")


@dataclass(frozen=True, slots=True)
class CollaborationMetricsView:
    workspace_id: UUID
    as_of: datetime
    samples: tuple[CollaborationMetricSample, ...]


class CollaborationMetricsStore(Protocol):
    def get_collaboration_metrics(self, workspace_id: UUID) -> CollaborationMetricsView:
        raise NotImplementedError


class CollaborationMetricsService:
    def __init__(self, store: CollaborationMetricsStore) -> None:
        self._store = store

    def get_metrics(self, workspace_id: UUID) -> CollaborationMetricsView:
        return self._store.get_collaboration_metrics(workspace_id)
