from datetime import UTC, datetime
from uuid import UUID

from sidera_control_plane.domain.plan_normalization import PlanNormalizer
from sidera_control_plane.domain.plans import (
    Dependency,
    DependencyEnforcement,
    DependencyKind,
    Milestone,
    TaskPlanSnapshot,
    WorkerSlot,
    WorkItem,
    WorkItemKind,
)

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
BACKEND_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")


def v1_snapshot(*, include_slot: bool = True) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000011"),
        schema_version=1,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=1,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="fixture:v1",
        title="Legacy plan",
        goal_summary="Normalize the old plan.",
        acceptance_refs=("legacy:acceptance",),
        work_items=(
            WorkItem(
                "backend",
                BACKEND_TASK_ID,
                WorkItemKind.CHILD,
                "Build backend",
                "Implement the callback retry API.",
                ("legacy:backend",),
                "m1",
                "implementer" if include_slot else None,
                ("api",),
                ("legacy:source",),
            ),
            WorkItem(
                "tests",
                None,
                WorkItemKind.CHILD,
                "Run tests",
                "Verify the callback retry API.",
                ("legacy:tests",),
                "m1",
                "tester" if include_slot else None,
                ("report",),
                ("legacy:source",),
            ),
        ),
        worker_slots=(
            WorkerSlot("implementer", "backend", ("python", "sql"), None, None, 1),
            WorkerSlot("tester", "qa", ("python",), None, None, 1),
        )
        if include_slot
        else (),
        milestones=(Milestone("m1", "Callback", ("backend", "tests"), (), 1),),
        dependencies=(
            Dependency(
                "backend",
                "tests",
                DependencyKind.HARD,
                ("evidence:backend",),
                DependencyEnforcement.INFORMATIONAL,
            ),
        ),
        published_by="test",
        published_at=datetime(2026, 8, 22, tzinfo=UTC),
    )


def test_v1_normalizes_each_work_item_slot_pair_to_one_worker_item() -> None:
    canonical = PlanNormalizer().normalize(v1_snapshot())

    assert [item.key for item in canonical.subtasks] == ["backend", "tests"]
    assert canonical.worker_items[0].legacy_identity_key == (
        "legacy:backend:implementer"
    )
    assert canonical.worker_items[0].subtask_key == "backend"
    assert canonical.worker_items[0].responsibility == "Implement the callback retry API."
    assert canonical.worker_item_dependencies == ()
    assert canonical.subtask_dependencies[0].upstream_subtask_key == "backend"
    assert canonical.milestones[0].subtask_keys == ("backend", "tests")


def test_v1_without_worker_slot_remains_a_planned_subtask() -> None:
    canonical = PlanNormalizer().normalize(v1_snapshot(include_slot=False))

    assert len(canonical.subtasks) == 2
    assert canonical.worker_items == ()


def test_v2_preserves_explicit_worker_item_reuse_identity() -> None:
    snapshot = TaskPlanSnapshot.from_payload(
        {
            "id": "10000000-0000-0000-0000-000000000012",
            "schema_version": 2,
            "workspace_id": str(WORKSPACE_ID),
            "root_task_id": str(ROOT_TASK_ID),
            "generation": 2,
            "parent_snapshot_id": None,
            "source_type": "test",
            "source_ref": "fixture:v2",
            "title": "New plan",
            "goal_summary": "Reuse explicit identity.",
            "acceptance_refs": [],
            "subtasks": [
                {
                    "key": "backend",
                    "task_id": str(BACKEND_TASK_ID),
                    "kind": "child",
                    "title": "Build backend",
                    "goal_summary": "Implement the callback retry API.",
                    "acceptance_refs": [],
                    "milestone_key": None,
                    "expected_outputs": [],
                    "source_refs": [],
                },
            ],
            "worker_items": [
                {
                    "key": "backend-worker",
                    "worker_item_id": "73000000-0000-0000-0000-000000000001",
                    "subtask_key": "backend",
                    "title": "Build backend",
                    "role": "backend",
                    "responsibility": "Implement the callback retry API.",
                    "capability_requirements": ["python"],
                    "worker_policy_ref": None,
                    "configuration_ref": None,
                    "concurrency_limit": 1,
                    "source_refs": [],
                },
            ],
            "milestones": [],
            "subtask_dependencies": [],
            "worker_item_dependencies": [],
            "published_by": "test",
            "published_at": "2026-08-22T00:00:00+00:00",
        }
    )

    canonical = PlanNormalizer().normalize(snapshot)

    assert canonical.worker_items[0].worker_item_id == UUID(
        "73000000-0000-0000-0000-000000000001"
    )
    assert canonical.worker_items[0].legacy_identity_key is None
