from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Row

from sidera_control_plane.application.resource_queries import (
    ExecutionView,
    LeaseView,
    WorkerView,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    ExecutionModel,
    WorkerModel,
)


class SqlAlchemyResourceQueryStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def list_workers(self, workspace_id: UUID) -> tuple[WorkerView, ...]:
        with self._database.session() as session:
            rows = session.execute(
                _worker_columns()
                .where(WorkerModel.workspace_id == workspace_id)
                .order_by(WorkerModel.display_name.asc(), WorkerModel.id.asc())
            ).all()
        return tuple(_worker_view(row) for row in rows)

    def get_worker(self, workspace_id: UUID, worker_id: UUID) -> WorkerView | None:
        with self._database.session() as session:
            row = session.execute(
                _worker_columns().where(
                    WorkerModel.workspace_id == workspace_id,
                    WorkerModel.id == worker_id,
                )
            ).one_or_none()
        return _worker_view(row) if row is not None else None

    def list_executions(
        self, workspace_id: UUID, task_id: UUID | None
    ) -> tuple[ExecutionView, ...]:
        statement = _execution_columns().where(
            ExecutionModel.workspace_id == workspace_id
        )
        if task_id is not None:
            statement = statement.where(ExecutionModel.task_id == task_id)
        with self._database.session() as session:
            rows = session.execute(
                statement.order_by(ExecutionModel.created_at.asc(), ExecutionModel.id.asc())
            ).all()
        return tuple(_execution_view(row) for row in rows)

    def get_execution(
        self, workspace_id: UUID, execution_id: UUID
    ) -> ExecutionView | None:
        with self._database.session() as session:
            row = session.execute(
                _execution_columns().where(
                    ExecutionModel.workspace_id == workspace_id,
                    ExecutionModel.id == execution_id,
                )
            ).one_or_none()
        return _execution_view(row) if row is not None else None

    def list_leases(
        self, workspace_id: UUID, execution_id: UUID
    ) -> tuple[LeaseView, ...]:
        with self._database.session() as session:
            rows = session.execute(
                _lease_columns()
                .where(
                    AssignmentLeaseModel.workspace_id == workspace_id,
                    AssignmentLeaseModel.execution_id == execution_id,
                )
                .order_by(AssignmentLeaseModel.fencing_token.asc())
            ).all()
        return tuple(_lease_view(row) for row in rows)


def _worker_columns():
    return select(
        WorkerModel.id,
        WorkerModel.workspace_id,
        WorkerModel.adapter_type,
        WorkerModel.display_name,
        WorkerModel.status,
        WorkerModel.desired_state,
        WorkerModel.capabilities,
        WorkerModel.capacity,
        WorkerModel.current_load,
        WorkerModel.cost_per_hour_microunits,
        WorkerModel.startup_latency_ms,
        WorkerModel.last_heartbeat_at,
        WorkerModel.version,
        WorkerModel.created_at,
        WorkerModel.updated_at,
    )


def _worker_view(row: Row[Any]) -> WorkerView:
    return WorkerView(
        id=row.id,
        workspace_id=row.workspace_id,
        adapter_type=row.adapter_type,
        display_name=row.display_name,
        status=row.status,
        desired_state=row.desired_state,
        capabilities=tuple(sorted(row.capabilities)),
        capacity=row.capacity,
        current_load=row.current_load,
        cost_per_hour_microunits=row.cost_per_hour_microunits,
        startup_latency_ms=row.startup_latency_ms,
        last_heartbeat_at=_utc(row.last_heartbeat_at),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _execution_columns():
    return select(
        ExecutionModel.id,
        ExecutionModel.workspace_id,
        ExecutionModel.task_id,
        ExecutionModel.lineage_id,
        ExecutionModel.attempt,
        ExecutionModel.max_attempts,
        ExecutionModel.purpose,
        ExecutionModel.status,
        ExecutionModel.capability_requirements,
        ExecutionModel.pinned_worker_id,
        ExecutionModel.assigned_worker_id,
        ExecutionModel.active_lease_id,
        ExecutionModel.last_fencing_token,
        ExecutionModel.not_before,
        ExecutionModel.failure_code,
        ExecutionModel.started_at,
        ExecutionModel.finished_at,
        ExecutionModel.version,
        ExecutionModel.created_at,
        ExecutionModel.updated_at,
    )


def _execution_view(row: Row[Any]) -> ExecutionView:
    return ExecutionView(
        id=row.id,
        workspace_id=row.workspace_id,
        task_id=row.task_id,
        lineage_id=row.lineage_id,
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        purpose=row.purpose,
        status=row.status,
        capability_requirements=tuple(sorted(row.capability_requirements)),
        pinned_worker_id=row.pinned_worker_id,
        assigned_worker_id=row.assigned_worker_id,
        active_lease_id=row.active_lease_id,
        last_fencing_token=row.last_fencing_token,
        not_before=_utc(row.not_before),
        failure_code=row.failure_code,
        started_at=_utc_optional(row.started_at),
        finished_at=_utc_optional(row.finished_at),
        version=row.version,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _lease_columns():
    return select(
        AssignmentLeaseModel.id,
        AssignmentLeaseModel.execution_id,
        AssignmentLeaseModel.worker_id,
        AssignmentLeaseModel.status,
        AssignmentLeaseModel.issued_at,
        AssignmentLeaseModel.expires_at,
        AssignmentLeaseModel.renewed_at,
        AssignmentLeaseModel.ended_at,
        AssignmentLeaseModel.fencing_token,
        AssignmentLeaseModel.assignment_reason,
        AssignmentLeaseModel.version,
    )


def _lease_view(row: Row[Any]) -> LeaseView:
    return LeaseView(
        id=row.id,
        execution_id=row.execution_id,
        worker_id=row.worker_id,
        status=row.status,
        issued_at=_utc(row.issued_at),
        expires_at=_utc(row.expires_at),
        renewed_at=_utc_optional(row.renewed_at),
        ended_at=_utc_optional(row.ended_at),
        fencing_token=row.fencing_token,
        assignment_reason=dict(row.assignment_reason),
        version=row.version,
    )


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _utc_optional(value: datetime | None) -> datetime | None:
    return _utc(value) if value is not None else None
