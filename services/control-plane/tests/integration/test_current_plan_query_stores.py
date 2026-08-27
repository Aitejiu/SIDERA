from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.plans import TaskPlanSnapshot, WorkItem, WorkItemKind
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
    TaskModel,
)
from sidera_control_plane.persistence.subtask_workspace_query_store import (
    SqlAlchemySubtaskWorkspaceQueryStore,
)
from sidera_control_plane.persistence.task_console_query_store import (
    SqlAlchemyTaskConsoleQueryStore,
)
from sidera_control_plane.persistence.task_first_query_store import (
    SqlAlchemyTaskFirstQueryStore,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("36000000-0000-0000-0000-000000000001")
SUBTASK_ID = UUID("36000000-0000-0000-0000-000000000002")
ORPHAN_ROOT_TASK_ID = UUID("36000000-0000-0000-0000-000000000003")


def _snapshot(
    *, root_task_id: UUID, snapshot_id: UUID, generation: int, title: str
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=1,
        workspace_id=WORKSPACE_ID,
        root_task_id=root_task_id,
        generation=generation,
        parent_snapshot_id=None,
        source_type="integration",
        source_ref=f"current-query:{generation}",
        title=title,
        goal_summary=title,
        acceptance_refs=("acceptance:current-query",),
        work_items=(
            WorkItem(
                key="subtask",
                task_id=SUBTASK_ID,
                kind=WorkItemKind.CHILD,
                title="Current query subtask",
                goal_summary="Read the explicit plan head.",
                acceptance_refs=("acceptance:current-query",),
                milestone_key=None,
                worker_slot_key=None,
                expected_outputs=(),
                source_refs=("integration",),
            ),
        ),
        worker_slots=(),
        milestones=(),
        dependencies=(),
        published_by="integration",
        published_at=NOW,
    )


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


def _task(task_id: UUID, title: str) -> TaskModel:
    return TaskModel(
        id=task_id,
        workspace_id=WORKSPACE_ID,
        title=title,
        description=title,
        acceptance_criteria=["acceptance:current-query"],
        status="pending",
        priority=50,
        requested_by="integration",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _seed(database: Database) -> tuple[TaskPlanSnapshot, TaskPlanSnapshot]:
    first = _snapshot(
        root_task_id=ROOT_TASK_ID,
        snapshot_id=uuid4(),
        generation=1,
        title="Head-selected plan",
    )
    orphan = _snapshot(
        root_task_id=ROOT_TASK_ID,
        snapshot_id=uuid4(),
        generation=2,
        title="Higher orphan plan",
    )
    orphan_root_snapshot = _snapshot(
        root_task_id=ORPHAN_ROOT_TASK_ID,
        snapshot_id=uuid4(),
        generation=1,
        title="Root without head",
    )
    with database.session() as session:
        session.add_all(
            [
                _task(ROOT_TASK_ID, "Current query root"),
                _task(SUBTASK_ID, "Current query subtask"),
                _task(ORPHAN_ROOT_TASK_ID, "Orphan root"),
            ]
        )
        session.flush()
        session.add_all(
            [
                _snapshot_model(first),
                _snapshot_model(orphan),
                _snapshot_model(orphan_root_snapshot),
                RootTaskPlanHeadModel(
                    workspace_id=WORKSPACE_ID,
                    root_task_id=ROOT_TASK_ID,
                    active_snapshot_id=first.id,
                    active_generation=first.generation,
                    active_snapshot_hash=plan_snapshot_content_hash(first),
                    version=1,
                    updated_at=NOW,
                ),
            ]
        )
        session.commit()
    return first, orphan


def test_current_plan_query_stores_follow_explicit_head(
    database: Database, workspace_id: UUID
) -> None:
    assert workspace_id == WORKSPACE_ID
    first, orphan = _seed(database)

    task_first = SqlAlchemyTaskFirstQueryStore(database)
    first_view = task_first.load(WORKSPACE_ID, ROOT_TASK_ID)
    assert first_view is not None
    assert first_view.plan.id == first.id
    assert first_view.plan.id != orphan.id
    assert task_first.list_current_root_ids(WORKSPACE_ID) == (ROOT_TASK_ID,)

    task_console = SqlAlchemyTaskConsoleQueryStore(database)
    console_view = task_console.load(WORKSPACE_ID, ROOT_TASK_ID)
    assert console_view is not None
    assert console_view.plan.snapshot_id == first.id
    assert task_console.list_current_root_ids(WORKSPACE_ID) == (ROOT_TASK_ID,)

    subtask_view = SqlAlchemySubtaskWorkspaceQueryStore(database).load(
        WORKSPACE_ID, ROOT_TASK_ID, "subtask"
    )
    assert subtask_view is not None
    assert subtask_view.plan.snapshot_id == first.id
