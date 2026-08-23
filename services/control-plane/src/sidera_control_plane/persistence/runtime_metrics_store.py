from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, select

from sidera_control_plane.application.runtime_metrics import (
    RuntimeDurationMetricsView,
    RuntimeMetricsView,
    RuntimeReadinessView,
    RuntimeReadinessWorkerView,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    EventModel,
    ExecutionBindingModel,
    ExecutionModel,
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
