from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.plan_commands import PublishPlanSnapshotHandler
from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    WorkerItemPlacement,
)
from sidera_control_plane.domain.commands import (
    CommandEnvelope,
    CommandResult,
    StoredCommand,
)
from sidera_control_plane.domain.errors import PlanGenerationConflict, ValidationError
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker_item import WorkerItem

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
MATERIALIZED_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")
FOREIGN_TASK_ID = UUID("30000000-0000-0000-0000-000000000003")


class FakeTaskRepository:
    def __init__(self, tasks: tuple[Task, ...]) -> None:
        self.tasks = {task.id: task for task in tasks}
        self.locked_ids: list[UUID] = []

    def get(
        self, task_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> Task | None:
        task = self.tasks.get(task_id)
        if task is None or task.workspace_id != workspace_id:
            return None
        if for_update:
            self.locked_ids.append(task_id)
        return deepcopy(task)

    def get_many(self, task_ids: tuple[UUID, ...], workspace_id: UUID) -> tuple[Task, ...]:
        return tuple(
            deepcopy(task)
            for task_id in task_ids
            if (task := self.tasks.get(task_id)) is not None
            and task.workspace_id == workspace_id
        )

    def workspace_exists(self, workspace_id: UUID) -> bool:
        return any(task.workspace_id == workspace_id for task in self.tasks.values())

    def add(self, task: Task) -> None:
        self.tasks[task.id] = deepcopy(task)

    def save(self, task: Task, previous_version: int) -> None:
        del previous_version
        self.tasks[task.id] = deepcopy(task)


class FakePlanRepository:
    def __init__(self, snapshots: tuple[TaskPlanSnapshot, ...] = ()) -> None:
        self.snapshots = {snapshot.id: snapshot for snapshot in snapshots}

    def add(self, snapshot: TaskPlanSnapshot) -> None:
        self.snapshots[snapshot.id] = snapshot

    def get(self, snapshot_id: UUID, workspace_id: UUID) -> TaskPlanSnapshot | None:
        snapshot = self.snapshots.get(snapshot_id)
        return snapshot if snapshot is not None and snapshot.workspace_id == workspace_id else None

    def get_current_for_root(
        self, root_task_id: UUID, workspace_id: UUID
    ) -> TaskPlanSnapshot | None:
        candidates = [
            snapshot
            for snapshot in self.snapshots.values()
            if snapshot.root_task_id == root_task_id
            and snapshot.workspace_id == workspace_id
        ]
        return max(candidates, key=lambda snapshot: snapshot.generation, default=None)

    def list_for_root(
        self, root_task_id: UUID, workspace_id: UUID
    ) -> tuple[TaskPlanSnapshot, ...]:
        return tuple(
            sorted(
                (
                    snapshot
                    for snapshot in self.snapshots.values()
                    if snapshot.root_task_id == root_task_id
                    and snapshot.workspace_id == workspace_id
                ),
                key=lambda snapshot: snapshot.generation,
                reverse=True,
            )
        )


class FakeCommandRepository:
    def __init__(self) -> None:
        self.records: dict[tuple[UUID, str, str, str], StoredCommand] = {}

    def find_duplicate(
        self,
        workspace_id: UUID,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> StoredCommand | None:
        return self.records.get((workspace_id, actor_type, actor_id, idempotency_key))

    def record_applied(
        self,
        envelope: CommandEnvelope,
        request_hash: str,
        result: CommandResult,
    ) -> None:
        self.records[(envelope.workspace_id, envelope.actor_type, envelope.actor_id, envelope.idempotency_key)] = StoredCommand(
            id=envelope.id,
            target_type=envelope.target_type,
            request_hash=request_hash,
            status="applied",
            result={
                "command_id": str(result.command_id),
                "aggregate_id": str(result.aggregate_id),
                "aggregate_version": result.aggregate_version,
                "event_ids": [str(event_id) for event_id in result.event_ids],
            },
            rejection_reason=None,
        )

    def record_rejected(
        self,
        envelope: CommandEnvelope,
        request_hash: str,
        error_code: str,
        error_result: dict[str, object],
    ) -> None:
        self.records[(envelope.workspace_id, envelope.actor_type, envelope.actor_id, envelope.idempotency_key)] = StoredCommand(
            id=envelope.id,
            target_type=envelope.target_type,
            request_hash=request_hash,
            status="rejected",
            result=error_result,
            rejection_reason=error_code,
        )


class FakeEventRepository:
    def __init__(self) -> None:
        self.events: list[EventData] = []

    def append_with_outbox(self, event: EventData) -> None:
        self.events.append(event)


class FakeWorkerItemRepository:
    def __init__(self) -> None:
        self.items: dict[UUID, WorkerItem] = {}
        self.added: list[WorkerItem] = []
        self.placements: dict[tuple[UUID, UUID], WorkerItemPlacement] = {}

    def get(
        self, item_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> WorkerItem | None:
        del for_update
        item = self.items.get(item_id)
        if item is None or item.workspace_id != workspace_id:
            return None
        return deepcopy(item)

    def add(self, item: WorkerItem) -> None:
        self.items[item.id] = deepcopy(item)
        self.added.append(deepcopy(item))

    def save(self, item: WorkerItem, previous_version: int) -> None:
        del previous_version
        self.items[item.id] = deepcopy(item)

    def get_by_stable_key(
        self, root_task_id: UUID, stable_key: str
    ) -> WorkerItem | None:
        return next(
            (
                deepcopy(item)
                for item in self.items.values()
                if item.root_task_id == root_task_id and item.stable_key == stable_key
            ),
            None,
        )

    def add_placement(self, placement: WorkerItemPlacement) -> None:
        key = (placement.plan_snapshot_id, placement.worker_item_id)
        existing = self.placements.get(key)
        if existing is not None and existing != placement:
            raise AssertionError("fake placement conflict")
        self.placements[key] = placement

    def get_placement(
        self, plan_snapshot_id: UUID, worker_item_id: UUID
    ) -> WorkerItemPlacement | None:
        return self.placements.get((plan_snapshot_id, worker_item_id))

    def get_current_placement(
        self, worker_item_id: UUID, workspace_id: UUID
    ) -> WorkerItemPlacement | None:
        del worker_item_id, workspace_id
        return None

    def list_placements(
        self, worker_item_id: UUID, workspace_id: UUID
    ) -> tuple[WorkerItemPlacement, ...]:
        del worker_item_id, workspace_id
        return ()

    def list_for_subtask(
        self, plan_snapshot_id: UUID, subtask_key: str
    ) -> tuple[WorkerItemPlacement, ...]:
        return tuple(
            placement
            for placement in self.placements.values()
            if placement.plan_snapshot_id == plan_snapshot_id
            and placement.subtask_key == subtask_key
        )


class FakeUnitOfWork:
    tasks: Any
    attention: Any
    plans: Any
    commands: Any
    events: Any
    projection_changes: Any
    workers: Any
    worker_items: Any
    executions: Any
    leases: Any
    sessions: Any
    modules: Any
    bindings: Any
    gateway_operations: Any
    workspaces: Any
    execution_events: Any
    artifacts: Any
    evidence_claims: Any
    execution_inputs: Any

    def __init__(
        self,
        tasks: FakeTaskRepository,
        plans: FakePlanRepository,
        commands: FakeCommandRepository | None = None,
    ) -> None:
        self.tasks = tasks
        self.plans = plans
        self.commands = commands or FakeCommandRepository()
        self.events = FakeEventRepository()
        self.worker_items = FakeWorkerItemRepository()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        plans = dict(self.plans.snapshots)
        events = list(self.events.events)
        worker_items = deepcopy(self.worker_items.items)
        added_worker_items = list(self.worker_items.added)
        placements = dict(self.worker_items.placements)
        try:
            yield
        except Exception:
            self.plans.snapshots = plans
            self.events.events = events
            self.worker_items.items = worker_items
            self.worker_items.added = added_worker_items
            self.worker_items.placements = placements
            raise


def make_task(task_id: UUID, workspace_id: UUID = WORKSPACE_ID) -> Task:
    return Task.create(
        task_id=task_id,
        workspace_id=workspace_id,
        title="Task",
        description="Task for plan publication",
        acceptance_criteria=("published",),
        priority=50,
        requested_by="test",
    )


def make_snapshot(
    *,
    snapshot_id: UUID,
    generation: int,
    parent_snapshot_id: UUID | None = None,
    workspace_id: UUID = WORKSPACE_ID,
    root_task_id: UUID = ROOT_TASK_ID,
    materialized_task_id: UUID | None = MATERIALIZED_TASK_ID,
) -> TaskPlanSnapshot:
    from sidera_control_plane.domain.plans import WorkItem, WorkItemKind

    work_items = ()
    if materialized_task_id is not None:
        work_items = (
            WorkItem(
                key="backend",
                task_id=materialized_task_id,
                kind=WorkItemKind.CHILD,
                title="Backend",
                goal_summary="Implement backend",
                acceptance_refs=("test:backend",),
                milestone_key=None,
                worker_slot_key=None,
                expected_outputs=("api",),
                source_refs=("test",),
            ),
        )
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=1,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        generation=generation,
        parent_snapshot_id=parent_snapshot_id,
        source_type="test",
        source_ref=f"test:g{generation}",
        title="Test plan",
        goal_summary="Publish a plan",
        acceptance_refs=("test:plan",),
        work_items=work_items,
        worker_slots=(),
        milestones=(),
        dependencies=(),
        published_by="local:test",
        published_at=datetime(2026, 8, 21, tzinfo=UTC),
    )


def make_envelope(
    snapshot: TaskPlanSnapshot,
    *,
    expected_generation: int,
    expected_version: int = 1,
    idempotency_key: str = "publish-001",
    target_type: str = "task",
    target_id: UUID | None = ROOT_TASK_ID,
) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=snapshot.workspace_id,
        command_type="PublishPlanSnapshot",
        target_type=target_type,
        actor_type="local",
        actor_id="aitejiu",
        target_id=target_id,
        payload={
            "expected_current_generation": expected_generation,
            "snapshot": snapshot.to_payload(),
        },
        expected_version=expected_version,
        idempotency_key=idempotency_key,
        correlation_id=uuid4(),
    )


def make_unit_of_work(
    *,
    snapshots: tuple[TaskPlanSnapshot, ...] = (),
    include_materialized_task: bool = True,
    include_root_task: bool = True,
    foreign_materialized_task: bool = False,
) -> FakeUnitOfWork:
    tasks: list[Task] = []
    if include_root_task:
        tasks.append(make_task(ROOT_TASK_ID))
    if include_materialized_task:
        tasks.append(
            make_task(
                FOREIGN_TASK_ID if foreign_materialized_task else MATERIALIZED_TASK_ID,
                UUID("20000000-0000-0000-0000-000000000002")
                if foreign_materialized_task
                else WORKSPACE_ID,
            )
        )
    return FakeUnitOfWork(FakeTaskRepository(tuple(tasks)), FakePlanRepository(snapshots))


def test_first_publish_writes_snapshot_and_auditable_event() -> None:
    snapshot = make_snapshot(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000001"),
        generation=1,
    )
    uow = make_unit_of_work()

    result = PublishPlanSnapshotHandler().handle(
        uow, make_envelope(snapshot, expected_generation=0)
    )

    assert result.aggregate_id == ROOT_TASK_ID
    assert result.aggregate_version == 1
    assert uow.plans.get(snapshot.id, WORKSPACE_ID) == snapshot
    assert len(uow.events.events) == 1
    assert uow.events.events[0].event_type == "task.plan_snapshot_published"
    assert uow.events.events[0].payload == {
        "snapshot_id": str(snapshot.id),
        "generation": 1,
        "root_task_id": str(ROOT_TASK_ID),
    }
    assert uow.tasks.locked_ids == [ROOT_TASK_ID]


def test_publish_is_idempotent_through_generic_command_service() -> None:
    snapshot = make_snapshot(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000001"),
        generation=1,
    )
    shared = make_unit_of_work()
    service = CommandService(
        lambda: cast(ControlPlaneUnitOfWork, shared),
        (PublishPlanSnapshotHandler(),),
    )
    envelope = make_envelope(snapshot, expected_generation=0)

    first = service.handle(envelope)
    replay = service.handle(replace(envelope, id=uuid4(), correlation_id=uuid4()))

    assert replay == first
    assert len(shared.plans.snapshots) == 1
    assert len(shared.events.events) == 1


def test_next_generation_requires_matching_parent() -> None:
    first = make_snapshot(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000001"),
        generation=1,
    )
    second = make_snapshot(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000002"),
        generation=2,
        parent_snapshot_id=first.id,
    )
    uow = make_unit_of_work(snapshots=(first,))

    PublishPlanSnapshotHandler().handle(
        uow, make_envelope(second, expected_generation=1, idempotency_key="publish-002")
    )

    assert uow.plans.get_current_for_root(ROOT_TASK_ID, WORKSPACE_ID) == second


@pytest.mark.parametrize(
    ("snapshot_generation", "expected_generation", "expected_actual"),
    [(1, 0, (0, 1))],
)
def test_generation_conflicts_are_rejected_without_writes(
    snapshot_generation: int,
    expected_generation: int,
    expected_actual: tuple[int, int],
) -> None:
    first = make_snapshot(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000001"),
        generation=1,
    )
    snapshot = make_snapshot(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000002"),
        generation=snapshot_generation,
        parent_snapshot_id=first.id if snapshot_generation > 1 else None,
    )
    uow = make_unit_of_work(snapshots=(first,) if expected_actual[1] == 1 else ())

    with pytest.raises(PlanGenerationConflict) as exc_info:
        PublishPlanSnapshotHandler().handle(
            uow,
            make_envelope(
                snapshot,
                expected_generation=expected_generation,
                idempotency_key=f"conflict-{snapshot_generation}",
            ),
        )

    assert (exc_info.value.expected, exc_info.value.actual) == expected_actual
    assert len(uow.plans.snapshots) == (1 if expected_actual[1] == 1 else 0)
    assert uow.events.events == []


def test_skipped_generation_is_rejected_without_writes() -> None:
    first = make_snapshot(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000001"),
        generation=1,
    )
    skipped = make_snapshot(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000003"),
        generation=3,
        parent_snapshot_id=first.id,
    )
    uow = make_unit_of_work(snapshots=(first,))

    with pytest.raises(ValidationError, match="generation"):
        PublishPlanSnapshotHandler().handle(
            uow, make_envelope(skipped, expected_generation=1)
        )

    assert len(uow.plans.snapshots) == 1
    assert uow.events.events == []


def test_missing_root_task_is_not_found() -> None:
    snapshot = make_snapshot(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000001"),
        generation=1,
    )
    uow = make_unit_of_work(include_root_task=False)

    with pytest.raises(NotFound):
        PublishPlanSnapshotHandler().handle(
            uow, make_envelope(snapshot, expected_generation=0)
        )


def test_foreign_materialized_task_is_rejected_atomically() -> None:
    snapshot = make_snapshot(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000001"),
        generation=1,
        materialized_task_id=FOREIGN_TASK_ID,
    )
    uow = make_unit_of_work(foreign_materialized_task=True)

    with pytest.raises(ValidationError, match="workspace"):
        PublishPlanSnapshotHandler().handle(
            uow, make_envelope(snapshot, expected_generation=0)
        )

    assert uow.plans.snapshots == {}
    assert uow.events.events == []
