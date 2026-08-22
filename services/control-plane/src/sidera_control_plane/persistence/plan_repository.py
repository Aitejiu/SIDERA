from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from sidera_control_plane.domain.plan_normalization import PlanNormalizer
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.persistence.models import PlanSnapshotModel


class PlanRepository:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._normalizer = PlanNormalizer()

    def add(self, snapshot: TaskPlanSnapshot) -> None:
        self._session.add(
            PlanSnapshotModel(
                id=snapshot.id,
                schema_version=snapshot.schema_version,
                workspace_id=snapshot.workspace_id,
                root_task_id=snapshot.root_task_id,
                generation=snapshot.generation,
                parent_snapshot_id=snapshot.parent_snapshot_id,
                source_type=snapshot.source_type,
                source_ref=snapshot.source_ref,
                title=snapshot.title,
                goal_summary=snapshot.goal_summary,
                payload=snapshot.to_payload(),
                published_by=snapshot.published_by,
                published_at=snapshot.published_at,
            )
        )

    def get(self, snapshot_id: UUID, workspace_id: UUID) -> TaskPlanSnapshot | None:
        row = self._session.scalar(
            select(PlanSnapshotModel).where(
                PlanSnapshotModel.id == snapshot_id,
                PlanSnapshotModel.workspace_id == workspace_id,
            )
        )
        return _to_domain(row) if row is not None else None

    def get_current_for_root(
        self, root_task_id: UUID, workspace_id: UUID
    ) -> TaskPlanSnapshot | None:
        row = self._session.scalar(
            select(PlanSnapshotModel)
            .where(
                PlanSnapshotModel.root_task_id == root_task_id,
                PlanSnapshotModel.workspace_id == workspace_id,
            )
            .order_by(PlanSnapshotModel.generation.desc())
            .limit(1)
        )
        return _to_domain(row) if row is not None else None

    def get_current_for_task(
        self, task_id: UUID, workspace_id: UUID
    ) -> TaskPlanSnapshot | None:
        rows = self._session.scalars(
            select(PlanSnapshotModel)
            .where(PlanSnapshotModel.workspace_id == workspace_id)
            .order_by(
                PlanSnapshotModel.root_task_id,
                PlanSnapshotModel.generation.desc(),
            )
        ).all()
        seen_roots: set[UUID] = set()
        for row in rows:
            if row.root_task_id in seen_roots:
                continue
            seen_roots.add(row.root_task_id)
            snapshot = _to_domain(row)
            canonical = self._normalizer.normalize(snapshot)
            if any(item.task_id == task_id for item in canonical.subtasks):
                return snapshot
        return None

    def get_for_generation(
        self, root_task_id: UUID, workspace_id: UUID, generation: int
    ) -> TaskPlanSnapshot | None:
        row = self._session.scalar(
            select(PlanSnapshotModel).where(
                PlanSnapshotModel.root_task_id == root_task_id,
                PlanSnapshotModel.workspace_id == workspace_id,
                PlanSnapshotModel.generation == generation,
            )
        )
        return _to_domain(row) if row is not None else None

    def list_for_root(
        self, root_task_id: UUID, workspace_id: UUID
    ) -> tuple[TaskPlanSnapshot, ...]:
        rows = self._session.scalars(
            select(PlanSnapshotModel)
            .where(
                PlanSnapshotModel.root_task_id == root_task_id,
                PlanSnapshotModel.workspace_id == workspace_id,
            )
            .order_by(PlanSnapshotModel.generation.desc())
        ).all()
        return tuple(_to_domain(row) for row in rows)


def _to_domain(row: PlanSnapshotModel) -> TaskPlanSnapshot:
    snapshot = TaskPlanSnapshot.from_payload(dict(row.payload))
    if (
        snapshot.id != row.id
        or snapshot.workspace_id != row.workspace_id
        or snapshot.root_task_id != row.root_task_id
        or snapshot.generation != row.generation
    ):
        raise ValueError("task plan snapshot payload does not match its identity columns")
    return snapshot
