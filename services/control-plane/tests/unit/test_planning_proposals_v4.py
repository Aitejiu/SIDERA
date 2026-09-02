from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sidera_control_plane.domain.collaboration import CollaborationPolicyRequestV1
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.planning import PlanningRequestKind
from sidera_control_plane.domain.planning_proposals import (
    DecisionSummary,
    ExecutionIntent,
    ImpactPolicy,
    PlanningProposalPayloadV4,
    ProposedRootTask,
    ProposedSubtask,
    ProposedWorkerItem,
)
from sidera_control_plane.domain.plans import (
    SubtaskMilestone,
    WorkItemKind,
)
from sidera_control_plane.domain.proposal_validation import validate_proposal
from tests.unit.test_planning_proposals import planning_profile_snapshot

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def proposal_v4() -> PlanningProposalPayloadV4:
    return PlanningProposalPayloadV4(
        schema_version=4,
        kind=PlanningRequestKind.CREATE,
        root_task=ProposedRootTask(
            title="Runtime-enabled plan",
            goal_summary="Use a controlled collaboration runtime.",
            acceptance_refs=("acceptance:root",),
        ),
        subtasks=(
            ProposedSubtask(
                key="build",
                kind=WorkItemKind.CHILD,
                title="Build",
                goal_summary="Build the implementation.",
                acceptance_refs=("acceptance:build",),
                expected_outputs=("artifact_type:build",),
                milestone_key="m1",
                source_refs=("brief:1",),
            ),
        ),
        worker_items=(
            ProposedWorkerItem(
                key="build-worker",
                subtask_key="build",
                title="Build worker",
                role="backend",
                responsibility="Build the implementation.",
                capability_requirements=("code",),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=("brief:1",),
            ),
        ),
        subtask_milestones=(
            SubtaskMilestone(
                key="m1",
                title="Build",
                subtask_keys=("build",),
                acceptance_refs=("acceptance:build",),
                order_hint=1,
            ),
        ),
        subtask_dependencies=(),
        worker_item_dependencies=(),
        execution_intents=(
            ExecutionIntent(
                worker_item_key="build-worker",
                purpose="Build the implementation.",
                context_refs=("context:brief",),
                expected_artifact_refs=("artifact_type:build",),
                required_evidence_refs=("acceptance:build",),
                capability_requirements=("code",),
                adapter_preferences=("opencode",),
                budget_class="standard",
                deadline_seconds=60,
                not_before_seconds=None,
                max_attempts=1,
                activation_dependencies=(),
            ),
        ),
        decision_summary=DecisionSummary(
            understanding="Build a runtime-enabled plan.",
            assumptions=(),
            risks=(),
            estimated_cost_microunits=1,
            estimated_runtime_seconds=60,
            estimated_concurrency=1,
            scope_includes=("build",),
            scope_excludes=(),
            metadata={},
        ),
        impact_policy=ImpactPolicy(
            activate_immediately=True,
            budget_level="standard",
            failure_policy="retry_then_block",
        ),
        collaboration_policy_request=CollaborationPolicyRequestV1.from_mapping(
            {
                "mode": "enabled",
                "default_policy": {"strategy_id": "all_required"},
                "subtask_overrides": [],
            }
        ),
    )


def test_initial_proposal_v4_round_trips_the_complete_policy_request() -> None:
    value = proposal_v4()

    assert PlanningProposalPayloadV4.from_mapping(value.to_mapping()) == value
    assert value.to_mapping()["schema_version"] == 4
    assert "runtime_instance_id" not in value.to_mapping()["collaboration_policy_request"]


def test_initial_proposal_v4_rejects_runtime_module_selection() -> None:
    raw = proposal_v4().to_mapping()
    policy = dict(raw["collaboration_policy_request"])
    policy["runtime_instance_id"] = str(uuid4())
    raw["collaboration_policy_request"] = policy

    with pytest.raises(ValidationError, match="unknown collaboration policy request fields"):
        PlanningProposalPayloadV4.from_mapping(raw)


def test_initial_proposal_v4_mapping_dispatches_through_validation() -> None:
    value = replace(
        proposal_v4(),
        collaboration_policy_request=CollaborationPolicyRequestV1.from_mapping(
            {
                "mode": "disabled",
                "default_policy": {"strategy_id": "all_required"},
                "subtask_overrides": [],
            }
        ),
    )
    profile = replace(planning_profile_snapshot(), proposal_schema_version=4)

    validated = validate_proposal(
        value.to_mapping(),
        profile_snapshot=profile,
        brief_revision=1,
    )

    assert isinstance(validated, PlanningProposalPayloadV4)
    assert validated == value
