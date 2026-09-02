from datetime import UTC, datetime
from uuid import UUID

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.plans import (
    Dependency,
    DependencyEnforcement,
    DependencyKind,
    Milestone,
    PlanValidationError,
    SubtaskMilestone,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemDependency,
    WorkerItemPlanItem,
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
            ),
        )


def plan_v2_payload() -> dict[str, object]:
    return {
        "id": "10000000-0000-0000-0000-000000000002",
        "schema_version": 2,
        "workspace_id": str(WORKSPACE_ID),
        "root_task_id": str(ROOT_TASK_ID),
        "generation": 2,
        "parent_snapshot_id": None,
        "source_type": "test",
        "source_ref": "fixture:r4-1:g2",
        "title": "Task First control room",
        "goal_summary": "Expose stable WorkerItem workspaces.",
        "acceptance_refs": ["spec:r4.1"],
        "subtasks": [
            {
                "key": "callback-idempotency",
                "task_id": str(CHILD_TASK_ID),
                "kind": "child",
                "title": "Callback idempotency",
                "goal_summary": "Make callback retries idempotent.",
                "acceptance_refs": ["acceptance:callback"],
                "milestone_key": "m1",
                "expected_outputs": ["callback-api"],
                "source_refs": ["spec:callback"],
            },
            {
                "key": "integration",
                "task_id": None,
                "kind": "integration",
                "title": "Integration",
                "goal_summary": "Verify the delivery path.",
                "acceptance_refs": ["acceptance:integration"],
                "milestone_key": "m1",
                "expected_outputs": ["integration-report"],
                "source_refs": ["spec:integration"],
            },
        ],
        "worker_items": [
            {
                "key": "callback-implementer",
                "worker_item_id": None,
                "subtask_key": "callback-idempotency",
                "title": "Implementer",
                "role": "backend",
                "responsibility": "Implement callback retry behavior.",
                "capability_requirements": ["python", "sql"],
                "worker_policy_ref": None,
                "configuration_ref": None,
                "concurrency_limit": 1,
                "source_refs": ["planner"],
            },
            {
                "key": "callback-reviewer",
                "worker_item_id": "73000000-0000-0000-0000-000000000001",
                "subtask_key": "callback-idempotency",
                "title": "Reviewer",
                "role": "reviewer",
                "responsibility": "Review callback retry evidence.",
                "capability_requirements": ["python"],
                "worker_policy_ref": "balanced",
                "configuration_ref": "review-default",
                "concurrency_limit": 1,
                "source_refs": ["planner"],
            },
            {
                "key": "integration-checker",
                "worker_item_id": None,
                "subtask_key": "integration",
                "title": "Integration checker",
                "role": "qa",
                "responsibility": "Verify the delivery path.",
                "capability_requirements": ["python"],
                "worker_policy_ref": None,
                "configuration_ref": None,
                "concurrency_limit": None,
                "source_refs": ["planner"],
            },
        ],
        "milestones": [
            {
                "key": "m1",
                "title": "Callback delivery",
                "subtask_keys": ["callback-idempotency", "integration"],
                "acceptance_refs": ["acceptance:callback"],
                "order_hint": 1,
            },
        ],
        "subtask_dependencies": [
            {
                "upstream_subtask_key": "callback-idempotency",
                "downstream_subtask_key": "integration",
                "kind": "hard",
                "required_evidence_refs": ["evidence:callback"],
                "enforcement": "informational",
            },
        ],
        "worker_item_dependencies": [
            {
                "upstream_worker_item_key": "callback-implementer",
                "downstream_worker_item_key": "callback-reviewer",
                "kind": "review",
                "required_artifact_refs": ["artifact:callback-diff"],
            },
        ],
        "published_by": "local:aitejiu",
        "published_at": "2026-08-22T00:00:00+00:00",
    }


def test_schema_v2_separates_subtasks_and_worker_items() -> None:
    snapshot = TaskPlanSnapshot.from_payload(plan_v2_payload())

    assert snapshot.schema_version == 2
    assert snapshot.subtasks[0].key == "callback-idempotency"
    assert snapshot.worker_item_plans[0].subtask_key == "callback-idempotency"
    assert snapshot.subtask_milestones[0].subtask_keys == (
        "callback-idempotency",
        "integration",
    )
    assert snapshot.worker_item_dependencies[0].kind == "review"
    assert snapshot.to_payload() == plan_v2_payload()


def test_schema_v2_round_trips_typed_objects() -> None:
    snapshot = TaskPlanSnapshot.create(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000002"),
        schema_version=2,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=2,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="fixture:r4-1:g2",
        title="Task First control room",
        goal_summary="Expose stable WorkerItem workspaces.",
        acceptance_refs=("spec:r4.1",),
        subtasks=(
            SubtaskPlanItem(
                "callback-idempotency",
                CHILD_TASK_ID,
                WorkItemKind.CHILD,
                "Callback idempotency",
                "Make callback retries idempotent.",
                ("acceptance:callback",),
                "m1",
                ("callback-api",),
                ("spec:callback",),
            ),
        ),
        worker_item_plans=(
            WorkerItemPlanItem(
                "callback-implementer",
                None,
                "callback-idempotency",
                "Implementer",
                "backend",
                "Implement callback retry behavior.",
                ("python",),
                None,
                None,
                1,
                ("planner",),
            ),
        ),
        subtask_milestones=(
            SubtaskMilestone("m1", "Callback delivery", ("callback-idempotency",), (), 1),
        ),
        subtask_dependencies=(),
        worker_item_dependencies=(
            WorkerItemDependency(
                "callback-implementer",
                "callback-implementer",
                "context",
                (),
            ),
        ),
        published_by="local:aitejiu",
        published_at=datetime(2026, 8, 22, tzinfo=UTC),
    )

    restored = TaskPlanSnapshot.from_payload(snapshot.to_payload())

    assert restored == snapshot


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda payload: {
                **payload,
                "worker_item_dependencies": [
                    {
                        **payload["worker_item_dependencies"][0],
                        "downstream_worker_item_key": "integration-checker",
                    },
                ],
            },
            "cross_subtask_worker_dependency",
        ),
        (
            lambda payload: {
                **payload,
                "subtask_dependencies": [
                    {
                        "upstream_subtask_key": "callback-idempotency",
                        "downstream_subtask_key": "integration",
                        "kind": "hard",
                        "required_evidence_refs": [],
                        "enforcement": "informational",
                    },
                    {
                        "upstream_subtask_key": "integration",
                        "downstream_subtask_key": "callback-idempotency",
                        "kind": "hard",
                        "required_evidence_refs": [],
                        "enforcement": "informational",
                    },
                ],
            },
            "cyclic_dependency",
        ),
        (
            lambda payload: {
                **payload,
                "worker_items": [
                    payload["worker_items"][0],
                    {
                        **payload["worker_items"][1],
                        "worker_item_id": payload["worker_items"][1]["worker_item_id"],
                    },
                    {
                        **payload["worker_items"][2],
                        "worker_item_id": payload["worker_items"][1]["worker_item_id"],
                    },
                ],
            },
            "duplicate_worker_item_id",
        ),
        (
            lambda payload: {
                **payload,
                "subtasks": [
                    {**payload["subtasks"][0], "parent_subtask_key": "root"},
                    payload["subtasks"][1],
                ],
            },
            "unknown_field",
        ),
    ],
)
def test_schema_v2_rejects_invalid_fixed_three_layer_shapes(
    mutate, reason: str
) -> None:
    with pytest.raises(PlanValidationError) as error:
        TaskPlanSnapshot.from_payload(mutate(plan_v2_payload()))

    assert error.value.reason == reason


def collaboration_policy_payload(
    *, overrides: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "runtime_definition_id": "61000000-0000-0000-0000-000000000001",
        "runtime_instance_id": "62000000-0000-0000-0000-000000000001",
        "runtime_interface_version": "collaboration-runtime-v1",
        "runtime_config_version": 3,
        "runtime_capability_snapshot": {
            "interface_version": "collaboration-runtime-v1",
            "projection_schema_versions": [1],
            "decision_schema_versions": [1],
            "strategy_ids": ["all_required"],
            "idempotency": "operation-id",
            "max_projection_bytes": 262_144,
            "max_decision_bytes": 65_536,
            "max_intents_per_decision": 128,
            "max_strategy_state_bytes": 16_384,
            "stability": "experimental",
        },
        "default_policy": {"enabled": True, "strategy_id": "all_required"},
        "subtask_overrides": overrides or [],
    }


def test_schema_v3_round_trips_policy_and_hashes_policy_changes() -> None:
    raw = plan_v2_payload()
    raw["schema_version"] = 3
    raw["collaboration_policy"] = collaboration_policy_payload()

    snapshot = TaskPlanSnapshot.from_payload(raw)
    assert snapshot.schema_version == 3
    assert snapshot.collaboration_policy is not None
    assert snapshot.to_payload() == raw

    changed = dict(raw)
    changed["collaboration_policy"] = collaboration_policy_payload(
        overrides=[
            {
                "subtask_key": "integration",
                "enabled": False,
                "strategy_id": "all_required",
            }
        ]
    )
    changed_snapshot = TaskPlanSnapshot.from_payload(changed)
    assert plan_snapshot_content_hash(changed_snapshot) != plan_snapshot_content_hash(snapshot)


def test_schema_v3_requires_explicit_policy_and_keeps_it_strict() -> None:
    missing = plan_v2_payload()
    missing["schema_version"] = 3
    with pytest.raises(PlanValidationError, match="missing field: collaboration_policy"):
        TaskPlanSnapshot.from_payload(missing)

    unknown = plan_v2_payload()
    unknown["schema_version"] = 3
    policy = collaboration_policy_payload()
    policy["runtime_instance_id"] = "forbidden-planner-field"
    unknown["collaboration_policy"] = policy
    with pytest.raises(ValidationError, match="runtime_instance_id"):
        TaskPlanSnapshot.from_payload(unknown)
