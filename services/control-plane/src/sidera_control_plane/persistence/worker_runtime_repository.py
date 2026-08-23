from collections.abc import Collection
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration
from sidera_control_plane.persistence.models import WorkerRuntimeConfigurationModel


class SqlAlchemyWorkerRuntimeConfigurationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_for_worker(
        self,
        worker_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkerRuntimeConfiguration | None:
        statement = select(WorkerRuntimeConfigurationModel).where(
            WorkerRuntimeConfigurationModel.worker_id == worker_id,
            WorkerRuntimeConfigurationModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain(row) if row is not None else None

    def get_for_workers(
        self,
        worker_ids: Collection[UUID],
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[WorkerRuntimeConfiguration, ...]:
        ordered_ids = tuple(dict.fromkeys(worker_ids))
        if not ordered_ids:
            return ()
        statement = (
            select(WorkerRuntimeConfigurationModel)
            .where(
                WorkerRuntimeConfigurationModel.worker_id.in_(ordered_ids),
                WorkerRuntimeConfigurationModel.workspace_id == workspace_id,
            )
            .order_by(WorkerRuntimeConfigurationModel.worker_id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        rows = self._session.scalars(statement).all()
        by_worker_id = {row.worker_id: _to_domain(row) for row in rows}
        return tuple(
            by_worker_id[worker_id]
            for worker_id in ordered_ids
            if worker_id in by_worker_id
        )

    def add(self, configuration: WorkerRuntimeConfiguration) -> None:
        self._session.add(
            WorkerRuntimeConfigurationModel(
                worker_id=configuration.worker_id,
                workspace_id=configuration.workspace_id,
                adapter_module_instance_id=configuration.adapter_module_instance_id,
                adapter_module_config_version=configuration.adapter_module_config_version,
                workspace_module_instance_id=configuration.workspace_module_instance_id,
                workspace_module_config_version=configuration.workspace_module_config_version,
                version=configuration.version,
            )
        )

    def save(
        self,
        configuration: WorkerRuntimeConfiguration,
        previous_version: int,
    ) -> None:
        result = self._session.execute(
            update(WorkerRuntimeConfigurationModel)
            .where(
                WorkerRuntimeConfigurationModel.worker_id == configuration.worker_id,
                WorkerRuntimeConfigurationModel.workspace_id
                == configuration.workspace_id,
                WorkerRuntimeConfigurationModel.version == previous_version,
            )
            .values(
                adapter_module_instance_id=configuration.adapter_module_instance_id,
                adapter_module_config_version=configuration.adapter_module_config_version,
                workspace_module_instance_id=configuration.workspace_module_instance_id,
                workspace_module_config_version=configuration.workspace_module_config_version,
                version=configuration.version,
                updated_at=datetime.now(UTC),
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                previous_version,
                self._current_version(
                    configuration.worker_id, configuration.workspace_id
                ),
            )

    def _current_version(self, worker_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(WorkerRuntimeConfigurationModel.version).where(
                WorkerRuntimeConfigurationModel.worker_id == worker_id,
                WorkerRuntimeConfigurationModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


def _to_domain(row: WorkerRuntimeConfigurationModel) -> WorkerRuntimeConfiguration:
    return WorkerRuntimeConfiguration(
        worker_id=row.worker_id,
        workspace_id=row.workspace_id,
        adapter_module_instance_id=row.adapter_module_instance_id,
        adapter_module_config_version=row.adapter_module_config_version,
        workspace_module_instance_id=row.workspace_module_instance_id,
        workspace_module_config_version=row.workspace_module_config_version,
        version=row.version,
    )
