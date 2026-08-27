from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from sidera_control_plane.domain.errors import ValidationError
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
)
from sidera_control_plane.domain.plans import (
    SubtaskMilestone,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.domain.revision_diff import RevisionDiffNormalizer

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
        subtask_identity_decisions=tuple(
            reversed(payload.subtask_identity_decisions)
        ),
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
