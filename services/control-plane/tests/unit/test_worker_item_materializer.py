from copy import deepcopy
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    WorkerItemPlacement,
)
from sidera_control_plane.application.worker_item_materializer import (
    WorkerItemMaterializer,
)
from sidera_control_plane.domain.plans import (
    PlanValidationError,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.domain.worker_item import WorkerItem

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("73000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
SUBTASK_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")


class FakeWorkerItemRepository:
    def __init__(self, items: tuple[WorkerItem, ...] = ()) -> None:
        self.items = {item.id: deepcopy(item) for item in items}
        self.added: list[WorkerItem] = []
        self.placements: dict[tuple[UUID, UUID], WorkerItemPlacement] = {}

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

    def get_by_stable_key(self, root_task_id, stable_key):
        for item in self.items.values():
            if item.root_task_id == root_task_id and item.stable_key == stable_key:
                return deepcopy(item)
        return None

    def add_placement(self, placement):
        key = (placement.plan_snapshot_id, placement.worker_item_id)
        existing = self.placements.get(key)
        if existing is not None and existing != placement:
            raise AssertionError("fake placement conflict")
        self.placements[key] = placement

    def get_placement(self, plan_snapshot_id, worker_item_id):
        return self.placements.get((plan_snapshot_id, worker_item_id))

    def get_current_placement(self, worker_item_id, workspace_id):
        del worker_item_id, workspace_id

    def list_placements(self, worker_item_id, workspace_id):
        del worker_item_id, workspace_id
        return ()

    def list_for_subtask(self, plan_snapshot_id, subtask_key):
        return tuple(
            placement
            for placement in self.placements.values()
            if placement.plan_snapshot_id == plan_snapshot_id
            and placement.subtask_key == subtask_key
        )


class FakeUnitOfWork:
    def __init__(self, items: tuple[WorkerItem, ...] = ()) -> None:
        self.worker_items = FakeWorkerItemRepository(items)


def materialize(
    uow: FakeUnitOfWork, snapshot: TaskPlanSnapshot
):
    return WorkerItemMaterializer().materialize(
        cast(ControlPlaneUnitOfWork, uow), snapshot, now=NOW
    )


def make_snapshot(
    *,
    snapshot_id: UUID | None = None,
    generation: int = 1,
    worker_item_id: UUID | None = None,
    responsibility: str = "Keep callback retries idempotent.",
    subtask_task_id: UUID | None = SUBTASK_TASK_ID,
    subtask_key: str = "callbacks",
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id or uuid4(),
        schema_version=2,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=generation,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="materializer:test",
        title="Materializer plan",
        goal_summary="Materialize a WorkerItem.",
        acceptance_refs=("test:materializer",),
        published_by="test",
        published_at=NOW,
        subtasks=(
            SubtaskPlanItem(
                key=subtask_key,
                task_id=subtask_task_id,
                kind=WorkItemKind.CHILD,
                title="Callbacks",
                goal_summary="Keep callbacks safe.",
                acceptance_refs=("callbacks are safe",),
                milestone_key=None,
                expected_outputs=("callback code",),
                source_refs=("test",),
            ),
        ),
        worker_item_plans=(
            WorkerItemPlanItem(
                key="callback-implementer",
                worker_item_id=worker_item_id,
                subtask_key=subtask_key,
                title="Callback implementer",
                role="implementer",
                responsibility=responsibility,
                capability_requirements=("python",),
                worker_policy_ref="policy:balanced",
                configuration_ref="config:callback",
                concurrency_limit=1,
                source_refs=("test",),
            ),
        ),
        subtask_milestones=(),
        subtask_dependencies=(),
        worker_item_dependencies=(),
    )


def existing_item() -> WorkerItem:
    return WorkerItem.create(
        item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        title="Callback implementer",
        responsibility="Keep callback retries idempotent.",
        now=NOW,
    )


def test_new_v2_plan_item_creates_stable_worker_item_and_resolved_snapshot() -> None:
    uow = FakeUnitOfWork()
    snapshot = make_snapshot()

    result = materialize(uow, snapshot)

    created = uow.worker_items.added[0]
    assert created.stable_key == f"worker-item:{created.id}"
    assert result.snapshot.worker_item_plans[0].worker_item_id == created.id
    assert result.placements[0].worker_item_id == created.id
    assert result.created_worker_item_ids == (created.id,)


def test_explicit_reuse_keeps_identity_and_adds_new_placement() -> None:
    uow = FakeUnitOfWork((existing_item(),))
    snapshot = make_snapshot(
        generation=2,
        worker_item_id=WORKER_ITEM_ID,
        subtask_key="integration",
    )

    result = materialize(uow, snapshot)

    assert result.created_worker_item_ids == ()
    assert result.placements[0].worker_item_id == WORKER_ITEM_ID
    assert result.snapshot.worker_item_plans[0].worker_item_id == WORKER_ITEM_ID


def test_explicit_reuse_rejects_responsibility_change() -> None:
    uow = FakeUnitOfWork((existing_item(),))
    snapshot = make_snapshot(
        worker_item_id=WORKER_ITEM_ID,
        responsibility="Replace the callback architecture entirely.",
    )

    with pytest.raises(PlanValidationError) as error:
        materialize(uow, snapshot)

    assert error.value.reason == "worker_item_responsibility_change_not_supported"


def test_explicit_reuse_rejects_cross_root_or_workspace_identity() -> None:
    other_item = WorkerItem.create(
        item_id=WORKER_ITEM_ID,
        workspace_id=UUID("20000000-0000-0000-0000-000000000002"),
        root_task_id=UUID("30000000-0000-0000-0000-000000000002"),
        title="Callback implementer",
        responsibility="Keep callback retries idempotent.",
        now=NOW,
    )
    uow = FakeUnitOfWork((other_item,))
    snapshot = make_snapshot(worker_item_id=WORKER_ITEM_ID)

    with pytest.raises(PlanValidationError) as error:
        materialize(uow, snapshot)

    assert error.value.reason == "worker_item_reuse_not_found"


def test_non_materialized_subtask_keeps_nullable_placement_task_id() -> None:
    uow = FakeUnitOfWork()
    snapshot = make_snapshot(subtask_task_id=None)

    result = materialize(uow, snapshot)

    assert result.placements[0].subtask_task_id is None


def test_repeating_same_snapshot_reuses_existing_placement() -> None:
    uow = FakeUnitOfWork()
    snapshot = make_snapshot()

    first = materialize(uow, snapshot)
    second = materialize(uow, snapshot)

    assert len(uow.worker_items.added) == 1
    assert second.snapshot == first.snapshot
    assert second.created_worker_item_ids == ()
