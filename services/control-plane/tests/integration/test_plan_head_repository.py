from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

import pytest

from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.persistence.models import TaskModel
from sidera_control_plane.persistence.plan_repository import PlanRepository

pytestmark = pytest.mark.integration

ROOT_TASK_ID = UUID("41000000-0000-0000-0000-000000000001")
SUBTASK_TASK_ID = UUID("41000000-0000-0000-0000-000000000002")
FIRST_SNAPSHOT_ID = UUID("42000000-0000-0000-0000-000000000001")
SECOND_SNAPSHOT_ID = UUID("42000000-0000-0000-0000-000000000002")
def make_plain_snapshot(
    workspace_id: UUID,
    *,
    snapshot_id: UUID,
    generation: int,
    parent_snapshot_id: UUID | None = None,
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=1,
        workspace_id=workspace_id,
        root_task_id=ROOT_TASK_ID,
        generation=generation,
        parent_snapshot_id=parent_snapshot_id,
        source_type="integration",
        source_ref=f"test:head:{generation}",
        title="Plan head test",
        goal_summary="Exercise explicit current plan pointers.",
        acceptance_refs=("head:test",),
        work_items=(),
        worker_slots=(),
        milestones=(),
        dependencies=(),
        published_by="integration",
        published_at=datetime(2026, 8, 28, tzinfo=UTC),
    )


def seed_tasks(database, workspace_id: UUID) -> None:
    with database.session() as session:
        session.add(
            TaskModel(
                id=ROOT_TASK_ID,
                workspace_id=workspace_id,
                title="Head root",
                description="Root task for head repository tests.",
                acceptance_criteria=["head:test"],
                status="pending",
                priority=50,
                requested_by="integration",
                version=1,
            )
        )
        session.add(
            TaskModel(
                id=SUBTASK_TASK_ID,
                workspace_id=workspace_id,
                title="Head subtask",
                description="Subtask for head repository tests.",
                acceptance_criteria=["head:test"],
                status="pending",
                priority=50,
                requested_by="integration",
                version=1,
            )
        )
        session.commit()


def test_head_is_explicit_and_cas_advances_current_plan(
    database, workspace_id: UUID
) -> None:
    seed_tasks(database, workspace_id)
    first = make_plain_snapshot(
        workspace_id, snapshot_id=FIRST_SNAPSHOT_ID, generation=1
    )
    second = make_plain_snapshot(
        workspace_id,
        snapshot_id=SECOND_SNAPSHOT_ID,
        generation=2,
        parent_snapshot_id=first.id,
    )

    with database.session() as session:
        repository = PlanRepository(session)
        repository.add_snapshot_and_advance_head(first, expected_head=None)
        session.commit()

    with database.session() as session:
        repository = PlanRepository(session)
        head = repository.get_head(ROOT_TASK_ID, workspace_id, for_update=True)
        assert head is not None
        assert head.active_snapshot_id == first.id
        repository.add_snapshot_and_advance_head(second, expected_head=head)
        session.commit()

    with database.session() as session:
        repository = PlanRepository(session)
        current = repository.get_current_for_root(ROOT_TASK_ID, workspace_id)
        assert current is not None
        assert current.id == second.id
        assert repository.get_for_generation(ROOT_TASK_ID, workspace_id, 1) == first


def test_current_lookup_does_not_fallback_to_max_generation(
    database, workspace_id: UUID
) -> None:
    seed_tasks(database, workspace_id)
    first = make_plain_snapshot(
        workspace_id, snapshot_id=FIRST_SNAPSHOT_ID, generation=1
    )
    orphan = make_plain_snapshot(
        workspace_id,
        snapshot_id=UUID("42000000-0000-0000-0000-000000000003"),
        generation=2,
        parent_snapshot_id=first.id,
    )

    with database.session() as session:
        repository = PlanRepository(session)
        repository.add(first)
        repository.add(orphan)
        session.commit()

    with database.session() as session:
        assert PlanRepository(session).get_current_for_root(ROOT_TASK_ID, workspace_id) is None


def test_stale_head_cas_is_rejected(
    database, workspace_id: UUID
) -> None:
    seed_tasks(database, workspace_id)
    first = make_plain_snapshot(
        workspace_id, snapshot_id=FIRST_SNAPSHOT_ID, generation=1
    )
    second = make_plain_snapshot(
        workspace_id,
        snapshot_id=SECOND_SNAPSHOT_ID,
        generation=2,
        parent_snapshot_id=first.id,
    )
    with database.session() as session:
        repository = PlanRepository(session)
        repository.add_snapshot_and_advance_head(first, expected_head=None)
        session.commit()

    with database.session() as session:
        stale = PlanRepository(session).get_head(ROOT_TASK_ID, workspace_id)
        assert stale is not None

    with database.session() as session:
        repository = PlanRepository(session)
        current = repository.get_head(ROOT_TASK_ID, workspace_id, for_update=True)
        assert current is not None
        repository.add_snapshot_and_advance_head(second, expected_head=current)
        session.commit()

    with database.session() as session:
        repository = PlanRepository(session)
        with pytest.raises(VersionConflict):
            repository.advance_head(
                deepcopy(stale),
                expected_snapshot_id=first.id,
                target=second,
            )
        session.rollback()
