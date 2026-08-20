from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.worker import Worker, WorkerStatus
from sidera_control_plane.persistence.models import WorkerModel


class WorkerRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self, worker_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> Worker | None:
        statement = select(WorkerModel).where(
            WorkerModel.id == worker_id,
            WorkerModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain_worker(row) if row is not None else None

    def add(self, worker: Worker) -> None:
        self._session.add(
            WorkerModel(
                id=worker.id,
                workspace_id=worker.workspace_id,
                adapter_type=worker.adapter_type,
                display_name=worker.display_name,
                status=worker.status.value,
                capabilities=sorted(worker.capabilities),
                capacity=worker.capacity,
                current_load=worker.current_load,
                cost_per_hour_microunits=worker.cost_per_hour_microunits,
                startup_latency_ms=worker.startup_latency_ms,
                last_heartbeat_at=worker.last_heartbeat_at,
                version=worker.version,
            )
        )

    def save(self, worker: Worker, previous_version: int) -> None:
        statement = (
            update(WorkerModel)
            .where(
                WorkerModel.id == worker.id,
                WorkerModel.workspace_id == worker.workspace_id,
                WorkerModel.version == previous_version,
            )
            .values(
                adapter_type=worker.adapter_type,
                display_name=worker.display_name,
                status=worker.status.value,
                capabilities=sorted(worker.capabilities),
                capacity=worker.capacity,
                current_load=worker.current_load,
                cost_per_hour_microunits=worker.cost_per_hour_microunits,
                startup_latency_ms=worker.startup_latency_ms,
                last_heartbeat_at=worker.last_heartbeat_at,
                version=worker.version,
                updated_at=datetime.now(UTC),
            )
        )
        result = self._session.execute(statement)
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            actual = self._current_version(worker.id, worker.workspace_id)
            raise VersionConflict(previous_version, actual)

    def lock_candidates(
        self,
        *,
        workspace_id: UUID,
        required_capabilities: frozenset[str],
        pinned_worker_id: UUID | None,
        heartbeat_cutoff: datetime,
    ) -> tuple[Worker, ...]:
        statement = (
            select(WorkerModel)
            .where(
                WorkerModel.workspace_id == workspace_id,
                WorkerModel.status.in_(
                    (
                        WorkerStatus.IDLE.value,
                        WorkerStatus.RESERVED.value,
                        WorkerStatus.BUSY.value,
                    )
                ),
                WorkerModel.last_heartbeat_at > heartbeat_cutoff,
                WorkerModel.current_load < WorkerModel.capacity,
                WorkerModel.capabilities.contains(sorted(required_capabilities)),
            )
            .order_by(WorkerModel.id.asc())
            .with_for_update(skip_locked=True)
        )
        if pinned_worker_id is not None:
            statement = statement.where(WorkerModel.id == pinned_worker_id)
        rows = self._session.scalars(statement).all()
        return tuple(_to_domain_worker(row) for row in rows)

    def claim_stale(
        self, *, heartbeat_cutoff: datetime, limit: int
    ) -> tuple[Worker, ...]:
        statement = (
            select(WorkerModel)
            .where(
                WorkerModel.status != WorkerStatus.OFFLINE.value,
                WorkerModel.last_heartbeat_at <= heartbeat_cutoff,
            )
            .order_by(WorkerModel.last_heartbeat_at.asc(), WorkerModel.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = self._session.scalars(statement).all()
        return tuple(_to_domain_worker(row) for row in rows)

    def _current_version(self, worker_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(WorkerModel.version).where(
                WorkerModel.id == worker_id,
                WorkerModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


def _to_domain_worker(row: WorkerModel) -> Worker:
    return Worker(
        id=row.id,
        workspace_id=row.workspace_id,
        adapter_type=row.adapter_type,
        display_name=row.display_name,
        status=WorkerStatus(row.status),
        capabilities=frozenset(row.capabilities),
        capacity=row.capacity,
        current_load=row.current_load,
        cost_per_hour_microunits=row.cost_per_hour_microunits,
        startup_latency_ms=row.startup_latency_ms,
        last_heartbeat_at=row.last_heartbeat_at,
        version=row.version,
    )
