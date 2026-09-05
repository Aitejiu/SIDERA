from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.modules import (
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    StabilityLevel,
)
from sidera_control_plane.domain.planning_proposals import PlanningProposalPayloadV5
from sidera_control_plane.domain.proposal_validation import (
    ProposalValidationError,
    validate_proposal,
)

from .result_input_fixtures import proposal_v5_mapping
from .test_collaboration_policy_v2 import runtime_binding
from .test_planning_profiles import planner_capabilities, planning_profile
from .test_planning_proposals import planning_profile_snapshot


def v5_profile_snapshot(*, capability: CollaborationRuntimeCapabilityProfile | None = None):
    binding = runtime_binding(capability=capability)
    profile = planning_profile(
        proposal_schema_version=5,
        collaboration_runtime_binding=binding,
    )
    return profile.snapshot(
        replace(planner_capabilities(), proposal_schema_versions=(5,)),
        module_instance_config_version=7,
    )


def test_initial_v5_uses_v2_policy_and_round_trips() -> None:
    raw = proposal_v5_mapping()

    value = PlanningProposalPayloadV5.from_mapping(raw)

    assert value.schema_version == 5
    assert value.to_mapping() == raw
    assert value.collaboration_policy_request.schema_version == 2
    assert value.collaboration_policy_request.input_groups[0].group_key == (
        "implementation"
    )


def test_initial_v5_validation_accepts_input_group_with_matching_profile() -> None:
    profile = v5_profile_snapshot()

    validated = validate_proposal(
        proposal_v5_mapping(),
        profile_snapshot=profile,
        brief_revision=1,
    )

    assert isinstance(validated, PlanningProposalPayloadV5)


def test_v5_rejects_missing_input_groups() -> None:
    raw = proposal_v5_mapping()
    policy = raw["collaboration_policy_request"]
    assert isinstance(policy, dict)
    del policy["input_groups"]

    with pytest.raises(ValidationError, match="input_groups"):
        PlanningProposalPayloadV5.from_mapping(raw)


def test_v5_rejects_disabled_collaboration_with_input_groups() -> None:
    raw = proposal_v5_mapping()
    policy = deepcopy(raw["collaboration_policy_request"])
    assert isinstance(policy, dict)
    policy["mode"] = "disabled"
    raw["collaboration_policy_request"] = policy

    with pytest.raises(ValidationError, match="disabled collaboration policy"):
        PlanningProposalPayloadV5.from_mapping(raw)


def test_old_profile_cannot_confirm_v5() -> None:
    with pytest.raises(ProposalValidationError, match="proposal schema"):
        validate_proposal(
            proposal_v5_mapping(),
            profile_snapshot=planning_profile_snapshot(),
            brief_revision=1,
        )


def test_v5_profile_requires_a_runtime_binding() -> None:
    with pytest.raises(ValidationError, match="runtime binding"):
        planning_profile(proposal_schema_version=5)


@pytest.mark.parametrize(
    "capability",
    [
        CollaborationRuntimeCapabilityProfile(
            interface_version="collaboration-runtime-v2",
            projection_schema_versions=(1, 2),
            decision_schema_versions=(2,),
            strategy_ids=("all_required",),
            idempotency=IdempotencyCapability.OPERATION_ID,
            max_projection_bytes=262_144,
            max_decision_bytes=65_536,
            max_intents_per_decision=128,
            max_strategy_state_bytes=16_384,
            stability=StabilityLevel.EXPERIMENTAL,
        ),
        CollaborationRuntimeCapabilityProfile(
            interface_version="collaboration-runtime-v2",
            projection_schema_versions=(2,),
            decision_schema_versions=(1, 2),
            strategy_ids=("all_required",),
            idempotency=IdempotencyCapability.OPERATION_ID,
            max_projection_bytes=262_144,
            max_decision_bytes=65_536,
            max_intents_per_decision=128,
            max_strategy_state_bytes=16_384,
            stability=StabilityLevel.EXPERIMENTAL,
        ),
    ],
)
def test_v5_profile_rejects_runtime_without_input_group_capability(
    capability: CollaborationRuntimeCapabilityProfile,
) -> None:
    with pytest.raises(ValidationError, match="input_groups_v1"):
        v5_profile_snapshot(capability=capability)
