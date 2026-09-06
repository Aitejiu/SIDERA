from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Protocol
from uuid import UUID

COLLABORATION_GOVERNANCE_COMMANDS = (
    "pause",
    "approve",
    "reject",
    "resume_automatic",
)
COLLABORATION_GOVERNANCE_COMMAND_RESULTS = (
    "applied",
    "rejected",
    "stale",
    "conflict",
)
COLLABORATION_GOVERNANCE_RESOLUTION_ACTIONS = ("approve", "reject")
COLLABORATION_GOVERNANCE_RESOLUTION_RESULTS = ("applied", "stale")
COLLABORATION_GOVERNANCE_STALE_REASONS = (
    "plan_generation_stale",
    "plan_generation_superseded",
    "collaboration_run_not_active",
    "worker_item_placement_stale",
    "intent_target_invalid",
    "projection_stale",
    "decision_conflict",
    "governance_fingerprint_stale",
    "governance_resumed",
    "unknown",
)
COLLABORATION_GOVERNANCE_CONFLICT_OPERATIONS = (
    "pause",
    "approve",
    "reject",
    "resume_automatic",
)
RESULT_INPUT_BLOCK_REASONS = (
    "result_input_unavailable",
    "result_input_integrity_failed",
    "result_input_format_unsupported",
    "result_input_budget_exceeded",
    "unknown",
)
RESULT_INPUT_SELECTION_RESULTS = ("conflict", "stale")
RESULT_INPUT_DELIVERY_OUTCOMES = ("delivered", "blocked", "failed")

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
        "sidera_collaboration_governance_active_holds",
        "sidera_collaboration_governance_pending_proposals",
        "sidera_collaboration_governance_commands_total",
        "sidera_collaboration_governance_resolution_total",
        "sidera_collaboration_governance_proposal_age_seconds",
        "sidera_collaboration_governance_stale_total",
        "sidera_collaboration_governance_conflicts_total",
        "sidera_collaboration_result_input_blocked_total",
        "sidera_collaboration_result_input_selection_total",
        "sidera_collaboration_result_input_delivery_total",
        "sidera_collaboration_result_input_delivery_duration_seconds",
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
    "sidera_collaboration_governance_active_holds": frozenset(),
    "sidera_collaboration_governance_pending_proposals": frozenset(),
    "sidera_collaboration_governance_commands_total": frozenset({"command", "result"}),
    "sidera_collaboration_governance_resolution_total": frozenset({"action", "result"}),
    "sidera_collaboration_governance_proposal_age_seconds": frozenset(),
    "sidera_collaboration_governance_stale_total": frozenset({"reason_code"}),
    "sidera_collaboration_governance_conflicts_total": frozenset({"operation"}),
    "sidera_collaboration_result_input_blocked_total": frozenset({"reason"}),
    "sidera_collaboration_result_input_selection_total": frozenset({"result"}),
    "sidera_collaboration_result_input_delivery_total": frozenset({"outcome"}),
    "sidera_collaboration_result_input_delivery_duration_seconds": frozenset(
        {"outcome"}
    ),
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
            "deferred",
            "delivered",
            "failed",
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
            *RESULT_INPUT_BLOCK_REASONS,
            "unknown",
        }
    ),
    "command": frozenset(COLLABORATION_GOVERNANCE_COMMANDS),
    "result": frozenset(
        {
            *COLLABORATION_GOVERNANCE_COMMAND_RESULTS,
            *COLLABORATION_GOVERNANCE_RESOLUTION_RESULTS,
        }
    ),
    "action": frozenset(COLLABORATION_GOVERNANCE_RESOLUTION_ACTIONS),
    "reason_code": frozenset(COLLABORATION_GOVERNANCE_STALE_REASONS),
    "operation": frozenset(COLLABORATION_GOVERNANCE_CONFLICT_OPERATIONS),
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
        if self.value < 0 or not isfinite(self.value):
            raise ValueError(
                "collaboration metric values must be finite and non-negative"
            )


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
