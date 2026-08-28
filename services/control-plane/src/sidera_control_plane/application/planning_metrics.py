from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PlanningMetricReasonView:
    reason: str
    count: int


@dataclass(frozen=True, slots=True)
class PlanningWorkerTransitionMetricView:
    action: str
    outcome: str
    count: int


@dataclass(frozen=True, slots=True)
class PlanningContextPatchDeliveryMetricView:
    adapter: str
    outcome: str
    count: int


@dataclass(frozen=True, slots=True)
class PlanningRevisionMetricsView:
    plan_revisions_active: int
    plan_revision_conflicts_total: tuple[PlanningMetricReasonView, ...]
    plan_transitions_active: int
    worker_transitions_total: tuple[PlanningWorkerTransitionMetricView, ...]
    context_patch_delivery_total: tuple[PlanningContextPatchDeliveryMetricView, ...]
    revision_confirmation_blocked_total: tuple[PlanningMetricReasonView, ...]


@dataclass(frozen=True, slots=True)
class PlanningRunMetricsView:
    queued: int
    running: int
    needs_clarification: int
    blocked: int
    failed: int
    succeeded: int
    cancelled: int


@dataclass(frozen=True, slots=True)
class PlanningLatencyMetricsView:
    queue_seconds: float
    planning_seconds: float
    materialization_seconds: float


@dataclass(frozen=True, slots=True)
class PlanningOutcomeMetricsView:
    retry_rate: float
    timeout_rate: float
    unknown_outcome_rate: float
    invalid_output_rate: float


@dataclass(frozen=True, slots=True)
class PlanningUsageMetricsView:
    input_tokens: int
    output_tokens: int
    cost_microunits: int
    context_bytes: int


@dataclass(frozen=True, slots=True)
class PlanningLifecycleMetricsView:
    clarifications: int
    revisions: int
    confirmed: int
    rejected: int


@dataclass(frozen=True, slots=True)
class PlanningExecutionRequestMetricsView:
    blocked: int
    ready: int
    gate_wait_seconds: float


@dataclass(frozen=True, slots=True)
class PlanningMetricsView:
    workspace_id: UUID
    as_of: datetime
    runs: PlanningRunMetricsView
    latency: PlanningLatencyMetricsView
    outcomes: PlanningOutcomeMetricsView
    usage: PlanningUsageMetricsView
    planning: PlanningLifecycleMetricsView
    execution_requests: PlanningExecutionRequestMetricsView
    revision: PlanningRevisionMetricsView


class PlanningMetricsStore(Protocol):
    def get_metrics(self, workspace_id: UUID) -> PlanningMetricsView: ...


class PlanningMetricsService:
    def __init__(self, store: PlanningMetricsStore) -> None:
        self._store = store

    def get_metrics(self, workspace_id: UUID) -> PlanningMetricsView:
        return self._store.get_metrics(workspace_id)
