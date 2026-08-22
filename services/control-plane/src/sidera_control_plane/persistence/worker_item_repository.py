from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.application.ports import (
    ProjectionChangePublisherPort,
    WorkerItemPlacement,
    WorkerItemRepositoryPort,
)
from sidera_control_plane.application.projection_invalidation import (
    projection_targets_for_worker_item_change,
)
from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.worker_item import WorkerItem, WorkerItemStatus
from sidera_control_plane.persistence.models import (
    PlanSnapshotModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
)


class WorkerItemRepository(WorkerItemRepositoryPort):
    def __init__(
        self,
        session: Session,
        projection_changes: ProjectionChangePublisherPort | None = None,
    ) -> None:
        self._session = session
        self._projection_changes = projection_changes

    def get(
        self, item_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> WorkerItem | None:
        statement = select(WorkerItemModel).where(
            WorkerItemModel.id == item_id,
            WorkerItemModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain_worker_item(row) if row is not None else None

    def add(self, item: WorkerItem) -> None:
        self._session.add(
            WorkerItemModel(
                id=item.id,
                workspace_id=item.workspace_id,
                root_task_id=item.root_task_id,
                stable_key=item.stable_key,
                title=item.title,
                responsibility=item.responsibility,
                status=item.status.value,
                context_version=item.context_version,
                acknowledged_context_version=item.acknowledged_context_version,
                derived_from_worker_item_ids=[
                    str(source_id) for source_id in item.derived_from_worker_item_ids
                ],
                version=item.version,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
        )
        self._publish_change(item, "worker_item.created", item.version)

    def save(self, item: WorkerItem, previous_version: int) -> None:
        statement = (
            update(WorkerItemModel)
            .where(
                WorkerItemModel.id == item.id,
                WorkerItemModel.workspace_id == item.workspace_id,
                WorkerItemModel.version == previous_version,
            )
            .values(
                root_task_id=item.root_task_id,
                stable_key=item.stable_key,
                title=item.title,
                responsibility=item.responsibility,
                status=item.status.value,
                context_version=item.context_version,
                acknowledged_context_version=item.acknowledged_context_version,
                derived_from_worker_item_ids=[
                    str(source_id) for source_id in item.derived_from_worker_item_ids
                ],
                version=item.version,
                updated_at=item.updated_at,
            )
        )
        result = self._session.execute(statement)
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            actual = self._current_version(item.id, item.workspace_id)
            raise VersionConflict(previous_version, actual)
        self._publish_change(item, "worker_item.updated", item.version)

    def get_by_stable_key(
        self, root_task_id: UUID, stable_key: str
    ) -> WorkerItem | None:
        row = self._session.scalar(
            select(WorkerItemModel).where(
                WorkerItemModel.root_task_id == root_task_id,
                WorkerItemModel.stable_key == stable_key,
            )
        )
        return _to_domain_worker_item(row) if row is not None else None

    def add_placement(self, placement: WorkerItemPlacement) -> None:
        statement = select(WorkerItemPlacementModel).where(
            WorkerItemPlacementModel.plan_snapshot_id == placement.plan_snapshot_id,
            WorkerItemPlacementModel.worker_item_id == placement.worker_item_id,
        )
        existing = self._session.scalar(statement)
        if existing is not None:
            if _to_placement(existing) != placement:
                raise ValidationError("immutable placement cannot be changed")
            return

        self._session.add(
            WorkerItemPlacementModel(
                plan_snapshot_id=placement.plan_snapshot_id,
                plan_generation=placement.plan_generation,
                workspace_id=placement.workspace_id,
                root_task_id=placement.root_task_id,
                worker_item_id=placement.worker_item_id,
                worker_item_key=placement.worker_item_key,
                subtask_key=placement.subtask_key,
                subtask_task_id=placement.subtask_task_id,
                role=placement.role,
                capability_requirements=list(placement.capability_requirements),
                worker_policy_ref=placement.worker_policy_ref,
                configuration_ref=placement.configuration_ref,
                created_at=placement.created_at,
            )
        )
        self._publish_change(
            placement,
            "worker_item.placement_materialized",
            placement.plan_generation,
        )

    def _publish_change(
        self,
        item: WorkerItem | WorkerItemPlacement,
        change_type: str,
        resource_version: int,
    ) -> None:
        if self._projection_changes is None:
            return
        for target in projection_targets_for_worker_item_change(
            worker_item_id=item.id if isinstance(item, WorkerItem) else item.worker_item_id,
            root_task_id=item.root_task_id,
            change_type=change_type,
        ):
            self._projection_changes.changed(
                workspace_id=item.workspace_id,
                projection_type=target.projection_type,
                resource_id=target.resource_id,
                change_type=change_type,
                resource_version=resource_version,
                correlation_id=item.id if isinstance(item, WorkerItem) else item.worker_item_id,
            )

    def get_placement(
        self, plan_snapshot_id: UUID, worker_item_id: UUID
    ) -> WorkerItemPlacement | None:
        row = self._session.scalar(
            select(WorkerItemPlacementModel).where(
                WorkerItemPlacementModel.plan_snapshot_id == plan_snapshot_id,
                WorkerItemPlacementModel.worker_item_id == worker_item_id,
            )
        )
        return _to_placement(row) if row is not None else None

    def get_current_placement(
        self, worker_item_id: UUID, workspace_id: UUID
    ) -> WorkerItemPlacement | None:
        row = self._session.scalar(
            select(WorkerItemPlacementModel)
            .join(
                PlanSnapshotModel,
                PlanSnapshotModel.id == WorkerItemPlacementModel.plan_snapshot_id,
            )
            .where(
                WorkerItemPlacementModel.worker_item_id == worker_item_id,
                WorkerItemPlacementModel.workspace_id == workspace_id,
                PlanSnapshotModel.workspace_id == workspace_id,
            )
            .order_by(PlanSnapshotModel.generation.desc())
            .limit(1)
        )
        return _to_placement(row) if row is not None else None

    def list_placements(
        self, worker_item_id: UUID, workspace_id: UUID
    ) -> tuple[WorkerItemPlacement, ...]:
        rows = self._session.scalars(
            select(WorkerItemPlacementModel)
            .join(
                PlanSnapshotModel,
                PlanSnapshotModel.id == WorkerItemPlacementModel.plan_snapshot_id,
            )
            .where(
                WorkerItemPlacementModel.worker_item_id == worker_item_id,
                WorkerItemPlacementModel.workspace_id == workspace_id,
                PlanSnapshotModel.workspace_id == workspace_id,
            )
            .order_by(PlanSnapshotModel.generation.desc())
        ).all()
        return tuple(_to_placement(row) for row in rows)

    def list_for_subtask(
        self, plan_snapshot_id: UUID, subtask_key: str
    ) -> tuple[WorkerItemPlacement, ...]:
        rows = self._session.scalars(
            select(WorkerItemPlacementModel)
            .join(
                PlanSnapshotModel,
                PlanSnapshotModel.id == WorkerItemPlacementModel.plan_snapshot_id,
            )
            .where(
                WorkerItemPlacementModel.plan_snapshot_id == plan_snapshot_id,
                WorkerItemPlacementModel.subtask_key == subtask_key,
                WorkerItemPlacementModel.workspace_id == PlanSnapshotModel.workspace_id,
            )
            .order_by(WorkerItemPlacementModel.worker_item_key)
        ).all()
        return tuple(_to_placement(row) for row in rows)

    def _current_version(self, item_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(WorkerItemModel.version).where(
                WorkerItemModel.id == item_id,
                WorkerItemModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


def _to_domain_worker_item(row: WorkerItemModel) -> WorkerItem:
    return WorkerItem(
        id=row.id,
        workspace_id=row.workspace_id,
        root_task_id=row.root_task_id,
        stable_key=row.stable_key,
        title=row.title,
        responsibility=row.responsibility,
        status=WorkerItemStatus(row.status),
        context_version=row.context_version,
        acknowledged_context_version=row.acknowledged_context_version,
        derived_from_worker_item_ids=tuple(
            UUID(source_id) for source_id in row.derived_from_worker_item_ids
        ),
        version=row.version,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _to_placement(row: WorkerItemPlacementModel) -> WorkerItemPlacement:
    return WorkerItemPlacement(
        plan_snapshot_id=row.plan_snapshot_id,
        plan_generation=row.plan_generation,
        workspace_id=row.workspace_id,
        root_task_id=row.root_task_id,
        worker_item_id=row.worker_item_id,
        worker_item_key=row.worker_item_key,
        subtask_key=row.subtask_key,
        subtask_task_id=row.subtask_task_id,
        role=row.role,
        capability_requirements=tuple(row.capability_requirements),
        worker_policy_ref=row.worker_policy_ref,
        configuration_ref=row.configuration_ref,
        created_at=_as_utc(row.created_at),
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
