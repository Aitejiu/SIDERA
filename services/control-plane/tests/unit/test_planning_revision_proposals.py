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
)
from sidera_control_plane.domain.plans import (
    DependencyEnforcement,
    DependencyKind,
    SubtaskDependency,
    SubtaskMilestone,
    WorkItemKind,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
SUBTASK_ID = UUID("50000000-0000-0000-0000-000000000001")
WORKER_ID = UUID("40000000-0000-0000-0000-000000000001")
SNAPSHOT_ID = UUID("10000000-0000-0000-0000-000000000001")


def revision_base() -> RevisionBaseDescriptorV1:
    return RevisionBaseDescriptorV1(
        schema_version=1,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        plan_snapshot_id=SNAPSHOT_ID,
        plan_generation=4,
        plan_content_hash="a" * 64,
        root_task_version=7,
        captured_at=NOW,
    )


def proposal() -> PlanningRevisionProposalPayloadV3:
    return PlanningRevisionProposalPayloadV3(
        schema_version=3,
        kind=PlanningRequestKind.REVISION,
        base_descriptor=revision_base(),
        root_task=ProposedRootTask(
            title="Running control room",
            goal_summary="Keep the existing execution context while changing acceptance.",
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
                reason="The subtask remains the same.",
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
                change_reasons=("acceptance changed",),
            ),
        ),
        retired_subtask_ids=(),
        retired_worker_item_ids=(),
        decision_summary=DecisionSummary(
            understanding="Revise the running implementation acceptance.",
            assumptions=("The current WorkerItem remains useful.",),
            risks=("The adapter may require continuation.",),
            estimated_cost_microunits=10,
            estimated_runtime_seconds=60,
            estimated_concurrency=1,
            scope_includes=("implementation",),
            scope_excludes=("deployment",),
            metadata={"source": "test"},
        ),
        impact_policy=ImpactPolicy(
            activate_immediately=True,
            budget_level="standard",
            failure_policy="block_affected_branch",
            manual_gate=False,
            revision_operation="revise",
        ),
    )


def test_revision_payload_round_trips_every_explicit_v3_field() -> None:
    value = proposal()

    restored = PlanningRevisionProposalPayloadV3.from_mapping(
        value.canonical_payload()
    )

    assert restored == value
    assert restored.schema_version == 3
    assert restored.kind is PlanningRequestKind.REVISION
    assert restored.base_descriptor == revision_base()
    assert restored.worker_item_identity_decisions[0].context_disposition is (
        ContextDisposition.PATCH_REQUIRED
    )


def test_revision_payload_rejects_unknown_fields_and_identity_shape_errors() -> None:
    raw = proposal().canonical_payload()
    raw["unexpected"] = True

    with pytest.raises(ValidationError, match="unknown revision proposal fields"):
        PlanningRevisionProposalPayloadV3.from_mapping(raw)

    with pytest.raises(ValidationError, match="one existing identity only"):
        WorkerItemIdentityDecision(
            target_worker_item_key="backend",
            strategy=IdentityStrategy.REUSE,
            existing_worker_item_id=WORKER_ID,
            derived_from_worker_item_ids=(WORKER_ID,),
            target_subtask_key="implementation",
            context_disposition=ContextDisposition.UNCHANGED,
            change_reasons=(),
        )


def test_revision_payload_requires_revision_kind_and_schema_version_three() -> None:
    raw = proposal().canonical_payload()
    raw["kind"] = "create"

    with pytest.raises(ValidationError, match="revision proposal kind"):
        PlanningRevisionProposalPayloadV3.from_mapping(raw)

    raw = proposal().canonical_payload()
    raw["schema_version"] = 2

    with pytest.raises(ValidationError, match="schema_version must be 3"):
        PlanningRevisionProposalPayloadV3.from_mapping(raw)


def test_revision_payload_rejects_non_revision_dependency_kind() -> None:
    value = proposal()

    with pytest.raises(ValidationError):
        replace(
            value,
            subtask_dependencies=(
                SubtaskDependency(
                    upstream_subtask_key="implementation",
                    downstream_subtask_key="implementation",
                    kind=DependencyKind.HARD,
                    required_evidence_refs=(),
                    enforcement=DependencyEnforcement.CONTROL_PLANE,
                ),
            ),
        )


def test_revision_payload_v4_requires_complete_collaboration_request() -> None:
    raw = proposal().canonical_payload()
    raw["schema_version"] = 4
    raw["collaboration_policy_request"] = {
        "mode": "enabled",
        "default_policy": {"strategy_id": "all_required"},
        "subtask_overrides": [],
    }

    value = PlanningRevisionProposalPayloadV4.from_mapping(raw)
    assert value.schema_version == 4
    assert PlanningRevisionProposalPayloadV4.from_mapping(value.to_mapping()) == value

    missing = dict(raw)
    del missing["collaboration_policy_request"]
    with pytest.raises(ValidationError, match="collaboration_policy_request"):
        PlanningRevisionProposalPayloadV4.from_mapping(missing)
