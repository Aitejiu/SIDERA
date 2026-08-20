from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Row

from sidera_control_plane.application.resource_queries import WorkerView
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import WorkerModel


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


def _worker_columns():
    return select(
        WorkerModel.id,
        WorkerModel.workspace_id,
        WorkerModel.adapter_type,
        WorkerModel.display_name,
        WorkerModel.status,
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
        capabilities=tuple(sorted(row.capabilities)),
        capacity=row.capacity,
        current_load=row.current_load,
        cost_per_hour_microunits=row.cost_per_hour_microunits,
        startup_latency_ms=row.startup_latency_ms,
        last_heartbeat_at=row.last_heartbeat_at,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
