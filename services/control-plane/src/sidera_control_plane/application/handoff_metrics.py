from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

HANDOFF_METRIC_NAMES = frozenset(
    {
        "sidera_handoff_proposals_total",
        "sidera_handoff_transitions",
        "sidera_handoff_duration_seconds",
        "sidera_handoff_gateway_operations_total",
        "sidera_handoff_recoveries_total",
    }
)
_METRIC_LABELS = {
    "sidera_handoff_proposals_total": frozenset({"outcome"}),
    "sidera_handoff_transitions": frozenset({"status", "phase"}),
    "sidera_handoff_duration_seconds": frozenset({"outcome", "transfer_mode"}),
    "sidera_handoff_gateway_operations_total": frozenset(
        {"phase", "outcome", "adapter_type"}
    ),
    "sidera_handoff_recoveries_total": frozenset({"strategy", "outcome"}),
}


@dataclass(frozen=True, slots=True)
class HandoffMetricSample:
    name: str
    labels: tuple[tuple[str, str], ...]
    value: float

    def __post_init__(self) -> None:
        expected = _METRIC_LABELS.get(self.name)
        if expected is None:
            raise ValueError(f"unsupported handoff metric: {self.name}")
        if {key for key, _ in self.labels} != expected:
            raise ValueError(f"invalid labels for handoff metric: {self.name}")
        if self.labels != tuple(sorted(self.labels)):
            raise ValueError("handoff metric labels must be sorted")
        if any(not key or not value for key, value in self.labels):
            raise ValueError("handoff metric labels cannot be empty")


@dataclass(frozen=True, slots=True)
class HandoffMetricsView:
    workspace_id: UUID
    as_of: datetime
    samples: tuple[HandoffMetricSample, ...]


class HandoffMetricsStore(Protocol):
    def get_metrics(self, workspace_id: UUID) -> HandoffMetricsView:
        raise NotImplementedError


class HandoffMetricsService:
    def __init__(self, store: HandoffMetricsStore) -> None:
        self._store = store

    def get_metrics(self, workspace_id: UUID) -> HandoffMetricsView:
        return self._store.get_metrics(workspace_id)
