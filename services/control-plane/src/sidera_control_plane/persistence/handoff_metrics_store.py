from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text

from sidera_control_plane.application.handoff_metrics import (
    HandoffMetricSample,
    HandoffMetricsView,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    GatewayOperationModel,
    HandoffProposalModel,
    HandoffRecoveryDecisionModel,
    HandoffTransitionModel,
    WorkerModel,
)


class SqlAlchemyHandoffMetricsStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get_metrics(self, workspace_id: UUID) -> HandoffMetricsView:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            proposals = tuple(
                session.scalars(
                    select(HandoffProposalModel).where(
                        HandoffProposalModel.workspace_id == workspace_id
                    )
                ).all()
            )
            transitions = tuple(
                session.scalars(
                    select(HandoffTransitionModel).where(
                        HandoffTransitionModel.workspace_id == workspace_id
                    )
                ).all()
            )
            operations = tuple(
                session.scalars(
                    select(GatewayOperationModel).where(
                        GatewayOperationModel.workspace_id == workspace_id
                    )
                ).all()
            )
            decisions = tuple(
                session.scalars(
                    select(HandoffRecoveryDecisionModel).where(
                        HandoffRecoveryDecisionModel.workspace_id == workspace_id
                    )
                ).all()
            )
            execution_ids = {operation.execution_id for operation in operations}
            executions = {
                row.id: row
                for row in session.scalars(
                    select(ExecutionModel).where(
                        ExecutionModel.workspace_id == workspace_id,
                        ExecutionModel.id.in_(execution_ids or {UUID(int=0)}),
                    )
                ).all()
            }
            worker_ids = {
                worker_id
                for execution in executions.values()
                for worker_id in (
                    execution.assigned_worker_id,
                    execution.pinned_worker_id,
                )
                if worker_id is not None
            }
            workers = {
                row.id: row.adapter_type
                for row in session.scalars(
                    select(WorkerModel).where(
                        WorkerModel.workspace_id == workspace_id,
                        WorkerModel.id.in_(worker_ids or {UUID(int=0)}),
                    )
                ).all()
            }
            samples = _samples(
                proposals,
                transitions,
                operations,
                decisions,
                executions,
                workers,
            )
            timestamps = [
                value.created_at
                for value in (*proposals, *transitions, *operations, *decisions)
            ]
            as_of = max(
                (_as_utc(value) for value in timestamps),
                default=datetime.now(UTC),
            )
            return HandoffMetricsView(
                workspace_id=workspace_id,
                as_of=as_of,
                samples=samples,
            )


def _samples(
    proposals: tuple[HandoffProposalModel, ...],
    transitions: tuple[HandoffTransitionModel, ...],
    operations: tuple[GatewayOperationModel, ...],
    decisions: tuple[HandoffRecoveryDecisionModel, ...],
    executions: dict[UUID, ExecutionModel],
    workers: dict[UUID, str],
) -> tuple[HandoffMetricSample, ...]:
    counts: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
    duration_totals: defaultdict[tuple[tuple[str, str], ...], float] = defaultdict(float)

    for proposal in proposals:
        _increment(counts, "sidera_handoff_proposals_total", {"outcome": proposal.status})
    for transition in transitions:
        _increment(
            counts,
            "sidera_handoff_transitions",
            {"status": transition.status, "phase": transition.phase},
        )
        if transition.completed_at is not None:
            start = transition.started_at or transition.confirmed_at
            duration_totals[
                _labels(
                    {
                        "outcome": _transition_outcome(transition.status),
                        "transfer_mode": "reuse_exact",
                    }
                )
            ] += max(0.0, (_as_utc(transition.completed_at) - _as_utc(start)).total_seconds())
    for operation in operations:
        execution = executions.get(operation.execution_id)
        worker_id = (
            execution.assigned_worker_id if execution is not None else None
        ) or (execution.pinned_worker_id if execution is not None else None)
        _increment(
            counts,
            "sidera_handoff_gateway_operations_total",
            {
                "phase": _operation_phase(operation.operation_type),
                "outcome": operation.status,
                "adapter_type": (
                    workers.get(worker_id, "unknown")
                    if worker_id is not None
                    else "unknown"
                ),
            },
        )
    for decision in decisions:
        _increment(
            counts,
            "sidera_handoff_recoveries_total",
            {"strategy": decision.strategy, "outcome": "applied"},
        )

    _add_zero_series(counts, "sidera_handoff_proposals_total", {"outcome": "none"})
    _add_zero_series(
        counts,
        "sidera_handoff_transitions",
        {"status": "none", "phase": "none"},
    )
    _add_zero_series(
        counts,
        "sidera_handoff_gateway_operations_total",
        {"phase": "none", "outcome": "none", "adapter_type": "none"},
    )
    _add_zero_series(
        counts,
        "sidera_handoff_recoveries_total",
        {"strategy": "none", "outcome": "none"},
    )
    if not duration_totals:
        duration_totals[_labels({"outcome": "none", "transfer_mode": "none"})] = 0.0

    samples = [
        HandoffMetricSample(name=name, labels=labels, value=float(value))
        for (name, labels), value in counts.items()
    ]
    samples.extend(
        HandoffMetricSample(
            name="sidera_handoff_duration_seconds",
            labels=labels,
            value=value,
        )
        for labels, value in duration_totals.items()
    )
    return tuple(sorted(samples, key=lambda item: (item.name, item.labels)))


def _increment(
    counts: Counter[tuple[str, tuple[tuple[str, str], ...]]],
    name: str,
    labels: dict[str, str],
) -> None:
    counts[(name, _labels(labels))] += 1


def _add_zero_series(
    counts: Counter[tuple[str, tuple[tuple[str, str], ...]]],
    name: str,
    labels: dict[str, str],
) -> None:
    counts.setdefault((name, _labels(labels)), 0)


def _labels(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, str(value)) for key, value in labels.items()))


def _transition_outcome(status: str) -> str:
    return "succeeded" if status == "converged" else status


def _operation_phase(operation_type: str) -> str:
    return {
        "handoff_checkpoint": "checkpoint_source",
        "handoff_stop": "stop_source",
        "handoff_freeze_workspace": "freeze_source_workspace",
        "handoff_start_target": "start_target_session",
        "handoff_transfer_workspace": "transfer_workspace",
        "handoff_transfer_input": "transfer_safe_inputs",
    }.get(operation_type, "handoff")


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
