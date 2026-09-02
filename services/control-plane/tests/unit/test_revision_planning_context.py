from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from tests.unit.test_plan_revisions import profile_snapshot

from sidera_control_plane.application.planner_runtime import (
    CapabilityCatalogSnapshot,
    PlannerInputAssembler,
    PlannerPolicyConstraints,
)
from sidera_control_plane.application.revision_planning_context import (
    AdapterCapabilitySnapshotV1,
    ConfirmedContextPackageRefV1,
    RevisionPlanningContextBuilder,
    RevisionPlanningContextV1,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
)
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.planning import (
    PlanningBriefRevision,
    PlanningRequest,
    PlanningRequestKind,
)
from sidera_control_plane.domain.plans import (
    DependencyEnforcement,
    DependencyKind,
    SubtaskDependency,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker_item import WorkerItem

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
BASE_SNAPSHOT_ID = UUID("10000000-0000-0000-0000-000000000001")
SUBTASK_A_ID = UUID("50000000-0000-0000-0000-000000000001")
SUBTASK_B_ID = UUID("50000000-0000-0000-0000-000000000002")
WORKER_A_ID = UUID("40000000-0000-0000-0000-000000000001")
WORKER_B_ID = UUID("40000000-0000-0000-0000-000000000002")
EXECUTION_ID = UUID("60000000-0000-0000-0000-000000000001")
LINEAGE_ID = UUID("60000000-0000-0000-0000-000000000002")
SESSION_ID = UUID("60000000-0000-0000-0000-000000000003")
SESSION_WORKER_ID = UUID("60000000-0000-0000-0000-000000000004")


def _snapshot() -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=BASE_SNAPSHOT_ID,
        schema_version=2,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=4,
        parent_snapshot_id=None,
        source_type="proposal",
        source_ref="proposal://base",
        title="Build the control plane",
        goal_summary="Keep a running plan reproducible",
        acceptance_refs=("acceptance:plan",),
        published_by="planner",
        published_at=NOW,
        subtasks=(
            SubtaskPlanItem(
                key="verify",
                task_id=SUBTASK_B_ID,
                kind=WorkItemKind.CHILD,
                title="Verify",
                goal_summary="Verify the result",
                acceptance_refs=("acceptance:verify",),
                milestone_key=None,
                expected_outputs=("evidence_type:test",),
                source_refs=("source:verify",),
            ),
            SubtaskPlanItem(
                key="implement",
                task_id=SUBTASK_A_ID,
                kind=WorkItemKind.CHILD,
                title="Implement",
                goal_summary="Implement the change",
                acceptance_refs=("acceptance:implement",),
                milestone_key=None,
                expected_outputs=("artifact_type:code",),
                source_refs=("source:implement",),
            ),
        ),
        worker_item_plans=(
            WorkerItemPlanItem(
                key="verify-worker",
                worker_item_id=WORKER_B_ID,
                subtask_key="verify",
                title="Verification worker",
                role="tester",
                responsibility="Run the verification suite",
                capability_requirements=("test",),
                worker_policy_ref="policy:test",
                configuration_ref="config:test",
                concurrency_limit=1,
                source_refs=("source:verify-worker",),
            ),
            WorkerItemPlanItem(
                key="implement-worker",
                worker_item_id=WORKER_A_ID,
                subtask_key="implement",
                title="Implementation worker",
                role="developer",
                responsibility="Implement the requested change",
                capability_requirements=("code",),
                worker_policy_ref="policy:code",
                configuration_ref="config:code",
                concurrency_limit=1,
                source_refs=("source:implement-worker",),
            ),
        ),
        subtask_milestones=(),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="implement",
                downstream_subtask_key="verify",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
        ),
        worker_item_dependencies=(),
    )


def _task(task_id: UUID, title: str) -> Task:
    return Task.create(
        task_id=task_id,
        workspace_id=WORKSPACE_ID,
        title=title,
        description=f"Description for {title}",
        acceptance_criteria=(f"acceptance:{title.lower()}",),
        priority=50,
        requested_by="operator",
    )


def _worker(item_id: UUID, stable_key: str) -> WorkerItem:
    return WorkerItem.create(
        item_id=item_id,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        stable_key=stable_key,
        title=stable_key,
        responsibility=f"Responsibility for {stable_key}",
        now=NOW,
    )


def _execution(worker_item_id: UUID, task_id: UUID) -> Execution:
    return Execution.create(
        execution_id=EXECUTION_ID,
        workspace_id=WORKSPACE_ID,
        task_id=task_id,
        worker_item_id=worker_item_id,
        lineage_id=LINEAGE_ID,
        attempt=1,
        max_attempts=2,
        purpose="execute worker item",
        capability_requirements=frozenset({"code"}),
        pinned_worker_id=None,
        not_before=NOW,
    )


def _session(execution: Execution) -> ExecutionSession:
    return ExecutionSession.start(
        session_id=SESSION_ID,
        workspace_id=WORKSPACE_ID,
        execution_id=execution.id,
        worker_id=SESSION_WORKER_ID,
        adapter_session_id="provider-private-session",
        now=NOW,
        capability_snapshot=WorkerAdapterCapabilityProfile(
            input=CapabilityInput.QUEUED,
            idempotency=IdempotencyCapability.OPERATION_ID,
            resume=ResumeCapability.SAME_PROCESS,
            stop=StopCapability.PROCESS,
            sandbox=SandboxCapability.FILESYSTEM,
            stability=StabilityLevel.STABLE,
            context_package_versions=(1,),
        ),
        process_ref="provider-process-secret",
    )


def _request(snapshot: TaskPlanSnapshot) -> PlanningRequest:
    base = {
        "schema_version": 1,
        "workspace_id": WORKSPACE_ID,
        "root_task_id": ROOT_TASK_ID,
        "plan_snapshot_id": snapshot.id,
        "plan_generation": snapshot.generation,
        "plan_content_hash": plan_snapshot_content_hash(snapshot),
        "root_task_version": 7,
        "captured_at": NOW,
    }
    from sidera_control_plane.domain.plan_revisions import RevisionBaseDescriptorV1

    return PlanningRequest.create_revision(
        request_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        profile_snapshot=profile_snapshot(),
        objective="Revise the running plan",
        constraints=("Keep completed work auditable",),
        acceptance_criteria=("The revision is reviewable",),
        context_refs=("context-package://confirmed",),
        requested_outputs=("revision plan",),
        source_type="web",
        source_ref="conversation://revision",
        actor="operator",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
        base=RevisionBaseDescriptorV1(**base),
    )


def _adapter_capabilities() -> tuple[AdapterCapabilitySnapshotV1, ...]:
    return (
        AdapterCapabilitySnapshotV1(
            adapter_ref="opencode:worker",
            interface_version="sidera.worker.v1",
            config_version=2,
            capabilities=("queued_input", "operation_idempotency"),
        ),
    )


def _build_context(*, max_context_bytes: int | None = None):
    snapshot = _snapshot()
    implement_worker = _worker(WORKER_A_ID, "implement-worker")
    implement_worker.advance_context(now=NOW)
    implement_worker.advance_context(now=NOW)
    implement_worker.acknowledge_context(expected_context_version=3, now=NOW)
    verify_worker = _worker(WORKER_B_ID, "verify-worker")
    execution = _execution(WORKER_A_ID, SUBTASK_A_ID)
    builder = RevisionPlanningContextBuilder(
        max_context_bytes=max_context_bytes,
    )
    return builder.build(
        request=_request(snapshot),
        snapshot=snapshot,
        tasks=(_task(SUBTASK_B_ID, "Verify"), _task(ROOT_TASK_ID, "Root"), _task(SUBTASK_A_ID, "Implement")),
        worker_items=(verify_worker, implement_worker),
        executions=(execution,),
        sessions=(_session(execution),),
        context_package_refs=(
            ConfirmedContextPackageRefV1(
                ref="context-package://confirmed",
                package_hash="a" * 64,
                context_version=3,
            ),
        ),
        adapter_capabilities=_adapter_capabilities(),
    )


def test_revision_context_contains_exact_base_and_stable_sanitized_facts() -> None:
    context = _build_context()
    mapping = context.to_mapping()
    serialized = json.dumps(mapping, sort_keys=True)

    assert context.base_descriptor.plan_generation == 4
    assert context.base_descriptor.plan_content_hash == plan_snapshot_content_hash(_snapshot())
    assert [item.plan_key for item in context.tasks] == ["implement", "root", "verify"]
    assert [item.stable_key for item in context.worker_items] == [
        "implement-worker",
        "verify-worker",
    ]
    assert context.worker_items[0].acknowledged_context_version == 3
    assert context.executions[0].session_id == context.sessions[0].session_id
    plan_snapshot = mapping["plan_snapshot"]
    assert isinstance(plan_snapshot, dict)
    subtask_dependencies = plan_snapshot["subtask_dependencies"]
    assert isinstance(subtask_dependencies, list)
    assert isinstance(subtask_dependencies[0], dict)
    assert subtask_dependencies[0]["downstream_subtask_key"] == "verify"
    assert "provider_process_ref" not in serialized
    assert "process_ref" not in serialized
    assert "provider-private-session" not in serialized
    assert "provider-process-secret" not in serialized
    assert "trace" not in serialized
    assert context.byte_size == len(context.canonical_bytes())


def test_revision_context_is_identical_when_inputs_are_reordered() -> None:
    first = _build_context()
    second = _build_context()

    assert first.canonical_bytes() == second.canonical_bytes()


def test_revision_context_rejects_a_snapshot_that_does_not_match_the_base_hash() -> None:
    snapshot = _snapshot()
    request = _request(snapshot)
    changed = TaskPlanSnapshot.create(
        snapshot_id=snapshot.id,
        schema_version=snapshot.schema_version,
        workspace_id=snapshot.workspace_id,
        root_task_id=snapshot.root_task_id,
        generation=snapshot.generation,
        parent_snapshot_id=snapshot.parent_snapshot_id,
        source_type=snapshot.source_type,
        source_ref=snapshot.source_ref,
        title="Changed title",
        goal_summary=snapshot.goal_summary,
        acceptance_refs=snapshot.acceptance_refs,
        published_by=snapshot.published_by,
        published_at=snapshot.published_at,
        subtasks=snapshot.subtasks,
        worker_item_plans=snapshot.worker_item_plans,
        subtask_milestones=snapshot.subtask_milestones,
        subtask_dependencies=snapshot.subtask_dependencies,
        worker_item_dependencies=snapshot.worker_item_dependencies,
    )

    with pytest.raises(ValidationError, match="base.*hash"):
        RevisionPlanningContextBuilder().build(
            request=request,
            snapshot=changed,
            tasks=(),
            worker_items=(),
            executions=(),
            sessions=(),
            context_package_refs=(),
            adapter_capabilities=(),
        )


def test_revision_context_enforces_the_byte_budget() -> None:
    with pytest.raises(ValidationError, match="budget"):
        _build_context(max_context_bytes=1)


def test_planner_input_discriminates_a_revision_context() -> None:
    context = _build_context()
    profile = profile_snapshot()
    request_id = uuid4()
    brief = PlanningBriefRevision.create(
        planning_request_id=request_id,
        revision=1,
        objective="Revise the running plan",
        constraints=(),
        acceptance_criteria=("The revision is reviewable",),
        context_refs=(),
        requested_outputs=("revision plan",),
        user_feedback=None,
        source_correlation=uuid4(),
        created_by="operator",
        created_at=NOW,
    )
    planner_input = PlannerInputAssembler().assemble(
        brief=brief,
        profile_snapshot=profile,
        capability_catalog=CapabilityCatalogSnapshot(
            snapshot_version=1,
            captured_at=NOW,
            capabilities=(),
            adapter_preferences=(),
            budget_level="standard",
            max_concurrency=1,
            workspace_providers=(),
        ),
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(profile),
        operation_id=uuid4(),
        planning_request_id=request_id,
        planning_run_id=uuid4(),
        planning_context=context,
    )

    assert planner_input.kind is PlanningRequestKind.REVISION
    assert isinstance(planner_input.planning_context, RevisionPlanningContextV1)
