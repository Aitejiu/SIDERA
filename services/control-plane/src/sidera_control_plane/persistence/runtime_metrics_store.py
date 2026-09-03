from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, case, func, select

from sidera_control_plane.application.collaboration_metrics import (
    COLLABORATION_GOVERNANCE_COMMAND_RESULTS,
    COLLABORATION_GOVERNANCE_COMMANDS,
    COLLABORATION_GOVERNANCE_CONFLICT_OPERATIONS,
    COLLABORATION_GOVERNANCE_RESOLUTION_ACTIONS,
    COLLABORATION_GOVERNANCE_RESOLUTION_RESULTS,
    COLLABORATION_GOVERNANCE_STALE_REASONS,
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
    CollaborationGovernanceHoldModel,
    CollaborationGovernanceResolutionModel,
    CollaborationReleaseProposalModel,
    CollaborationRunModel,
    CommandModel,
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

_GOVERNANCE_COMMAND_LABELS = {
    "PauseCollaborationRelease": "pause",
    "ApproveCollaborationReleaseProposal": "approve",
    "RejectCollaborationReleaseProposal": "reject",
    "ResumeAutomaticCollaboration": "resume_automatic",
}
_GOVERNANCE_COMMAND_TYPES = tuple(_GOVERNANCE_COMMAND_LABELS)
_GOVERNANCE_CONFLICT_CODES = frozenset(
    {
        "version_conflict",
        "plan_generation_conflict",
        "collaboration_governance_hold_exists",
        "collaboration_governance_hold_not_active",
        "collaboration_governance_proposal_not_pending",
        "collaboration_governance_generation_stale",
    }
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
            for duration in (
                _duration_value(payload.get("assignment_duration_seconds")),
            )
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
                        ExecutionBindingModel.workspace_id
                        == ExecutionModel.workspace_id,
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
        missing = sum(
            worker.runtime_status == "missing" for worker in readiness_workers
        )
        invalid = sum(
            worker.runtime_status == "invalid" for worker in readiness_workers
        )
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
            active_holds = int(
                session.scalar(
                    select(func.count())
                    .select_from(CollaborationGovernanceHoldModel)
                    .where(
                        CollaborationGovernanceHoldModel.workspace_id == workspace_id,
                        CollaborationGovernanceHoldModel.status == "active",
                    )
                )
                or 0
            )
            pending_proposals = int(
                session.scalar(
                    select(func.count())
                    .select_from(CollaborationReleaseProposalModel)
                    .where(
                        CollaborationReleaseProposalModel.workspace_id == workspace_id,
                        CollaborationReleaseProposalModel.status == "pending",
                    )
                )
                or 0
            )
            oldest_pending_proposal = session.scalar(
                select(func.min(CollaborationReleaseProposalModel.created_at)).where(
                    CollaborationReleaseProposalModel.workspace_id == workspace_id,
                    CollaborationReleaseProposalModel.status == "pending",
                )
            )
            command_result = case(
                (
                    and_(
                        CommandModel.status == "applied",
                        CommandModel.command_type
                        == "ApproveCollaborationReleaseProposal",
                        CollaborationGovernanceResolutionModel.result == "stale",
                    ),
                    "stale",
                ),
                (CommandModel.status == "applied", "applied"),
                (
                    CommandModel.rejection_reason.in_(_GOVERNANCE_CONFLICT_CODES),
                    "conflict",
                ),
                else_="rejected",
            ).label("result")
            command_rows = [
                (row[0], row[1], row[2])
                for row in session.execute(
                    select(CommandModel.command_type, command_result, func.count())
                    .select_from(CommandModel)
                    .outerjoin(
                        CollaborationGovernanceResolutionModel,
                        and_(
                            CollaborationGovernanceResolutionModel.workspace_id
                            == CommandModel.workspace_id,
                            CollaborationGovernanceResolutionModel.command_id
                            == CommandModel.id,
                        ),
                    )
                    .where(
                        CommandModel.workspace_id == workspace_id,
                        CommandModel.command_type.in_(_GOVERNANCE_COMMAND_TYPES),
                    )
                    .group_by(CommandModel.command_type, command_result)
                ).all()
            ]
            resolution_rows = [
                (row[0], row[1], row[2])
                for row in session.execute(
                    select(
                        CollaborationGovernanceResolutionModel.action,
                        CollaborationGovernanceResolutionModel.result,
                        func.count(),
                    )
                    .where(
                        CollaborationGovernanceResolutionModel.workspace_id
                        == workspace_id
                    )
                    .group_by(
                        CollaborationGovernanceResolutionModel.action,
                        CollaborationGovernanceResolutionModel.result,
                    )
                ).all()
            ]
            stale_event_reason = func.coalesce(
                EventModel.payload["reason_code"].as_string(),
                EventModel.payload["code"].as_string(),
            )
            stale_reason = case(
                *(
                    (stale_event_reason == reason, reason)
                    for reason in COLLABORATION_GOVERNANCE_STALE_REASONS
                    if reason != "unknown"
                ),
                else_="unknown",
            ).label("reason_code")
            stale_rows = [
                (row[0], row[1])
                for row in session.execute(
                    select(stale_reason, func.count())
                    .select_from(CollaborationReleaseProposalModel)
                    .outerjoin(
                        EventModel,
                        and_(
                            EventModel.workspace_id == workspace_id,
                            EventModel.aggregate_type
                            == "collaboration_release_proposal",
                            EventModel.aggregate_id
                            == CollaborationReleaseProposalModel.id,
                            EventModel.event_type
                            == "CollaborationReleaseProposalStale",
                        ),
                    )
                    .where(
                        CollaborationReleaseProposalModel.workspace_id == workspace_id,
                        CollaborationReleaseProposalModel.status == "stale",
                    )
                    .group_by(stale_reason)
                ).all()
            ]

        counts: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
        values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(
            float
        )

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
                (item for item in applications if item.intent_id == _intent.id),
                None,
            )
            _increment_metric(
                counts,
                "sidera_collaboration_intents_total",
                {
                    "outcome": _stable_intent_outcome(
                        application.status if application else "pending"
                    )
                },
            )
        for _release in releases:
            _increment_metric(
                counts,
                "sidera_collaboration_releases_total",
                {"outcome": "issued"},
            )

        _add_governance_metrics(
            counts,
            values,
            active_holds=active_holds,
            pending_proposals=pending_proposals,
            oldest_pending_proposal=oldest_pending_proposal,
            now=now,
            command_rows=command_rows,
            resolution_rows=resolution_rows,
            stale_rows=stale_rows,
        )
        _add_collaboration_zero_series(counts, values)
        samples = tuple(
            CollaborationMetricSample(name=name, labels=labels, value=float(value))
            for (name, labels), value in sorted(
                (
                    *((key, float(value)) for key, value in counts.items()),
                    *values.items(),
                )
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


def _metric_key(
    name: str, labels: dict[str, str]
) -> tuple[str, tuple[tuple[str, str], ...]]:
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
    for outcome in (
        "pending",
        "applied",
        "rejected",
        "stale",
        "duplicate",
        "deferred",
    ):
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
    counts.setdefault(
        _metric_key("sidera_collaboration_governance_active_holds", {}),
        0,
    )
    counts.setdefault(
        _metric_key("sidera_collaboration_governance_pending_proposals", {}),
        0,
    )
    values.setdefault(
        _metric_key("sidera_collaboration_governance_proposal_age_seconds", {}),
        0.0,
    )
    for command in COLLABORATION_GOVERNANCE_COMMANDS:
        for result in COLLABORATION_GOVERNANCE_COMMAND_RESULTS:
            counts.setdefault(
                _metric_key(
                    "sidera_collaboration_governance_commands_total",
                    {"command": command, "result": result},
                ),
                0,
            )
    for action in COLLABORATION_GOVERNANCE_RESOLUTION_ACTIONS:
        for result in COLLABORATION_GOVERNANCE_RESOLUTION_RESULTS:
            counts.setdefault(
                _metric_key(
                    "sidera_collaboration_governance_resolution_total",
                    {"action": action, "result": result},
                ),
                0,
            )
    for reason_code in COLLABORATION_GOVERNANCE_STALE_REASONS:
        counts.setdefault(
            _metric_key(
                "sidera_collaboration_governance_stale_total",
                {"reason_code": reason_code},
            ),
            0,
        )
    for operation in COLLABORATION_GOVERNANCE_CONFLICT_OPERATIONS:
        counts.setdefault(
            _metric_key(
                "sidera_collaboration_governance_conflicts_total",
                {"operation": operation},
            ),
            0,
        )


def _add_governance_metrics(
    counts: Counter[tuple[str, tuple[tuple[str, str], ...]]],
    values: dict[tuple[str, tuple[tuple[str, str], ...]], float],
    *,
    active_holds: int,
    pending_proposals: int,
    oldest_pending_proposal: object,
    now: datetime,
    command_rows: Sequence[tuple[object, object, object]],
    resolution_rows: Sequence[tuple[object, object, object]],
    stale_rows: Sequence[tuple[object, object]],
) -> None:
    _add_metric_count(
        counts,
        "sidera_collaboration_governance_active_holds",
        {},
        active_holds,
    )
    _add_metric_count(
        counts,
        "sidera_collaboration_governance_pending_proposals",
        {},
        pending_proposals,
    )
    values[_metric_key("sidera_collaboration_governance_proposal_age_seconds", {})] = (
        _queue_age(oldest_pending_proposal, now) if pending_proposals else 0.0
    )

    for command_type, result, value in command_rows:
        command = _GOVERNANCE_COMMAND_LABELS.get(str(command_type))
        stable_result = str(result)
        if (
            command is None
            or stable_result not in COLLABORATION_GOVERNANCE_COMMAND_RESULTS
        ):
            continue
        count = _bounded_count(value)
        _add_metric_count(
            counts,
            "sidera_collaboration_governance_commands_total",
            {"command": command, "result": stable_result},
            count,
        )
        if stable_result == "conflict":
            _add_metric_count(
                counts,
                "sidera_collaboration_governance_conflicts_total",
                {"operation": command},
                count,
            )

    for action, result, value in resolution_rows:
        stable_action = str(action)
        stable_result = str(result)
        if (
            stable_action not in COLLABORATION_GOVERNANCE_RESOLUTION_ACTIONS
            or stable_result not in COLLABORATION_GOVERNANCE_RESOLUTION_RESULTS
        ):
            continue
        count = _bounded_count(value)
        _add_metric_count(
            counts,
            "sidera_collaboration_governance_resolution_total",
            {"action": stable_action, "result": stable_result},
            count,
        )
        if stable_result == "stale":
            _add_metric_count(
                counts,
                "sidera_collaboration_governance_conflicts_total",
                {"operation": stable_action},
                count,
            )

    for reason_code, value in stale_rows:
        stable_reason = str(reason_code)
        if stable_reason not in COLLABORATION_GOVERNANCE_STALE_REASONS:
            stable_reason = "unknown"
        _add_metric_count(
            counts,
            "sidera_collaboration_governance_stale_total",
            {"reason_code": stable_reason},
            _bounded_count(value),
        )


def _add_metric_count(
    counts: Counter[tuple[str, tuple[tuple[str, str], ...]]],
    name: str,
    labels: dict[str, str],
    value: int,
) -> None:
    counts[_metric_key(name, labels)] += max(0, value)


def _bounded_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _evaluation_outcome(status: str) -> str:
    return (
        status
        if status in {"pending", "evaluating", "retry_wait", "completed", "blocked"}
        else "blocked"
    )


def _stable_run_status(status: str) -> str:
    return (
        status
        if status in {"pending", "active", "blocked", "completed", "superseded"}
        else "blocked"
    )


def _stable_outcome(outcome: str) -> str:
    return outcome if outcome in {"intents", "no_change"} else "no_change"


def _stable_intent_outcome(status: str) -> str:
    return (
        status
        if status
        in {
            "pending",
            "applied",
            "rejected",
            "stale",
            "duplicate",
            "deferred",
        }
        else "rejected"
    )


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
    return (
        value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    )
