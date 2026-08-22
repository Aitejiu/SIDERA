from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    WorkerItemPlacement,
)
from sidera_control_plane.application.worker_item_backfill import (
    WorkerItemBackfillService,
    responsibility_fingerprint,
)
from sidera_control_plane.domain.plans import (
    TaskPlanSnapshot,
    WorkerSlot,
    WorkItem,
    WorkItemKind,
)
from sidera_control_plane.domain.worker_item import WorkerItem

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
SUBTASK_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


class FakePlanRepository:
    def __init__(self, snapshots: tuple[TaskPlanSnapshot, ...]) -> None:
        self.snapshots = snapshots

    def list_for_root(self, root_task_id, workspace_id):
        return tuple(
            sorted(
                (
                    snapshot
                    for snapshot in self.snapshots
                    if snapshot.root_task_id == root_task_id
                    and snapshot.workspace_id == workspace_id
                ),
                key=lambda snapshot: snapshot.generation,
            )
        )


class FakeWorkerItemRepository:
    def __init__(self) -> None:
        self.items: dict[UUID, WorkerItem] = {}
        self.added: list[WorkerItem] = []
        self.placements: dict[tuple[UUID, UUID], WorkerItemPlacement] = {}

    def get_by_stable_key(self, root_task_id, stable_key):
        return next(
            (
                deepcopy(item)
                for item in self.items.values()
                if item.root_task_id == root_task_id and item.stable_key == stable_key
            ),
            None,
        )

    def get(self, item_id, workspace_id, *, for_update=False):
        del for_update
        item = self.items.get(item_id)
        if item is None or item.workspace_id != workspace_id:
            return None
        return deepcopy(item)

    def add(self, item):
        self.items[item.id] = deepcopy(item)
        self.added.append(deepcopy(item))

    def save(self, item, previous_version):
        del previous_version
        self.items[item.id] = deepcopy(item)

    def list_placements(self, worker_item_id, workspace_id):
        return tuple(
            placement
            for placement in self.placements.values()
            if placement.worker_item_id == worker_item_id
            and placement.workspace_id == workspace_id
        )

    def get_placement(self, plan_snapshot_id, worker_item_id):
        return self.placements.get((plan_snapshot_id, worker_item_id))

    def add_placement(self, placement):
        self.placements[(placement.plan_snapshot_id, placement.worker_item_id)] = placement

    def list_for_subtask(self, plan_snapshot_id, subtask_key):
        return tuple(
            placement
            for placement in self.placements.values()
            if placement.plan_snapshot_id == plan_snapshot_id
            and placement.subtask_key == subtask_key
        )


class FakeExecutionRepository:
    def list_ids_for_task_window(self, task_id, workspace_id, start, end):
        del task_id, workspace_id, start, end
        return ()

    def assign_worker_item_if_unset(self, execution_id, workspace_id, worker_item_id):
        del execution_id, workspace_id, worker_item_id
        return False


class FakeUnitOfWork:
    def __init__(self, snapshots: tuple[TaskPlanSnapshot, ...]) -> None:
        self.plans = FakePlanRepository(snapshots)
        self.worker_items = FakeWorkerItemRepository()
        self.executions = FakeExecutionRepository()


def make_snapshot(
    *,
    generation: int,
    goal_summary: str = "Build API",
    snapshot_id: UUID | None = None,
) -> TaskPlanSnapshot:
    parent_snapshot_id = (
        UUID("40000000-0000-0000-0000-000000000001")
        if generation > 1
        else None
    )
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id or uuid4(),
        schema_version=1,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=generation,
        parent_snapshot_id=parent_snapshot_id,
        source_type="test",
        source_ref=f"backfill:g{generation}",
        title="Legacy plan",
        goal_summary="Backfill legacy WorkerItems.",
        acceptance_refs=("test:backfill",),
        work_items=(
            WorkItem(
                key="backend",
                task_id=SUBTASK_TASK_ID,
                kind=WorkItemKind.CHILD,
                title="Backend",
                goal_summary=goal_summary,
                acceptance_refs=("api works",),
                milestone_key=None,
                worker_slot_key="implementer",
                expected_outputs=("api",),
                source_refs=("test",),
            ),
        ),
        worker_slots=(
            WorkerSlot(
                key="implementer",
                role="implementer",
                capability_requirements=("sql", "python"),
                worker_policy_ref="policy:legacy",
                configuration_ref="config:legacy",
                concurrency_limit=1,
            ),
        ),
        milestones=(),
        dependencies=(),
        published_by="test",
        published_at=NOW + timedelta(days=generation - 1),
    )


def test_legacy_fingerprint_is_stable_and_order_independent() -> None:
    assert responsibility_fingerprint(
        "implementer", "Build API", ("python", "sql")
    ) == responsibility_fingerprint(
        " implementer ", "Build API", ("sql", "python")
    )
    assert responsibility_fingerprint(
        "legacy:backend:implementer",
        "implementer",
        "Build API",
        ("python", "sql"),
    ) == responsibility_fingerprint(
        " legacy:backend:implementer ",
        " implementer ",
        "Build API",
        ("sql", "python"),
    )


def test_changed_legacy_fingerprint_creates_derived_generation_item() -> None:
    first_snapshot = make_snapshot(
        generation=1,
        snapshot_id=UUID("40000000-0000-0000-0000-000000000001"),
    )
    second_snapshot = make_snapshot(
        generation=2,
        goal_summary="Build API with streaming",
        snapshot_id=UUID("40000000-0000-0000-0000-000000000002"),
    )
    uow = FakeUnitOfWork((first_snapshot, second_snapshot))

    report = WorkerItemBackfillService().backfill_root(
        cast(ControlPlaneUnitOfWork, uow), WORKSPACE_ID, ROOT_TASK_ID
    )

    first, second = uow.worker_items.added
    assert second.id != first.id
    assert second.derived_from_worker_item_ids == (first.id,)
    assert report.created_worker_items == 2
