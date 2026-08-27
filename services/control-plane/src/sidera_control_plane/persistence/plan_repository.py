from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.domain.errors import (
    PlanGenerationConflict,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.plan_normalization import PlanNormalizer
from sidera_control_plane.domain.plan_revisions import (
    RootTaskPlanHead,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.persistence.models import (
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
)


class PlanRepository:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._normalizer = PlanNormalizer()

    def add(self, snapshot: TaskPlanSnapshot) -> None:
        self._session.add(_snapshot_model(snapshot))

    def add_snapshot_and_advance_head(
        self,
        snapshot: TaskPlanSnapshot,
        *,
        expected_head: RootTaskPlanHead | None,
    ) -> RootTaskPlanHead:
        if expected_head is None:
            with self._session.no_autoflush:
                existing = self.get_head(
                    snapshot.root_task_id, snapshot.workspace_id, for_update=True
                )
            if existing is not None:
                raise VersionConflict(0, existing.version)
            if snapshot.generation != 1 or snapshot.parent_snapshot_id is not None:
                raise PlanGenerationConflict(1, snapshot.generation)
            self.add(snapshot)
            self._session.flush()
            head = RootTaskPlanHead(
                workspace_id=snapshot.workspace_id,
                root_task_id=snapshot.root_task_id,
                active_snapshot_id=snapshot.id,
                active_generation=snapshot.generation,
                active_snapshot_hash=plan_snapshot_content_hash(snapshot),
                version=1,
                updated_at=datetime.now(UTC),
            )
            self._session.add(_head_model(head))
            self._session.flush()
            return head

        self._validate_next_snapshot(snapshot, expected_head)
        self.add(snapshot)
        self._session.flush()
        return self.advance_head(
            expected_head,
            expected_snapshot_id=expected_head.active_snapshot_id,
            target=snapshot,
        )

    def get_head(
        self,
        root_task_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> RootTaskPlanHead | None:
        statement = select(RootTaskPlanHeadModel).where(
            RootTaskPlanHeadModel.root_task_id == root_task_id,
            RootTaskPlanHeadModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_head(row) if row is not None else None

    def advance_head(
        self,
        head: RootTaskPlanHead,
        *,
        expected_snapshot_id: UUID,
        target: TaskPlanSnapshot,
    ) -> RootTaskPlanHead:
        if (
            target.workspace_id != head.workspace_id
            or target.root_task_id != head.root_task_id
        ):
            raise ValidationError("plan head target belongs to another root or workspace")
        self._validate_next_snapshot(target, head)
        previous_version = head.version
        head.advance(
            expected_snapshot_id=expected_snapshot_id,
            expected_version=previous_version,
            target_snapshot_id=target.id,
            target_generation=target.generation,
            target_snapshot_hash=plan_snapshot_content_hash(target),
            now=datetime.now(UTC),
        )
        result = self._session.execute(
            update(RootTaskPlanHeadModel)
            .where(
                RootTaskPlanHeadModel.root_task_id == head.root_task_id,
                RootTaskPlanHeadModel.workspace_id == head.workspace_id,
                RootTaskPlanHeadModel.version == previous_version,
                RootTaskPlanHeadModel.active_snapshot_id == expected_snapshot_id,
            )
            .values(
                active_snapshot_id=head.active_snapshot_id,
                active_generation=head.active_generation,
                active_snapshot_hash=head.active_snapshot_hash,
                version=head.version,
                updated_at=head.updated_at,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            actual = self._session.scalar(
                select(RootTaskPlanHeadModel.version).where(
                    RootTaskPlanHeadModel.root_task_id == head.root_task_id,
                    RootTaskPlanHeadModel.workspace_id == head.workspace_id,
                )
            )
            raise VersionConflict(previous_version, actual or 0)
        return head

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
            .join(
                RootTaskPlanHeadModel,
                (RootTaskPlanHeadModel.active_snapshot_id == PlanSnapshotModel.id)
                & (RootTaskPlanHeadModel.workspace_id == PlanSnapshotModel.workspace_id)
                & (RootTaskPlanHeadModel.root_task_id == PlanSnapshotModel.root_task_id),
            )
            .where(
                PlanSnapshotModel.root_task_id == root_task_id,
                PlanSnapshotModel.workspace_id == workspace_id,
            )
        )
        return _to_domain(row) if row is not None else None

    def get_current_for_task(
        self, task_id: UUID, workspace_id: UUID
    ) -> TaskPlanSnapshot | None:
        rows = self._session.scalars(
            select(PlanSnapshotModel)
            .join(
                RootTaskPlanHeadModel,
                (RootTaskPlanHeadModel.active_snapshot_id == PlanSnapshotModel.id)
                & (RootTaskPlanHeadModel.workspace_id == PlanSnapshotModel.workspace_id)
                & (RootTaskPlanHeadModel.root_task_id == PlanSnapshotModel.root_task_id),
            )
            .where(PlanSnapshotModel.workspace_id == workspace_id)
            .order_by(PlanSnapshotModel.root_task_id)
        ).all()
        for row in rows:
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
            .order_by(PlanSnapshotModel.generation.asc())
        ).all()
        return tuple(_to_domain(row) for row in rows)

    def list_roots_with_snapshots(self, workspace_id: UUID) -> tuple[UUID, ...]:
        rows = self._session.scalars(
            select(PlanSnapshotModel.root_task_id)
            .where(PlanSnapshotModel.workspace_id == workspace_id)
            .distinct()
            .order_by(PlanSnapshotModel.root_task_id)
        ).all()
        return tuple(rows)

    @staticmethod
    def _validate_next_snapshot(
        snapshot: TaskPlanSnapshot, head: RootTaskPlanHead
    ) -> None:
        if snapshot.generation != head.active_generation + 1:
            raise PlanGenerationConflict(head.active_generation + 1, snapshot.generation)
        if snapshot.parent_snapshot_id != head.active_snapshot_id:
            raise ValidationError("snapshot parent must be the current plan head")


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


def _snapshot_model(snapshot: TaskPlanSnapshot) -> PlanSnapshotModel:
    return PlanSnapshotModel(
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


def _head_model(head: RootTaskPlanHead) -> RootTaskPlanHeadModel:
    return RootTaskPlanHeadModel(
        workspace_id=head.workspace_id,
        root_task_id=head.root_task_id,
        active_snapshot_id=head.active_snapshot_id,
        active_generation=head.active_generation,
        active_snapshot_hash=head.active_snapshot_hash,
        version=head.version,
        updated_at=head.updated_at,
    )


def _to_head(row: RootTaskPlanHeadModel) -> RootTaskPlanHead:
    return RootTaskPlanHead(
        workspace_id=row.workspace_id,
        root_task_id=row.root_task_id,
        active_snapshot_id=row.active_snapshot_id,
        active_generation=row.active_generation,
        active_snapshot_hash=row.active_snapshot_hash,
        version=row.version,
        updated_at=row.updated_at,
    )
