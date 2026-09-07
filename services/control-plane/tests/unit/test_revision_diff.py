from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from sidera_control_plane.domain.collaboration import (
    CollaborationPolicyRequestV1,
    CollaborationPolicyRequestV2,
    CollaborationPolicySnapshotV1,
    CollaborationPolicySnapshotV2,
    CollaborationRuntimeBindingSnapshot,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.modules import (
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    StabilityLevel,
)
from sidera_control_plane.domain.plan_revisions import (
    ContextDisposition,
    IdentityStrategy,
    RevisionBaseDescriptorV1,
    SubtaskIdentityDecision,
    WorkerItemIdentityDecision,
    WorkerTransitionAction,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.planning import PlanningRequestKind
from sidera_control_plane.domain.planning_proposals import (
    DecisionSummary,
    ExecutionIntent,
    ImpactPolicy,
    ProposedRootTask,
    ProposedSubtask,
    ProposedWorkerItem,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
    PlanningRevisionProposalPayloadV4,
    PlanningRevisionProposalPayloadV6,
)
from sidera_control_plane.domain.plans import (
    SubtaskMilestone,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.domain.result_integration import IntegrationSpecV1
from sidera_control_plane.domain.revision_diff import (
    NormalizedRevisionDiff,
    RevisionDiffNormalizer,
)
from tests.unit.result_input_fixtures import revision_v5_mapping
from tests.unit.result_integration_fixtures import (
    integration_mapping,
    revision_v6_mapping,
)
from tests.unit.test_collaboration_policy_v2 import runtime_binding

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
SUBTASK_ID = UUID("50000000-0000-0000-0000-000000000001")
WORKER_ID = UUID("40000000-0000-0000-0000-000000000001")
SNAPSHOT_ID = UUID("10000000-0000-0000-0000-000000000001")


def base_snapshot() -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=SNAPSHOT_ID,
        schema_version=2,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=4,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="fixture",
        title="Running control room",
        goal_summary="Implement the current behavior.",
        acceptance_refs=("acceptance:root",),
        published_by="test",
        published_at=NOW,
        subtasks=(
            SubtaskPlanItem(
                key="implementation",
                task_id=SUBTASK_ID,
                kind=WorkItemKind.CHILD,
                title="Implementation",
                goal_summary="Implement the current behavior.",
                acceptance_refs=("acceptance:implementation",),
                milestone_key="m1",
                expected_outputs=("artifact:implementation",),
                source_refs=("brief:initial",),
            ),
        ),
        worker_item_plans=(
            WorkerItemPlanItem(
                key="backend",
                worker_item_id=WORKER_ID,
                subtask_key="implementation",
                title="Backend worker",
                role="backend",
                responsibility="Implement the current behavior.",
                capability_requirements=("python",),
                worker_policy_ref="policy:default",
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=("brief:initial",),
            ),
        ),
        subtask_milestones=(
            SubtaskMilestone(
                key="m1",
                title="Implementation",
                subtask_keys=("implementation",),
                acceptance_refs=("acceptance:implementation",),
                order_hint=1,
            ),
        ),
    )


def revision_payload(snapshot: TaskPlanSnapshot) -> PlanningRevisionProposalPayloadV3:
    return PlanningRevisionProposalPayloadV3(
        schema_version=3,
        kind=PlanningRequestKind.REVISION,
        base_descriptor=RevisionBaseDescriptorV1(
            schema_version=1,
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
            plan_snapshot_id=SNAPSHOT_ID,
            plan_generation=4,
            plan_content_hash=plan_snapshot_content_hash(snapshot),
            root_task_version=7,
            captured_at=NOW,
        ),
        root_task=ProposedRootTask(
            title="Running control room",
            goal_summary="Implement the revised behavior.",
            acceptance_refs=("acceptance:root",),
        ),
        subtasks=(
            ProposedSubtask(
                key="implementation",
                kind=WorkItemKind.CHILD,
                title="Implementation",
                goal_summary="Implement the revised behavior.",
                acceptance_refs=("acceptance:implementation",),
                expected_outputs=("artifact:implementation",),
                milestone_key="m1",
                source_refs=("brief:revision",),
            ),
        ),
        worker_items=(
            ProposedWorkerItem(
                key="backend",
                subtask_key="implementation",
                title="Backend worker",
                role="backend",
                responsibility="Implement the revised behavior.",
                capability_requirements=("python",),
                worker_policy_ref="policy:default",
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=("brief:revision",),
            ),
        ),
        subtask_milestones=(
            SubtaskMilestone(
                key="m1",
                title="Implementation",
                subtask_keys=("implementation",),
                acceptance_refs=("acceptance:implementation",),
                order_hint=1,
            ),
        ),
        subtask_dependencies=(),
        worker_item_dependencies=(),
        execution_intents=(
            ExecutionIntent(
                worker_item_key="backend",
                purpose="Implement the revised behavior.",
                context_refs=("artifact://current-context",),
                expected_artifact_refs=("artifact:implementation",),
                required_evidence_refs=("evidence:implementation",),
                capability_requirements=("python",),
                adapter_preferences=("opencode",),
                budget_class="standard",
                deadline_seconds=None,
                not_before_seconds=None,
                max_attempts=2,
                activation_dependencies=(),
            ),
        ),
        subtask_identity_decisions=(
            SubtaskIdentityDecision(
                target_subtask_key="implementation",
                strategy=IdentityStrategy.REUSE,
                existing_subtask_task_id=SUBTASK_ID,
                derived_from_subtask_task_ids=(),
                reason="same subtask",
            ),
        ),
        worker_item_identity_decisions=(
            WorkerItemIdentityDecision(
                target_worker_item_key="backend",
                strategy=IdentityStrategy.REUSE,
                existing_worker_item_id=WORKER_ID,
                derived_from_worker_item_ids=(),
                target_subtask_key="implementation",
                context_disposition=ContextDisposition.PATCH_REQUIRED,
                change_reasons=("goal changed",),
            ),
        ),
        retired_subtask_ids=(),
        retired_worker_item_ids=(),
        decision_summary=DecisionSummary(
            understanding="Revise the active behavior.",
            assumptions=(),
            risks=(),
            estimated_cost_microunits=10,
            estimated_runtime_seconds=60,
            estimated_concurrency=1,
            scope_includes=("implementation",),
            scope_excludes=(),
            metadata={},
        ),
        impact_policy=ImpactPolicy(
            activate_immediately=True,
            budget_level="standard",
            failure_policy="block_affected_branch",
            revision_operation="revise",
        ),
    )


def test_normalized_diff_is_stable_and_exposes_patch_fallback() -> None:
    snapshot = base_snapshot()
    payload = revision_payload(snapshot)
    diff = RevisionDiffNormalizer().normalize(snapshot, payload)
    shuffled = replace(
        payload,
        subtasks=tuple(reversed(payload.subtasks)),
        worker_items=tuple(reversed(payload.worker_items)),
        subtask_milestones=tuple(reversed(payload.subtask_milestones)),
        execution_intents=tuple(reversed(payload.execution_intents)),
        subtask_identity_decisions=tuple(reversed(payload.subtask_identity_decisions)),
        worker_item_identity_decisions=tuple(
            reversed(payload.worker_item_identity_decisions)
        ),
    )
    shuffled_diff = RevisionDiffNormalizer().normalize(snapshot, shuffled)

    assert diff.base_descriptor == payload.base_descriptor
    assert diff.target_descriptor_preview.target_generation == 5
    assert diff.transition_preview[0].allowed_actions == (
        WorkerTransitionAction.PATCH,
        WorkerTransitionAction.CONTINUE,
    )
    assert diff.impact_counts.patch == 1
    assert diff.impact_counts.continue_ == 1
    assert diff.affected_branch_keys == ("backend",)
    assert len(diff.diff_hash) == 64
    assert diff.diff_hash == shuffled_diff.diff_hash


def test_normalized_diff_round_trips_through_its_persisted_mapping() -> None:
    snapshot = base_snapshot()
    diff = RevisionDiffNormalizer().normalize(snapshot, revision_payload(snapshot))

    restored = NormalizedRevisionDiff.from_mapping(diff.to_mapping())

    assert restored == diff
    assert "input_group_changes" not in diff.to_mapping()


def test_legacy_revision_diff_hashes_remain_stable() -> None:
    snapshot = base_snapshot()
    payload = revision_payload(snapshot)
    normalizer = RevisionDiffNormalizer()

    disabled = CollaborationPolicyRequestV1.from_mapping(
        {
            "mode": "disabled",
            "default_policy": {"strategy_id": "all_required"},
            "subtask_overrides": [],
        }
    )
    v4 = PlanningRevisionProposalPayloadV4(
        schema_version=4,
        kind=payload.kind,
        base_descriptor=payload.base_descriptor,
        root_task=payload.root_task,
        subtasks=payload.subtasks,
        worker_items=payload.worker_items,
        subtask_milestones=payload.subtask_milestones,
        subtask_dependencies=payload.subtask_dependencies,
        worker_item_dependencies=payload.worker_item_dependencies,
        execution_intents=payload.execution_intents,
        subtask_identity_decisions=payload.subtask_identity_decisions,
        worker_item_identity_decisions=payload.worker_item_identity_decisions,
        retired_subtask_ids=payload.retired_subtask_ids,
        retired_worker_item_ids=payload.retired_worker_item_ids,
        decision_summary=payload.decision_summary,
        impact_policy=payload.impact_policy,
        collaboration_policy_request=disabled,
    )

    assert (
        normalizer.normalize(snapshot, payload).diff_hash
        == "6977eaebf386ab5ce6707ce246488649e69e8b76a37b040b864e61220cedd735"
    )
    assert (
        normalizer.normalize(snapshot, v4).diff_hash
        == "b0ad95615494cdf5770fc9ec03957ac3517fc31ea00eb97a17c7edea009dca43"
    )


def test_v4_revision_diff_includes_collaboration_policy_change() -> None:
    binding = CollaborationRuntimeBindingSnapshot(
        runtime_definition_id=UUID("61000000-0000-0000-0000-000000000001"),
        runtime_instance_id=UUID("62000000-0000-0000-0000-000000000001"),
        runtime_interface_version="collaboration-runtime-v1",
        runtime_config_version=3,
        runtime_capability_snapshot=CollaborationRuntimeCapabilityProfile(
            interface_version="collaboration-runtime-v1",
            projection_schema_versions=(1,),
            decision_schema_versions=(1,),
            strategy_ids=("all_required",),
            idempotency=IdempotencyCapability.OPERATION_ID,
            max_projection_bytes=262_144,
            max_decision_bytes=65_536,
            max_intents_per_decision=128,
            max_strategy_state_bytes=16_384,
            stability=StabilityLevel.EXPERIMENTAL,
        ),
    )
    enabled = CollaborationPolicyRequestV1.from_mapping(
        {
            "mode": "enabled",
            "default_policy": {"strategy_id": "all_required"},
            "subtask_overrides": [],
        }
    )
    disabled = CollaborationPolicyRequestV1.from_mapping(
        {
            "mode": "disabled",
            "default_policy": {"strategy_id": "all_required"},
            "subtask_overrides": [],
        }
    )
    snapshot = replace(
        base_snapshot(),
        schema_version=3,
        collaboration_policy=CollaborationPolicySnapshotV1.from_request(
            enabled, binding
        ),
    )
    legacy = revision_payload(snapshot)
    payload = PlanningRevisionProposalPayloadV4(
        schema_version=4,
        kind=legacy.kind,
        base_descriptor=legacy.base_descriptor,
        root_task=legacy.root_task,
        subtasks=legacy.subtasks,
        worker_items=legacy.worker_items,
        subtask_milestones=legacy.subtask_milestones,
        subtask_dependencies=legacy.subtask_dependencies,
        worker_item_dependencies=legacy.worker_item_dependencies,
        execution_intents=legacy.execution_intents,
        subtask_identity_decisions=legacy.subtask_identity_decisions,
        worker_item_identity_decisions=legacy.worker_item_identity_decisions,
        retired_subtask_ids=legacy.retired_subtask_ids,
        retired_worker_item_ids=legacy.retired_worker_item_ids,
        decision_summary=legacy.decision_summary,
        impact_policy=legacy.impact_policy,
        collaboration_policy_request=disabled,
    )

    diff = RevisionDiffNormalizer().normalize(snapshot, payload)

    assert diff.collaboration_policy_changes
    assert any(
        item.field_path == "mode"
        and item.before == "enabled"
        and item.after == "disabled"
        for item in diff.collaboration_policy_changes
    )
    assert NormalizedRevisionDiff.from_mapping(diff.to_mapping()) == diff


def test_normalizer_rejects_unchanged_context_when_effective_fields_change() -> None:
    snapshot = base_snapshot()
    payload = revision_payload(snapshot)
    decision = payload.worker_item_identity_decisions[0]
    unchanged = WorkerItemIdentityDecision(
        target_worker_item_key=decision.target_worker_item_key,
        strategy=decision.strategy,
        existing_worker_item_id=decision.existing_worker_item_id,
        derived_from_worker_item_ids=decision.derived_from_worker_item_ids,
        target_subtask_key=decision.target_subtask_key,
        context_disposition=ContextDisposition.UNCHANGED,
        change_reasons=(),
    )
    altered = replace(
        payload,
        worker_item_identity_decisions=(unchanged,),
    )

    with pytest.raises(ValidationError, match="context fingerprint"):
        RevisionDiffNormalizer().normalize(snapshot, altered)


def _v4_snapshot_with_two_workers() -> TaskPlanSnapshot:
    base = base_snapshot()
    source_id = UUID("40000000-0000-0000-0000-000000000002")
    source = WorkerItemPlanItem(
        key="source",
        worker_item_id=source_id,
        subtask_key="implementation",
        title="Source worker",
        role="researcher",
        responsibility="Produce a source result.",
        capability_requirements=("python",),
        worker_policy_ref="policy:default",
        configuration_ref=None,
        concurrency_limit=1,
        source_refs=("brief:initial",),
    )
    request = CollaborationPolicyRequestV2.from_mapping(
        {
            "schema_version": 2,
            "mode": "enabled",
            "default_policy": {"strategy_id": "all_required"},
            "subtask_overrides": [],
            "input_groups": [
                {
                    "group_key": "source",
                    "title": "Source result",
                    "consumer_worker_item_key": "backend",
                    "candidates": [{"worker_item_key": "source", "priority": 1}],
                    "requirements": {
                        "execution_status": "succeeded",
                        "required_artifact_refs": ["artifact_type:report"],
                        "required_evidence_refs": ["acceptance:source"],
                        "evidence_status": "candidate",
                    },
                    "mode": "all",
                    "minimum_count": None,
                    "selection_strategy": "plan_order",
                }
            ],
        }
    )
    policy = CollaborationPolicySnapshotV2.from_request(request, runtime_binding())
    return replace(
        base,
        schema_version=4,
        worker_item_plans=(*base.worker_item_plans, source),
        collaboration_policy=policy,
    )


def test_v5_group_consumer_change_requires_explicit_context_disposition() -> None:
    snapshot = _v4_snapshot_with_two_workers()
    raw_payload = revision_v5_mapping()
    descriptor = raw_payload["base_descriptor"]
    assert isinstance(descriptor, dict)
    descriptor.update(
        {
            "workspace_id": str(snapshot.workspace_id),
            "root_task_id": str(snapshot.root_task_id),
            "plan_snapshot_id": str(snapshot.id),
            "plan_generation": snapshot.generation,
            "plan_content_hash": plan_snapshot_content_hash(snapshot),
        }
    )
    policy = raw_payload["collaboration_policy_request"]
    assert isinstance(policy, dict)
    group = policy["input_groups"]
    assert isinstance(group, list)
    assert isinstance(group[0], dict)
    group[0]["consumer_worker_item_key"] = "a"
    group[0]["candidates"] = [
        {"worker_item_key": "b", "priority": 1},
        {"worker_item_key": "c", "priority": 2},
    ]
    decisions = raw_payload["worker_item_identity_decisions"]
    assert isinstance(decisions, list)
    assert isinstance(decisions[0], dict)
    decisions[0]["context_disposition"] = "unchanged"
    decisions[0]["change_reasons"] = []
    retired = raw_payload["retired_worker_item_ids"]
    assert isinstance(retired, list)
    retired.append("40000000-0000-0000-0000-000000000002")

    from sidera_control_plane.domain.planning_revision_proposals import (
        PlanningRevisionProposalPayloadV5,
    )

    payload = PlanningRevisionProposalPayloadV5.from_mapping(raw_payload)
    resolved = CollaborationPolicySnapshotV2.from_request(
        payload.collaboration_policy_request, runtime_binding()
    )

    with pytest.raises(ValidationError, match="context fingerprint"):
        RevisionDiffNormalizer().normalize(
            snapshot,
            payload,
            collaboration_policy=resolved,
        )


def test_v5_group_consumer_change_uses_patch_transition_when_declared() -> None:
    snapshot = _v4_snapshot_with_two_workers()
    raw_payload = revision_v5_mapping()
    descriptor = raw_payload["base_descriptor"]
    assert isinstance(descriptor, dict)
    descriptor.update(
        {
            "workspace_id": str(snapshot.workspace_id),
            "root_task_id": str(snapshot.root_task_id),
            "plan_snapshot_id": str(snapshot.id),
            "plan_generation": snapshot.generation,
            "plan_content_hash": plan_snapshot_content_hash(snapshot),
        }
    )
    policy = raw_payload["collaboration_policy_request"]
    assert isinstance(policy, dict)
    group = policy["input_groups"]
    assert isinstance(group, list)
    assert isinstance(group[0], dict)
    group[0]["consumer_worker_item_key"] = "a"
    group[0]["candidates"] = [
        {"worker_item_key": "b", "priority": 1},
        {"worker_item_key": "c", "priority": 2},
    ]
    decisions = raw_payload["worker_item_identity_decisions"]
    assert isinstance(decisions, list)
    assert isinstance(decisions[0], dict)
    decisions[0]["context_disposition"] = "patch_required"
    decisions[0]["change_reasons"] = ["input group consumer changed"]
    retired = raw_payload["retired_worker_item_ids"]
    assert isinstance(retired, list)
    retired.append("40000000-0000-0000-0000-000000000002")

    from sidera_control_plane.domain.planning_revision_proposals import (
        PlanningRevisionProposalPayloadV5,
    )

    payload = PlanningRevisionProposalPayloadV5.from_mapping(raw_payload)
    resolved = CollaborationPolicySnapshotV2.from_request(
        payload.collaboration_policy_request, runtime_binding()
    )
    diff = RevisionDiffNormalizer().normalize(
        snapshot,
        payload,
        collaboration_policy=resolved,
    )

    transition = next(
        item for item in diff.transition_preview if item.target_worker_item_key == "a"
    )
    assert transition.allowed_actions == (
        WorkerTransitionAction.PATCH,
        WorkerTransitionAction.CONTINUE,
    )
    assert "input_group_changed" in transition.reason_codes


def test_v6_revision_diff_exposes_integration_spec_changes() -> None:
    base = _v4_snapshot_with_two_workers()
    base_spec = integration_mapping()
    base_spec["integration_worker_item_key"] = "backend"
    base_spec["input_group_keys"] = ["source"]
    base_spec["rework_policy"] = {
        "max_auto_rework_rounds": 1,
        "max_auto_rework_seconds": 900,
        "allowed_worker_item_keys": ["backend", "source"],
    }
    base = replace(
        base,
        schema_version=5,
        integration_spec=IntegrationSpecV1.from_mapping(base_spec),
    )

    raw = revision_v6_mapping()
    descriptor = raw["base_descriptor"]
    assert isinstance(descriptor, dict)
    descriptor.update(
        {
            "workspace_id": str(base.workspace_id),
            "root_task_id": str(base.root_task_id),
            "plan_snapshot_id": str(base.id),
            "plan_generation": base.generation,
            "plan_content_hash": plan_snapshot_content_hash(base),
        }
    )
    retired = raw["retired_worker_item_ids"]
    assert isinstance(retired, list)
    source_id = base.worker_item_plans[-1].worker_item_id
    assert source_id is not None
    retired.append(str(source_id))
    target_spec = raw["integration_spec"]
    assert isinstance(target_spec, dict)
    checks = target_spec["checks"]
    assert isinstance(checks, list)
    assert isinstance(checks[0], dict)
    checks[0]["timeout_seconds"] = 120
    target_spec["apply_policy"] = {
        "apply_on_accept": True,
        "mode": "fast_forward_only",
        "target_ref": "refs/heads/main",
    }

    payload = PlanningRevisionProposalPayloadV6.from_mapping(raw)
    resolved = CollaborationPolicySnapshotV2.from_request(
        payload.collaboration_policy_request, runtime_binding()
    )
    diff = RevisionDiffNormalizer().normalize(
        base,
        payload,
        collaboration_policy=resolved,
    )

    paths = {item.field_path for item in diff.integration_spec_changes}
    assert {
        "integration_spec.integration_worker_item_key",
        "integration_spec.input_group_keys",
        "integration_spec.checks",
        "integration_spec.rework_policy",
        "integration_spec.apply_policy",
    } <= paths
    assert "integration_spec_changed" in diff.risk_flags
    assert (
        NormalizedRevisionDiff.from_mapping(diff.to_mapping()) == diff
    )
