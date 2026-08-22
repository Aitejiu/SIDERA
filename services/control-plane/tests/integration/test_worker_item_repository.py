from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from sidera_control_plane.application.ports import WorkerItemPlacement
from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import PlanSnapshotModel, TaskModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
SNAPSHOT_ONE_ID = UUID("40000000-0000-0000-0000-000000000001")
SNAPSHOT_TWO_ID = UUID("40000000-0000-0000-0000-000000000002")
WORKER_ITEM_ID = UUID("73000000-0000-0000-0000-000000000001")


def seed_plan_records(database: Database, workspace_id: UUID) -> None:
    with database.session() as session:
        session.add(
            TaskModel(
                id=ROOT_TASK_ID,
                workspace_id=workspace_id,
                title="WorkerItem repository task",
                description="Persist WorkerItem placements.",
                acceptance_criteria=["placement history is queryable"],
                status="pending",
                priority=50,
                requested_by="repository-test",
                version=1,
            )
        )
        session.commit()

    with database.session() as session:
        session.add_all(
            [
                PlanSnapshotModel(
                    id=SNAPSHOT_ONE_ID,
                    schema_version=2,
                    workspace_id=workspace_id,
                    root_task_id=ROOT_TASK_ID,
                    generation=1,
                    parent_snapshot_id=None,
                    source_type="test",
                    source_ref="repository:g1",
                    title="Repository plan 1",
                    goal_summary="First placement generation.",
                    payload={"schema_version": 2},
                    published_by="repository-test",
                    published_at=NOW,
                ),
                PlanSnapshotModel(
                    id=SNAPSHOT_TWO_ID,
                    schema_version=2,
                    workspace_id=workspace_id,
                    root_task_id=ROOT_TASK_ID,
                    generation=2,
                    parent_snapshot_id=SNAPSHOT_ONE_ID,
                    source_type="test",
                    source_ref="repository:g2",
                    title="Repository plan 2",
                    goal_summary="Second placement generation.",
                    payload={"schema_version": 2},
                    published_by="repository-test",
                    published_at=NOW,
                ),
            ]
        )
        session.commit()


def make_worker_item(workspace_id: UUID) -> WorkerItem:
    return WorkerItem.create(
        item_id=WORKER_ITEM_ID,
        workspace_id=workspace_id,
        root_task_id=ROOT_TASK_ID,
        title="Callback implementer",
        responsibility="Keep callback retry behavior idempotent.",
        now=NOW,
    )


def make_placement(
    workspace_id: UUID,
    snapshot_id: UUID,
    generation: int,
    worker_item_key: str,
    subtask_key: str,
) -> WorkerItemPlacement:
    return WorkerItemPlacement(
        plan_snapshot_id=snapshot_id,
        plan_generation=generation,
        workspace_id=workspace_id,
        root_task_id=ROOT_TASK_ID,
        worker_item_id=WORKER_ITEM_ID,
        worker_item_key=worker_item_key,
        subtask_key=subtask_key,
        subtask_task_id=None,
        role="implementer",
        capability_requirements=("python",),
        worker_policy_ref="policy:balanced",
        configuration_ref="config:callback",
        created_at=NOW,
    )


def test_worker_item_repository_round_trips_aggregate_and_placement_history(
    database: Database,
    workspace_id: UUID,
) -> None:
    seed_plan_records(database, workspace_id)
    item = make_worker_item(workspace_id)
    first = make_placement(
        workspace_id, SNAPSHOT_ONE_ID, 1, "callback-implementer", "callbacks"
    )
    second = make_placement(
        workspace_id, SNAPSHOT_TWO_ID, 2, "callback-implementer", "integration"
    )

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.worker_items.add(item)
        uow.worker_items.add_placement(first)
        uow.worker_items.add_placement(first)
        uow.worker_items.add_placement(second)

    with SqlAlchemyUnitOfWork(database) as uow:
        restored = uow.worker_items.get(item.id, workspace_id)
        locked = uow.worker_items.get(item.id, workspace_id, for_update=True)
        by_key = uow.worker_items.get_by_stable_key(
            ROOT_TASK_ID, f"worker-item:{WORKER_ITEM_ID}"
        )
        first_restored = uow.worker_items.get_placement(
            SNAPSHOT_ONE_ID, WORKER_ITEM_ID
        )
        current = uow.worker_items.get_current_placement(item.id, workspace_id)
        history = uow.worker_items.list_placements(item.id, workspace_id)
        local = uow.worker_items.list_for_subtask(SNAPSHOT_ONE_ID, "callbacks")

    assert restored == item
    assert locked == item
    assert by_key == item
    assert first_restored == first
    assert current == second
    assert history == (second, first)
    assert local == (first,)


def test_worker_item_repository_enforces_optimistic_versions_and_placement_immutability(
    database: Database,
    workspace_id: UUID,
) -> None:
    seed_plan_records(database, workspace_id)
    item = make_worker_item(workspace_id)
    placement = make_placement(
        workspace_id, SNAPSHOT_ONE_ID, 1, "callback-implementer", "callbacks"
    )

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.worker_items.add(item)
        uow.worker_items.add_placement(placement)

    with SqlAlchemyUnitOfWork(database) as uow:
        stored = uow.worker_items.get(item.id, workspace_id)
        assert stored is not None
        stored.advance_context(now=NOW)
        uow.worker_items.save(stored, previous_version=1)

    with SqlAlchemyUnitOfWork(database) as uow:
        stale = uow.worker_items.get(item.id, workspace_id)
        assert stale is not None
        with pytest.raises(VersionConflict, match="expected version"):
            uow.worker_items.save(stale, previous_version=1)

        changed = replace(placement, worker_item_key="different-key")
        with pytest.raises(ValidationError, match="immutable placement"):
            uow.worker_items.add_placement(changed)
