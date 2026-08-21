from datetime import UTC, datetime
from uuid import UUID

import pytest

from sidera_control_plane.domain.plans import (
    Dependency,
    DependencyEnforcement,
    DependencyKind,
    Milestone,
    PlanValidationError,
    TaskPlanSnapshot,
    WorkerSlot,
    WorkItem,
    WorkItemKind,
)

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
CHILD_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")


def make_snapshot(
    *,
    work_items: tuple[WorkItem, ...] | None = None,
    dependencies: tuple[Dependency, ...] = (),
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000001"),
        schema_version=1,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=1,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="fixture:r4:g1",
        title="Task First control room",
        goal_summary="Expose progress and exact intervention.",
        acceptance_refs=("spec:19.6",),
        work_items=work_items
        or (
            WorkItem(
                key="backend",
                task_id=CHILD_TASK_ID,
                kind=WorkItemKind.CHILD,
                title="Build projections",
                goal_summary="Serve canonical page views.",
                acceptance_refs=("spec:8.1",),
                milestone_key="m1",
                worker_slot_key="backend",
                expected_outputs=("task-overview",),
                source_refs=("spec:8",),
            ),
            WorkItem(
                key="web",
                task_id=None,
                kind=WorkItemKind.CHILD,
                title="Build Web",
                goal_summary="Render the Task First experience.",
                acceptance_refs=("spec:9",),
                milestone_key="m1",
                worker_slot_key="web",
                expected_outputs=("control-room",),
                source_refs=("spec:9",),
            ),
        ),
        worker_slots=(
            WorkerSlot("backend", "backend", ("python",), None, None, 1),
            WorkerSlot("web", "frontend", ("react",), None, None, 1),
        ),
        milestones=(
            Milestone("m1", "R4", ("backend", "web"), ("spec:22",), 1),
        ),
        dependencies=dependencies,
        published_by="local:aitejiu",
        published_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


def test_plan_snapshot_round_trips_as_explicit_json_payload() -> None:
    snapshot = make_snapshot(
        dependencies=(
            Dependency(
                "backend",
                "web",
                DependencyKind.HARD,
                (),
                DependencyEnforcement.INFORMATIONAL,
            ),
        )
    )

    restored = TaskPlanSnapshot.from_payload(snapshot.to_payload())

    assert restored == snapshot
    assert restored.work_items[1].task_id is None


def test_plan_snapshot_rejects_duplicate_work_item_keys() -> None:
    first = make_snapshot().work_items[0]
    duplicate = WorkItem(
        key=first.key,
        task_id=None,
        kind=WorkItemKind.INTEGRATION,
        title="Duplicate",
        goal_summary="Duplicate key must be rejected.",
        acceptance_refs=(),
        milestone_key=None,
        worker_slot_key=None,
        expected_outputs=(),
        source_refs=(),
    )

    with pytest.raises(PlanValidationError, match="work item keys must be unique"):
        make_snapshot(work_items=(first, duplicate))


def test_plan_snapshot_rejects_unknown_references_and_self_edges() -> None:
    with pytest.raises(PlanValidationError, match="dependency references unknown"):
        make_snapshot(
            dependencies=(
                Dependency(
                    "backend",
                    "missing",
                    DependencyKind.INFORMATIONAL,
                    (),
                    DependencyEnforcement.INFORMATIONAL,
                ),
            )
        )

    with pytest.raises(PlanValidationError, match="dependency cannot point to itself"):
        make_snapshot(
            dependencies=(
                Dependency(
                    "backend",
                    "backend",
                    DependencyKind.HARD,
                    (),
                    DependencyEnforcement.INFORMATIONAL,
                ),
            )
        )


def test_plan_snapshot_rejects_dependency_cycles() -> None:
    with pytest.raises(PlanValidationError, match="dependency graph must be acyclic"):
        make_snapshot(
            dependencies=(
                Dependency(
                    "backend",
                    "web",
                    DependencyKind.HARD,
                    (),
                    DependencyEnforcement.INFORMATIONAL,
                ),
                Dependency(
                    "web",
                    "backend",
                    DependencyKind.HARD,
                    (),
                    DependencyEnforcement.INFORMATIONAL,
                ),
            )
        )


def test_r4_rejects_scheduler_enforced_plan_dependencies() -> None:
    with pytest.raises(
        PlanValidationError, match="unsupported_dependency_enforcement"
    ):
        make_snapshot(
            dependencies=(
                Dependency(
                    "backend",
                    "web",
                    DependencyKind.HARD,
                    (),
                    DependencyEnforcement.CONTROL_PLANE,
                ),
            )
        )
