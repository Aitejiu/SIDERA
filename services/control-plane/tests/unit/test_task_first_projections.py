from datetime import UTC, datetime
from uuid import UUID

import pytest

from sidera_control_plane.application.task_first_queries import (
    BindingFact,
    EvidenceFact,
    ExecutionFact,
    LeaseFact,
    SessionFact,
    TaskFact,
    TaskFirstProjectionService,
    WorkerFact,
)
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
from sidera_control_plane.domain.projections import TaskNodeState

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
DONE_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")
WAITING_TASK_ID = UUID("30000000-0000-0000-0000-000000000003")
RUNNING_TASK_ID = UUID("30000000-0000-0000-0000-000000000004")
WORKER_ID = UUID("60000000-0000-0000-0000-000000000001")
EXECUTION_ID = UUID("70000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("80000000-0000-0000-0000-000000000001")
LEASE_ID = UUID("90000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def work_item(
    key: str,
    task_id: UUID | None,
    *,
    milestone_key: str = "m1",
) -> WorkItem:
    return WorkItem(
        key=key,
        task_id=task_id,
        kind=WorkItemKind.CHILD,
        title=key.title(),
        goal_summary=f"Complete {key}",
        acceptance_refs=(f"acceptance:{key}",),
        milestone_key=milestone_key,
        worker_slot_key="backend",
        expected_outputs=(f"output:{key}",),
        source_refs=(f"source:{key}",),
    )


def make_snapshot() -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000001"),
        schema_version=1,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=1,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="projection:test",
        title="Projection test",
        goal_summary="Compose a Task First graph.",
        acceptance_refs=("acceptance:root",),
        work_items=(
            work_item("done", DONE_TASK_ID),
            work_item("waiting", WAITING_TASK_ID),
            work_item("running", RUNNING_TASK_ID),
            work_item("planned", None, milestone_key="m2"),
        ),
        worker_slots=(
            WorkerSlot(
                key="backend",
                role="backend",
                capability_requirements=("python",),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=1,
            ),
        ),
        milestones=(
            Milestone(
                key="m1",
                title="Implementation",
                work_item_keys=("done", "waiting", "running"),
                acceptance_refs=(),
                order_hint=1,
            ),
            Milestone(
                key="m2",
                title="Planned",
                work_item_keys=("planned",),
                acceptance_refs=(),
                order_hint=2,
            ),
        ),
        dependencies=(
            Dependency(
                upstream_work_item_key="running",
                downstream_work_item_key="waiting",
                kind=DependencyKind.INFORMATIONAL,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.INFORMATIONAL,
            ),
        ),
        published_by="local:test",
        published_at=NOW,
    )


def task_fact(task_id: UUID, status: str) -> TaskFact:
    return TaskFact(
        id=task_id,
        workspace_id=WORKSPACE_ID,
        title=task_id.hex[:6],
        acceptance_criteria=("acceptance",),
        status=status,
        version=1,
        updated_at=NOW,
    )


def empty_facts() -> dict[str, dict[UUID, object]]:
    return {
        "executions": {},
        "leases": {},
        "bindings": {},
        "sessions": {},
        "workers": {},
        "evidence": {},
        "activity": {},
    }


def compose(
    *,
    tasks: dict[UUID, TaskFact],
    executions: dict[UUID, ExecutionFact] | None = None,
    leases: dict[UUID, LeaseFact] | None = None,
    bindings: dict[UUID, BindingFact] | None = None,
    sessions: dict[UUID, SessionFact] | None = None,
    workers: dict[UUID, WorkerFact] | None = None,
    evidence: dict[UUID, tuple[EvidenceFact, ...]] | None = None,
):
    facts = empty_facts()
    facts["executions"] = executions or {}
    facts["leases"] = leases or {}
    facts["bindings"] = bindings or {}
    facts["sessions"] = sessions or {}
    facts["workers"] = workers or {}
    facts["evidence"] = evidence or {}
    facts["activity"] = {}
    return TaskFirstProjectionService().compose(
        make_snapshot(),
        tasks=tasks,
        executions=facts["executions"],
        leases=facts["leases"],
        bindings=facts["bindings"],
        sessions=facts["sessions"],
        workers=facts["workers"],
        evidence=facts["evidence"],
        activity={},
        last_change_cursor=7,
        as_of=NOW,
    )


def test_planned_materialized_and_dependency_overlay_use_real_facts() -> None:
    projection = compose(
        tasks={
            DONE_TASK_ID: task_fact(DONE_TASK_ID, "succeeded"),
            WAITING_TASK_ID: task_fact(WAITING_TASK_ID, "pending"),
            RUNNING_TASK_ID: task_fact(RUNNING_TASK_ID, "ready"),
        }
    )

    nodes = {node.work_item_key: node for node in projection.nodes}
    assert nodes["planned"].state is TaskNodeState.PLANNED
    assert nodes["waiting"].state is TaskNodeState.WAITING_DEPENDENCY
    assert nodes["waiting"].waiting_on_plan_dependency is True
    assert nodes["waiting"].scheduler_enforced is False
    assert nodes["running"].state is TaskNodeState.READY
    assert nodes["done"].state is TaskNodeState.SUCCEEDED
    assert projection.overview.materialized_count == 3
    assert projection.overview.planned_count == 1
    assert projection.overview.completed_count == 1
    assert projection.overview.next_work_item_keys == ("waiting", "running", "planned")
    assert tuple(node.work_item_key for node in projection.nodes) == (
        "done",
        "waiting",
        "running",
        "planned",
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("queued", TaskNodeState.QUEUED),
        ("leased", TaskNodeState.ASSIGNED),
        ("running", TaskNodeState.RUNNING),
        ("succeeded", TaskNodeState.SUCCEEDED),
        ("failed", TaskNodeState.FAILED),
        ("canceled", TaskNodeState.CANCELED),
        ("lost", TaskNodeState.RETRYING),
    ],
)
def test_execution_statuses_override_task_display_state(
    status: str, expected: TaskNodeState
) -> None:
    execution = ExecutionFact(
        id=EXECUTION_ID,
        task_id=RUNNING_TASK_ID,
        status=status,
        attempt=2,
        session_id=SESSION_ID if status == "running" else None,
        assigned_worker_id=WORKER_ID if status == "leased" else None,
        started_at=NOW if status == "running" else None,
        finished_at=NOW if status in {"succeeded", "failed", "canceled"} else None,
        updated_at=NOW,
        capability_requirements=("python",),
    )
    projection = compose(
        tasks={
            DONE_TASK_ID: task_fact(DONE_TASK_ID, "succeeded"),
            WAITING_TASK_ID: task_fact(WAITING_TASK_ID, "pending"),
            RUNNING_TASK_ID: task_fact(RUNNING_TASK_ID, "ready"),
        },
        executions={RUNNING_TASK_ID: execution},
        leases={
            EXECUTION_ID: LeaseFact(
                id=LEASE_ID,
                execution_id=EXECUTION_ID,
                status="active",
                worker_id=WORKER_ID,
                version=1,
            )
        }
        if status == "leased"
        else {},
        bindings={
            EXECUTION_ID: BindingFact(
                execution_id=EXECUTION_ID,
                worker_id=WORKER_ID,
                version=1,
            )
        }
        if status == "leased"
        else {},
        sessions={
            SESSION_ID: SessionFact(
                id=SESSION_ID,
                execution_id=EXECUTION_ID,
                worker_id=WORKER_ID,
                status="active",
                last_activity_at=NOW,
                version=1,
            )
        }
        if status == "running"
        else {},
        workers={
            WORKER_ID: WorkerFact(
                id=WORKER_ID,
                display_name="API worker",
                capabilities=("python",),
            )
        },
    )

    node = next(node for node in projection.nodes if node.task_id == RUNNING_TASK_ID)
    assert node.state is expected
    if status == "leased":
        assert node.worker_id == WORKER_ID
        assert node.scheduler_enforced is True
    if status == "running":
        assert node.session_id == SESSION_ID


def test_evidence_and_activity_are_projected_without_percentage_fields() -> None:
    projection = compose(
        tasks={
            DONE_TASK_ID: task_fact(DONE_TASK_ID, "succeeded"),
            WAITING_TASK_ID: task_fact(WAITING_TASK_ID, "pending"),
            RUNNING_TASK_ID: task_fact(RUNNING_TASK_ID, "ready"),
        },
        evidence={
            DONE_TASK_ID: (
                EvidenceFact(
                    id=UUID("a0000000-0000-0000-0000-000000000001"),
                    task_id=DONE_TASK_ID,
                    acceptance_ref="acceptance:done",
                    created_at=NOW,
                ),
            )
        },
    )

    done = next(node for node in projection.nodes if node.task_id == DONE_TASK_ID)
    assert done.evidence_satisfied == 1
    assert done.evidence_required == 1
    assert not hasattr(done, "progress_percent")
