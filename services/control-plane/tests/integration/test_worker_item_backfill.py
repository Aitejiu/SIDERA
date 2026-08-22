from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from sidera_control_plane.application.worker_item_backfill import (
    WorkerItemBackfillService,
)
from sidera_control_plane.domain.plans import (
    TaskPlanSnapshot,
    WorkerSlot,
    WorkItem,
    WorkItemKind,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    TaskModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
)
from sidera_control_plane.persistence.plan_repository import PlanRepository
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _task(task_id: UUID, workspace_id: UUID, title: str) -> TaskModel:
    return TaskModel(
        id=task_id,
        workspace_id=workspace_id,
        title=title,
        description=title,
        acceptance_criteria=["backfill is deterministic"],
        status="pending",
        priority=50,
        requested_by="backfill-test",
        version=1,
    )


def _seed_tasks(
    database: Database,
    workspace_id: UUID,
    root_task_id: UUID,
    subtask_task_ids: tuple[UUID, ...],
) -> None:
    with database.session() as session:
        session.add(_task(root_task_id, workspace_id, "Backfill root"))
        session.add_all(
            _task(task_id, workspace_id, f"Backfill subtask {index}")
            for index, task_id in enumerate(subtask_task_ids)
        )
        session.commit()


def _snapshot(
    *,
    workspace_id: UUID,
    root_task_id: UUID,
    snapshot_id: UUID,
    generation: int,
    published_at: datetime,
    subtask_task_id: UUID,
    goal_summary: str = "Build API",
    worker_specs: tuple[tuple[str, str, str, str], ...] = (
        ("backend", "Backend", "Build API", "implementer"),
    ),
) -> TaskPlanSnapshot:
    work_items = tuple(
        WorkItem(
            key=key,
            task_id=subtask_task_id,
            kind=WorkItemKind.CHILD,
            title=title,
            goal_summary=goal,
            acceptance_refs=("api works",),
            milestone_key=None,
            worker_slot_key=slot_key,
            expected_outputs=("api",),
            source_refs=("backfill-test",),
        )
        for key, title, goal, slot_key in worker_specs
    )
    worker_slots = tuple(
        WorkerSlot(
            key=slot_key,
            role="implementer",
            capability_requirements=("python", "sql"),
            worker_policy_ref="policy:legacy",
            configuration_ref="config:legacy",
            concurrency_limit=1,
        )
        for _key, _title, _goal, slot_key in worker_specs
    )
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=1,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        generation=generation,
        parent_snapshot_id=None,
        source_type="test",
        source_ref=f"backfill:g{generation}",
        title="Legacy plan",
        goal_summary=goal_summary,
        acceptance_refs=("backfill works",),
        work_items=work_items,
        worker_slots=worker_slots,
        milestones=(),
        dependencies=(),
        published_by="backfill-test",
        published_at=published_at,
    )


def _seed_snapshots(
    database: Database, snapshots: tuple[TaskPlanSnapshot, ...]
) -> None:
    with database.session() as session:
        repository = PlanRepository(session)
        for snapshot in snapshots:
            repository.add(snapshot)
            session.commit()


def _seed_execution(
    database: Database,
    workspace_id: UUID,
    task_id: UUID,
    created_at: datetime,
) -> UUID:
    execution_id = uuid4()
    with database.session() as session:
        session.add(
            ExecutionModel(
                id=execution_id,
                workspace_id=workspace_id,
                task_id=task_id,
                lineage_id=execution_id,
                attempt=1,
                max_attempts=1,
                purpose="Backfill execution",
                status="queued",
                capability_requirements=["python"],
                pinned_worker_id=None,
                assigned_worker_id=None,
                active_lease_id=None,
                worker_item_id=None,
                last_fencing_token=0,
                not_before=created_at,
                failure_code=None,
                started_at=None,
                finished_at=None,
                version=1,
                stop_requested=False,
                source_execution_id=None,
                source_session_id=None,
                created_at=created_at,
            )
        )
        session.commit()
    return execution_id


def _backfill(
    database: Database, workspace_id: UUID, root_task_id: UUID
):
    with SqlAlchemyUnitOfWork(database) as uow:
        return WorkerItemBackfillService().backfill_root(
            uow, workspace_id, root_task_id
        )


def test_backfill_reuses_generations_derives_changes_and_links_unique_executions(
    database: Database, workspace_id: UUID
) -> None:
    root_reused = uuid4()
    subtask_reused = uuid4()
    root_changed = uuid4()
    subtask_changed = uuid4()
    _seed_tasks(
        database,
        workspace_id,
        root_reused,
        (subtask_reused,),
    )
    _seed_tasks(
        database,
        workspace_id,
        root_changed,
        (subtask_changed,),
    )

    reused_first_id = uuid4()
    reused_second_id = uuid4()
    reused_first = _snapshot(
        workspace_id=workspace_id,
        root_task_id=root_reused,
        snapshot_id=reused_first_id,
        generation=1,
        published_at=NOW,
        subtask_task_id=subtask_reused,
    )
    reused_second = _snapshot(
        workspace_id=workspace_id,
        root_task_id=root_reused,
        snapshot_id=reused_second_id,
        generation=2,
        published_at=NOW + timedelta(days=1),
        subtask_task_id=subtask_reused,
    )
    changed_first = _snapshot(
        workspace_id=workspace_id,
        root_task_id=root_changed,
        snapshot_id=uuid4(),
        generation=1,
        published_at=NOW,
        subtask_task_id=subtask_changed,
    )
    changed_second = _snapshot(
        workspace_id=workspace_id,
        root_task_id=root_changed,
        snapshot_id=uuid4(),
        generation=2,
        published_at=NOW + timedelta(days=1),
        subtask_task_id=subtask_changed,
        worker_specs=(("backend", "Backend", "Build streaming API", "implementer"),),
    )
    _seed_snapshots(
        database,
        (reused_first, reused_second, changed_first, changed_second),
    )
    reused_execution_one = _seed_execution(
        database, workspace_id, subtask_reused, NOW + timedelta(minutes=1)
    )
    reused_execution_two = _seed_execution(
        database,
        workspace_id,
        subtask_reused,
        NOW + timedelta(days=1, minutes=1),
    )

    first_report = _backfill(database, workspace_id, root_reused)
    assert first_report.created_worker_items == 1
    assert first_report.created_placements == 2
    assert first_report.linked_executions == 2
    second_report = _backfill(database, workspace_id, root_reused)
    assert second_report.created_worker_items == 0
    assert second_report.created_placements == 0
    assert second_report.linked_executions == 0

    with database.session() as session:
        reused_items = session.scalars(
            select(WorkerItemModel).where(
                WorkerItemModel.root_task_id == root_reused
            )
        ).all()
        reused_placements = session.scalars(
            select(WorkerItemPlacementModel).where(
                WorkerItemPlacementModel.root_task_id == root_reused
            )
        ).all()
        executions = session.scalars(
            select(ExecutionModel).where(
                ExecutionModel.id.in_((reused_execution_one, reused_execution_two))
            )
        ).all()
        assert len(reused_items) == 1
        assert len(reused_placements) == 2
        assert {execution.worker_item_id for execution in executions} == {
            reused_items[0].id
        }

    changed_report = _backfill(database, workspace_id, root_changed)
    assert changed_report.created_worker_items == 2
    assert changed_report.created_placements == 2
    with database.session() as session:
        changed_items = session.scalars(
            select(WorkerItemModel)
            .where(WorkerItemModel.root_task_id == root_changed)
            .order_by(WorkerItemModel.created_at)
        ).all()
        assert len(changed_items) == 2
        assert changed_items[1].derived_from_worker_item_ids == [
            str(changed_items[0].id)
        ]


def test_backfill_keeps_ambiguous_execution_unbound_and_rolls_back_root(
    database: Database, workspace_id: UUID
) -> None:
    ambiguous_root = uuid4()
    ambiguous_subtask = uuid4()
    invalid_root = uuid4()
    missing_subtask = uuid4()
    _seed_tasks(database, workspace_id, ambiguous_root, (ambiguous_subtask,))
    _seed_tasks(database, workspace_id, invalid_root, ())
    ambiguous_snapshot = _snapshot(
        workspace_id=workspace_id,
        root_task_id=ambiguous_root,
        snapshot_id=uuid4(),
        generation=1,
        published_at=NOW,
        subtask_task_id=ambiguous_subtask,
    )
    invalid_snapshot = _snapshot(
        workspace_id=workspace_id,
        root_task_id=invalid_root,
        snapshot_id=uuid4(),
        generation=1,
        published_at=NOW,
        subtask_task_id=missing_subtask,
    )
    _seed_snapshots(database, (ambiguous_snapshot, invalid_snapshot))
    extra_worker_item_id = uuid4()
    with database.session() as session:
        session.add(
            WorkerItemModel(
                id=extra_worker_item_id,
                workspace_id=workspace_id,
                root_task_id=ambiguous_root,
                stable_key="legacy:extra",
                title="Historical extra placement",
                responsibility="Historical data must remain auditable.",
                status="active",
                context_version=1,
                acknowledged_context_version=1,
                derived_from_worker_item_ids=[],
                version=1,
                created_at=NOW,
            )
        )
        session.add(
            WorkerItemPlacementModel(
                plan_snapshot_id=ambiguous_snapshot.id,
                plan_generation=1,
                workspace_id=workspace_id,
                root_task_id=ambiguous_root,
                worker_item_id=extra_worker_item_id,
                worker_item_key="legacy-extra",
                subtask_key="backend",
                subtask_task_id=ambiguous_subtask,
                role="reviewer",
                capability_requirements=["review"],
                worker_policy_ref="policy:legacy",
                configuration_ref="config:legacy",
                created_at=NOW,
            )
        )
        session.commit()
    ambiguous_execution = _seed_execution(
        database, workspace_id, ambiguous_subtask, NOW + timedelta(minutes=1)
    )

    report = _backfill(database, workspace_id, ambiguous_root)
    assert report.linked_executions == 0
    assert report.ambiguous_execution_ids == (ambiguous_execution,)
    with database.session() as session:
        execution = session.scalar(
            select(ExecutionModel).where(ExecutionModel.id == ambiguous_execution)
        )
        assert execution is not None
        assert execution.worker_item_id is None

    with pytest.raises(IntegrityError):
        _backfill(database, workspace_id, invalid_root)

    with database.session() as session:
        assert (
            session.scalar(
                select(WorkerItemModel.id).where(
                    WorkerItemModel.root_task_id == invalid_root
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(WorkerItemPlacementModel.plan_snapshot_id).where(
                    WorkerItemPlacementModel.root_task_id == invalid_root
                )
            )
            is None
        )
