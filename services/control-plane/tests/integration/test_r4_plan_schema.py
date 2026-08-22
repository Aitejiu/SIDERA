from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import inspect, select

from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.persistence.models import (
    PlanSnapshotModel,
    ProjectionChangeModel,
    TaskModel,
    WorkspaceProjectionCursorModel,
)
from sidera_control_plane.persistence.plan_repository import PlanRepository


def test_r4_migration_creates_immutable_plan_and_projection_tables(database) -> None:
    with database.session() as session:
        tables = set(inspect(session.bind).get_table_names())

    assert {
        "task_plan_snapshots",
        "workspace_projection_cursors",
        "projection_changes",
    } <= tables


def test_plan_repository_round_trips_snapshot_and_current_generation(
    database, workspace_id
) -> None:
    snapshot = TaskPlanSnapshot.create(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000002"),
        schema_version=1,
        workspace_id=workspace_id,
        root_task_id=UUID("30000000-0000-0000-0000-000000000001"),
        generation=1,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="fixture:r4:g1",
        title="R4",
        goal_summary="Test immutable plan storage.",
        acceptance_refs=("test:r4",),
        work_items=(),
        worker_slots=(),
        milestones=(),
        dependencies=(),
        published_by="local:test",
        published_at=datetime(2026, 8, 21, tzinfo=UTC),
    )

    with database.session() as session:
        session.add(
            TaskModel(
                id=snapshot.root_task_id,
                workspace_id=workspace_id,
                title="R4 root task",
                description="Root task for plan snapshot storage.",
                acceptance_criteria=["snapshot persists"],
                status="pending",
                priority=50,
                requested_by="test",
                version=1,
            )
        )
        session.commit()

        repository = PlanRepository(session)
        repository.add(snapshot)
        session.commit()

    with database.session() as session:
        repository = PlanRepository(session)
        restored = repository.get(snapshot.id, workspace_id)
        current = repository.get_current_for_root(snapshot.root_task_id, workspace_id)
        assert restored == snapshot
        assert current == snapshot

        row = session.scalar(
            select(PlanSnapshotModel).where(PlanSnapshotModel.id == snapshot.id)
        )
        assert row is not None
        assert row.payload == snapshot.to_payload()


def test_r4_projection_tables_have_workspace_scoped_keys() -> None:
    assert WorkspaceProjectionCursorModel.__tablename__ == "workspace_projection_cursors"
    assert ProjectionChangeModel.__tablename__ == "projection_changes"
