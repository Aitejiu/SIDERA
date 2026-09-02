from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, select

from sidera_control_plane.application.collaboration_metrics import (
    CollaborationMetricSample,
    CollaborationMetricsView,
)
from sidera_control_plane.application.runtime_metrics import (
    RuntimeDurationMetricsView,
    RuntimeMetricsView,
    RuntimeReadinessView,
    RuntimeReadinessWorkerView,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    CollaborationEvaluationModel,
    CollaborationRunModel,
    EventModel,
    ExecutionBindingModel,
    ExecutionModel,
    RuntimeDecisionModel,
    RuntimeIntentApplicationModel,
    RuntimeIntentModel,
    RuntimeReleaseModel,
    SchedulingBlockModel,
)
from sidera_control_plane.persistence.resource_query_store import (
    SqlAlchemyResourceQueryStore,
)


class SqlAlchemyRuntimeMetricsStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._resource_store = SqlAlchemyResourceQueryStore(database)

    def get_metrics(self, workspace_id: UUID) -> RuntimeMetricsView:
        with self._database.session() as session:
            event_rows = session.execute(
                select(EventModel.event_type, EventModel.payload).where(
                    EventModel.workspace_id == workspace_id,
                    EventModel.event_type.in_(
                        ("ExecutionRuntimeBound", "ExecutionRuntimeStartBlocked")
                    ),
                )
            ).all()
            active_block_rows = session.execute(
                select(SchedulingBlockModel.reason_code).where(
                    SchedulingBlockModel.workspace_id == workspace_id,
                    SchedulingBlockModel.status == "active",
                )
            ).all()

        workers = self._resource_store.list_workers(workspace_id)
        assignments = sum(
            1 for event_type, _ in event_rows if event_type == "ExecutionRuntimeBound"
        )
        failures = Counter(
            _stable_code(payload.get("error_code"))
            for event_type, payload in event_rows
            if event_type == "ExecutionRuntimeStartBlocked"
        )
        durations = tuple(
            duration
            for event_type, payload in event_rows
            if event_type == "ExecutionRuntimeBound"
            for duration in (_duration_value(payload.get("assignment_duration_seconds")),)
            if duration is not None
        )
        runtime_status = Counter(
            worker.runtime_configuration_status
            for worker in workers
            if worker.desired_state == "active"
        )
        blocks = Counter(str(row[0]) for row in active_block_rows)
        duration_metrics = _duration_metrics(durations)
        return RuntimeMetricsView(
            workspace_id=workspace_id,
            as_of=datetime.now(UTC),
            scheduler_assignments_total=assignments,
            scheduler_blocks_active=_sorted_counts(blocks),
            worker_runtime_configuration_status=_sorted_counts(runtime_status),
            runtime_binding_failures_total=_sorted_counts(failures),
            scheduler_assignment_duration_seconds=duration_metrics,
        )

    def get_readiness(self, workspace_id: UUID) -> RuntimeReadinessView:
        workers = tuple(
            worker
            for worker in self._resource_store.list_workers(workspace_id)
            if worker.desired_state == "active"
        )
        with self._database.session() as session:
            execution_rows = session.scalars(
                select(ExecutionModel.id)
                .outerjoin(
                    ExecutionBindingModel,
                    and_(
                        ExecutionBindingModel.execution_id == ExecutionModel.id,
                        ExecutionBindingModel.workspace_id == ExecutionModel.workspace_id,
                    ),
                )
                .where(
                    ExecutionModel.workspace_id == workspace_id,
                    ExecutionModel.status.in_(("leased", "running")),
                    ExecutionBindingModel.id.is_(None),
                )
                .order_by(ExecutionModel.id.asc())
            ).all()

        readiness_workers = tuple(
            RuntimeReadinessWorkerView(
                worker_id=worker.id,
                worker_status=worker.status,
                runtime_status=worker.runtime_configuration_status,
                reason=worker.runtime_configuration_reason,
            )
            for worker in workers
        )
        mismatches = tuple(
            worker for worker in readiness_workers if worker.runtime_status == "invalid"
        )
        missing = sum(worker.runtime_status == "missing" for worker in readiness_workers)
        invalid = sum(worker.runtime_status == "invalid" for worker in readiness_workers)
        return RuntimeReadinessView(
            workspace_id=workspace_id,
            as_of=datetime.now(UTC),
            workers=readiness_workers,
            missing_runtime_configuration=missing,
            invalid_runtime_configuration=invalid,
            executions_without_binding=tuple(execution_rows),
            module_mismatches=mismatches,
        )

    def get_collaboration_metrics(self, workspace_id: UUID) -> CollaborationMetricsView:
        now = datetime.now(UTC)
        with self._database.session() as session:
            evaluations = tuple(
                session.scalars(
                    select(CollaborationEvaluationModel).where(
                        CollaborationEvaluationModel.workspace_id == workspace_id
                    )
                ).all()
            )
            runs = tuple(
                session.scalars(
                    select(CollaborationRunModel).where(
                        CollaborationRunModel.workspace_id == workspace_id
                    )
                ).all()
            )
            decisions = tuple(
                session.scalars(
                    select(RuntimeDecisionModel).where(
                        RuntimeDecisionModel.workspace_id == workspace_id
                    )
                ).all()
            )
            intents = tuple(
                session.scalars(
                    select(RuntimeIntentModel).where(
                        RuntimeIntentModel.workspace_id == workspace_id
                    )
                ).all()
            )
            applications = tuple(
                session.scalars(
                    select(RuntimeIntentApplicationModel).where(
                        RuntimeIntentApplicationModel.workspace_id == workspace_id
                    )
                ).all()
            )
            releases = tuple(
                session.scalars(
                    select(RuntimeReleaseModel).where(
                        RuntimeReleaseModel.workspace_id == workspace_id
                    )
                ).all()
            )

        counts: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
        values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)

        for evaluation in evaluations:
            _increment_metric(
                counts,
                "sidera_collaboration_evaluations_total",
                {"outcome": _evaluation_outcome(evaluation.status)},
            )
            if evaluation.status in {"pending", "retry_wait"}:
                age = _queue_age(evaluation.next_attempt_at, now)
                key = _metric_key(
                    "sidera_collaboration_evaluation_queue_age_seconds",
                    {"outcome": evaluation.status},
                )
                values[key] = max(values[key], age)
            if evaluation.attempt > 1:
                _increment_metric(
                    counts,
                    "sidera_collaboration_retries_total",
                    {"reason": _stable_retry_reason(evaluation.last_error_code)},
                )
        for run in runs:
            _increment_metric(
                counts,
                "sidera_collaboration_runs",
                {"status": _stable_run_status(run.status)},
            )
        for decision in decisions:
            outcome = _stable_outcome(decision.outcome)
            _increment_metric(
                counts,
                "sidera_collaboration_decisions_total",
                {"outcome": outcome},
            )
            duration_key = _metric_key(
                "sidera_collaboration_evaluation_duration_seconds",
                {"outcome": outcome},
            )
            values[duration_key] += max(0.0, float(decision.runtime_duration_ms) / 1000)
        for _intent in intents:
            # Applications are the authoritative result for an intent. Missing
            # applications remain visible as pending without exposing intent IDs.
            application = next(
                (
                    item
                    for item in applications
                    if item.intent_id == _intent.id
                ),
                None,
            )
            _increment_metric(
                counts,
                "sidera_collaboration_intents_total",
                {"outcome": _stable_intent_outcome(application.status if application else "pending")},
            )
        for _release in releases:
            _increment_metric(
                counts,
                "sidera_collaboration_releases_total",
                {"outcome": "issued"},
            )

        _add_collaboration_zero_series(counts, values)
        samples = tuple(
            CollaborationMetricSample(name=name, labels=labels, value=float(value))
            for (name, labels), value in sorted(
                (*((key, float(value)) for key, value in counts.items()), *values.items())
            )
        )
        return CollaborationMetricsView(
            workspace_id=workspace_id,
            as_of=now,
            samples=samples,
        )


def _duration_metrics(values: tuple[float, ...]) -> RuntimeDurationMetricsView:
    if not values:
        return RuntimeDurationMetricsView(
            count=0,
            total_seconds=0.0,
            average_seconds=0.0,
            max_seconds=0.0,
        )
    total = sum(values)
    return RuntimeDurationMetricsView(
        count=len(values),
        total_seconds=total,
        average_seconds=total / len(values),
        max_seconds=max(values),
    )


def _duration_value(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    duration = float(value)
    if duration < 0 or not math.isfinite(duration):
        return None
    return duration


def _stable_code(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()[:100]
    return "unknown"


def _sorted_counts(values: Counter[str]) -> dict[str, int]:
    return {key: int(values[key]) for key in sorted(values)}


def _metric_key(name: str, labels: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
    return name, tuple(sorted(labels.items()))


def _increment_metric(
    counts: Counter[tuple[str, tuple[tuple[str, str], ...]]],
    name: str,
    labels: dict[str, str],
) -> None:
    counts[_metric_key(name, labels)] += 1


def _add_collaboration_zero_series(
    counts: Counter[tuple[str, tuple[tuple[str, str], ...]]],
    values: dict[tuple[str, tuple[tuple[str, str], ...]], float],
) -> None:
    for outcome in ("pending", "evaluating", "retry_wait", "completed", "blocked"):
        counts.setdefault(
            _metric_key("sidera_collaboration_evaluations_total", {"outcome": outcome}),
            0,
        )
    for outcome in ("intents", "no_change"):
        counts.setdefault(
            _metric_key("sidera_collaboration_decisions_total", {"outcome": outcome}),
            0,
        )
        values.setdefault(
            _metric_key(
                "sidera_collaboration_evaluation_duration_seconds",
                {"outcome": outcome},
            ),
            0.0,
        )
    for outcome in ("pending", "applied", "rejected", "stale", "duplicate"):
        counts.setdefault(
            _metric_key("sidera_collaboration_intents_total", {"outcome": outcome}),
            0,
        )
    counts.setdefault(
        _metric_key("sidera_collaboration_releases_total", {"outcome": "issued"}),
        0,
    )
    for status in ("pending", "active", "blocked", "completed", "superseded"):
        counts.setdefault(
            _metric_key("sidera_collaboration_runs", {"status": status}),
            0,
        )
    for outcome in ("pending", "retry_wait"):
        values.setdefault(
            _metric_key(
                "sidera_collaboration_evaluation_queue_age_seconds",
                {"outcome": outcome},
            ),
            0.0,
        )
    for reason in (
        "evaluation_lease_expired",
        "runtime_timeout",
        "runtime_unavailable",
        "runtime_output_invalid",
        "projection_too_large",
        "unsupported_runtime_capability",
        "runtime_config_mismatch",
        "worker_phase_failed",
    ):
        counts.setdefault(
            _metric_key("sidera_collaboration_retries_total", {"reason": reason}),
            0,
        )


def _evaluation_outcome(status: str) -> str:
    return status if status in {"pending", "evaluating", "retry_wait", "completed", "blocked"} else "blocked"


def _stable_run_status(status: str) -> str:
    return status if status in {"pending", "active", "blocked", "completed", "superseded"} else "blocked"


def _stable_outcome(outcome: str) -> str:
    return outcome if outcome in {"intents", "no_change"} else "no_change"


def _stable_intent_outcome(status: str) -> str:
    return status if status in {"pending", "applied", "rejected", "stale", "duplicate"} else "rejected"


def _stable_retry_reason(reason: str | None) -> str:
    allowed = {
        "evaluation_lease_expired",
        "runtime_timeout",
        "runtime_unavailable",
        "runtime_output_invalid",
        "projection_too_large",
        "unsupported_runtime_capability",
        "runtime_config_mismatch",
        "worker_phase_failed",
    }
    return reason if reason in allowed else "unknown"


def _queue_age(value: object, now: datetime) -> float:
    if not isinstance(value, datetime):
        return 0.0
    return max(0.0, (now - _as_utc(value)).total_seconds())


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
