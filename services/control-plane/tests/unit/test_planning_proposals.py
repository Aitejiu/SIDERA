from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    IdempotencyCapability,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.planning import PlanningRequestKind
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.planning_proposals import (
    ActivationDependency,
    DecisionSummary,
    ExecutionIntent,
    ImpactPolicy,
    PlanningProfileDescriptor,
    PlanningProposalPayload,
    ProposalRevision,
    ProposedRootTask,
    ProposedSubtask,
    ProposedWorkerItem,
    canonical_proposal_bytes,
    derive_proposal_diff,
    proposal_content_hash,
)
from sidera_control_plane.domain.plans import (
    DependencyEnforcement,
    DependencyKind,
    SubtaskDependency,
    SubtaskMilestone,
    WorkerItemDependency,
    WorkItemKind,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def planner_capabilities() -> PlannerAdapterCapabilityProfile:
    return PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.STABLE,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=(2,),
    )


def profile_descriptor() -> PlanningProfileDescriptor:
    return PlanningProfileDescriptor(
        profile_id=uuid4(),
        profile_version=4,
        planner_module_instance_id=uuid4(),
        planner_module_config_version=2,
    )


def planning_profile_snapshot():
    profile = PlanningProfile(
        id=uuid4(),
        workspace_id=uuid4(),
        profile_key="default",
        version=4,
        display_name="Default planner",
        planner_module_instance_id=uuid4(),
        planner_module_config_version=2,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.v1",
        system_policy={},
        proposal_schema_version=2,
        allowed_capabilities=("code", "test", "docs"),
        adapter_preferences=("opencode",),
        adapter_forbidden=("unsafe",),
        budget=PlanningBudgetPolicy(
            max_context_bytes=64_000,
            max_tokens=100_000,
            max_cost_microunits=1_000_000,
            max_runtime_seconds=600,
            max_revisions=3,
            max_nodes=20,
            max_concurrency=4,
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=1,
            backoff_seconds=(5,),
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
            invalid_output_policy=InvalidOutputPolicy.RETRY,
        ),
        default_priority=50,
        execution_max_attempts=3,
        execution_budget_level="standard",
        user_selectable=True,
        status=PlanningProfileStatus.ACTIVE,
        planner_secret_refs=("secret://planner/key",),
    )
    return profile.snapshot(planner_capabilities())


def payload() -> PlanningProposalPayload:
    summary = DecisionSummary(
        understanding="Split the delivery into parallel implementation and verification work.",
        assumptions=("The repository checkout is available.",),
        risks=("Integration may expose contract drift.",),
        estimated_cost_microunits=10_000,
        estimated_runtime_seconds=120,
        estimated_concurrency=2,
        scope_includes=("control plane", "web",),
        scope_excludes=("production rollout",),
        metadata={"z": [1, 2], "a": {"enabled": True}},
    )
    subtasks = (
        ProposedSubtask(
            key="design",
            kind=WorkItemKind.CHILD,
            title="Design contracts",
            goal_summary="Define the control-plane contract.",
            acceptance_refs=("acceptance:contracts",),
            expected_outputs=("contract",),
            milestone_key="m1",
            source_refs=("brief:1",),
        ),
        ProposedSubtask(
            key="implement",
            kind=WorkItemKind.CHILD,
            title="Implement services",
            goal_summary="Implement the backend and frontend pieces.",
            acceptance_refs=("acceptance:implementation",),
            expected_outputs=("code",),
            milestone_key="m1",
            source_refs=("brief:1",),
        ),
        ProposedSubtask(
            key="verify",
            kind=WorkItemKind.CHILD,
            title="Verify delivery",
            goal_summary="Run the integrated checks.",
            acceptance_refs=("acceptance:verification",),
            expected_outputs=("report",),
            milestone_key="m2",
            source_refs=("brief:1",),
        ),
    )
    workers = (
        ProposedWorkerItem(
            key="contract-worker",
            subtask_key="design",
            title="Contract worker",
            role="architect",
            responsibility="Define stable contracts.",
            capability_requirements=("docs",),
            worker_policy_ref="policy:default",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("brief:1",),
        ),
        ProposedWorkerItem(
            key="backend-worker",
            subtask_key="implement",
            title="Backend worker",
            role="backend",
            responsibility="Implement backend behavior.",
            capability_requirements=("code",),
            worker_policy_ref="policy:default",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("brief:1",),
        ),
        ProposedWorkerItem(
            key="web-worker",
            subtask_key="implement",
            title="Web worker",
            role="frontend",
            responsibility="Implement the web surface.",
            capability_requirements=("code",),
            worker_policy_ref="policy:default",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("brief:1",),
        ),
        ProposedWorkerItem(
            key="verify-worker",
            subtask_key="verify",
            title="Verification worker",
            role="qa",
            responsibility="Verify the integrated result.",
            capability_requirements=("test",),
            worker_policy_ref="policy:default",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("brief:1",),
        ),
    )
    subtask_dependencies = (
        SubtaskDependency(
            upstream_subtask_key="design",
            downstream_subtask_key="implement",
            kind=DependencyKind.HARD,
            required_evidence_refs=("acceptance:contracts",),
            enforcement=DependencyEnforcement.CONTROL_PLANE,
        ),
        SubtaskDependency(
            upstream_subtask_key="design",
            downstream_subtask_key="verify",
            kind=DependencyKind.HARD,
            required_evidence_refs=(),
            enforcement=DependencyEnforcement.CONTROL_PLANE,
        ),
        SubtaskDependency(
            upstream_subtask_key="implement",
            downstream_subtask_key="verify",
            kind=DependencyKind.HARD,
            required_evidence_refs=("acceptance:implementation",),
            enforcement=DependencyEnforcement.CONTROL_PLANE,
        ),
    )
    worker_dependencies = (
        WorkerItemDependency(
            upstream_worker_item_key="contract-worker",
            downstream_worker_item_key="backend-worker",
            kind="hard",
            required_artifact_refs=("artifact_type:report",),
        ),
        WorkerItemDependency(
            upstream_worker_item_key="contract-worker",
            downstream_worker_item_key="web-worker",
            kind="hard",
            required_artifact_refs=(),
        ),
        WorkerItemDependency(
            upstream_worker_item_key="backend-worker",
            downstream_worker_item_key="verify-worker",
            kind="hard",
            required_artifact_refs=("artifact_type:diff",),
        ),
        WorkerItemDependency(
            upstream_worker_item_key="web-worker",
            downstream_worker_item_key="verify-worker",
            kind="hard",
            required_artifact_refs=(),
        ),
        WorkerItemDependency(
            upstream_worker_item_key="contract-worker",
            downstream_worker_item_key="verify-worker",
            kind="informational",
            required_artifact_refs=(),
        ),
    )
    intents = tuple(
        ExecutionIntent(
            worker_item_key=worker.key,
            purpose=worker.responsibility,
            context_refs=("context:brief",),
            expected_artifact_refs=("artifact_type:diff",),
            required_evidence_refs=("acceptance:contracts",),
            capability_requirements=worker.capability_requirements,
            adapter_preferences=("opencode",),
            budget_class="standard",
            deadline_seconds=300,
            not_before_seconds=None,
            max_attempts=2,
            activation_dependencies=(
                (
                    ActivationDependency(
                        upstream_worker_item_key="contract-worker",
                        downstream_worker_item_key=worker.key,
                        kind="informational",
                        required_artifact_refs=(),
                    ),
                )
                if worker.key != "contract-worker"
                else ()
            ),
        )
        for worker in workers
    )
    return PlanningProposalPayload(
        schema_version=2,
        kind=PlanningRequestKind.CREATE,
        root_task=ProposedRootTask(
            title="Planning control loop",
            goal_summary="Deliver the planning control loop.",
            acceptance_refs=("acceptance:contracts", "acceptance:verification"),
        ),
        subtasks=subtasks,
        worker_items=workers,
        subtask_milestones=(
            SubtaskMilestone(
                key="m1",
                title="Implementation",
                subtask_keys=("design", "implement"),
                acceptance_refs=("acceptance:contracts",),
                order_hint=1,
            ),
            SubtaskMilestone(
                key="m2",
                title="Verification",
                subtask_keys=("verify",),
                acceptance_refs=("acceptance:verification",),
                order_hint=2,
            ),
        ),
        subtask_dependencies=subtask_dependencies,
        worker_item_dependencies=worker_dependencies,
        execution_intents=intents,
        decision_summary=summary,
        impact_policy=ImpactPolicy(
            activate_immediately=True,
            budget_level="standard",
            failure_policy="retry_then_block",
        ),
        base_descriptor=None,
    )


def test_canonical_hash_is_stable_and_includes_decision_inputs() -> None:
    item = payload()
    descriptor = profile_descriptor()
    original = proposal_content_hash(item, brief_revision=1, profile_descriptor=descriptor)
    reordered_metadata = {"a": {"enabled": True}, "z": [1, 2]}
    reordered = replace(item, decision_summary=replace(item.decision_summary, metadata=reordered_metadata))
    assert proposal_content_hash(
        reordered, brief_revision=1, profile_descriptor=descriptor
    ) == original
    assert canonical_proposal_bytes(item, brief_revision=1, profile_descriptor=descriptor).startswith(b"{")
    assert proposal_content_hash(
        replace(item, decision_summary=replace(item.decision_summary, understanding="changed")),
        brief_revision=1,
        profile_descriptor=descriptor,
    ) != original
    assert proposal_content_hash(
        replace(
            item,
            execution_intents=(
                replace(item.execution_intents[0], purpose="changed"),
                *item.execution_intents[1:],
            ),
        ),
        brief_revision=1,
        profile_descriptor=descriptor,
    ) != original
    assert proposal_content_hash(
        replace(
            item,
            impact_policy=replace(item.impact_policy, failure_policy="block"),
        ),
        brief_revision=1,
        profile_descriptor=descriptor,
    ) != original
    assert proposal_content_hash(item, brief_revision=2, profile_descriptor=descriptor) != original
    assert proposal_content_hash(
        item,
        brief_revision=1,
        profile_descriptor=replace(descriptor, profile_version=5),
    ) != original
    derived_diff = derive_proposal_diff(item)
    assert derived_diff is not None  # derived/UI projection is outside the payload hash
    assert proposal_content_hash(
        item,
        brief_revision=1,
        profile_descriptor=descriptor,
    ) == original


def test_canonical_serializer_rejects_untyped_payloads_and_non_finite_values() -> None:
    with pytest.raises(ValidationError, match="typed"):
        canonical_proposal_bytes(
            {"schema_version": 2},  # type: ignore[arg-type]
            brief_revision=1,
            profile_descriptor=profile_descriptor(),
        )
    with pytest.raises(ValidationError, match="integer"):
        DecisionSummary(
            understanding="understanding",
            assumptions=(),
            risks=(),
            estimated_cost_microunits=1.5,  # type: ignore[arg-type]
            estimated_runtime_seconds=1,
            estimated_concurrency=1,
            scope_includes=("scope",),
            scope_excludes=(),
            metadata={},
        )


def test_proposal_mapping_accepts_lists_but_normalizes_to_tuple_dto() -> None:
    item = payload()

    assert PlanningProposalPayload.from_mapping(item.to_mapping()) == item


def test_create_diff_marks_added_nodes_and_hard_dependency_blockers() -> None:
    diff = derive_proposal_diff(payload())

    assert diff.root_task_added is True
    assert diff.added_subtask_keys == ("design", "implement", "verify")
    assert len(diff.added_worker_item_keys) == 4
    assert diff.immediately_ready_worker_item_keys == ("contract-worker",)
    assert diff.blocked_worker_item_keys == (
        "backend-worker",
        "web-worker",
        "verify-worker",
    )
    assert diff.added_worker_item_dependency_count == 5


def test_proposal_revision_derives_diff_and_never_accepts_planner_diff() -> None:
    item = payload()
    revision = ProposalRevision.create(
        planning_request_id=uuid4(),
        revision=1,
        brief_revision=1,
        planning_run_id=uuid4(),
        kind=PlanningRequestKind.CREATE,
        payload=item,
        profile_descriptor=profile_descriptor(),
        profile_snapshot=planning_profile_snapshot(),
        created_at=NOW,
    )

    assert revision.derived_diff == derive_proposal_diff(item)
    assert len(revision.content_hash) == 64
    with pytest.raises(ValidationError, match="derived"):
        PlanningProposalPayload.from_mapping({**item.to_mapping(), "derived_diff": {}})
