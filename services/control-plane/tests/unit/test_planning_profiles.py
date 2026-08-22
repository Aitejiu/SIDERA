from dataclasses import replace
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
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)


def planner_capabilities(
    *, max_context_bytes: int = 128_000, interface_version: str = "sidera.planner.v1"
) -> PlannerAdapterCapabilityProfile:
    return PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=max_context_bytes,
        stability=StabilityLevel.STABLE,
        interface_version=interface_version,
        proposal_schema_versions=(1, 2),
    )


def planning_profile(**overrides: object) -> PlanningProfile:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "profile_key": "default",
        "version": 3,
        "display_name": "Default planner",
        "planner_module_instance_id": uuid4(),
        "planner_module_config_version": 7,
        "planner_interface_version": "sidera.planner.v1",
        "prompt_policy_version": "prompt-2026-08-23",
        "system_policy": {
            "approval": {"required": True},
            "labels": ["task-first", "review"],
        },
        "proposal_schema_version": 1,
        "allowed_capabilities": ("code", "test", "code"),
        "adapter_preferences": ("opencode", "fake", "opencode"),
        "adapter_forbidden": ("unsafe", "unsafe"),
        "budget": PlanningBudgetPolicy(
            max_context_bytes=64_000,
            max_tokens=20_000,
            max_cost_microunits=3_000_000,
            max_runtime_seconds=900,
            max_revisions=4,
            max_nodes=30,
            max_concurrency=4,
        ),
        "retry_policy": PlanningRetryPolicy(
            max_retries=2,
            backoff_seconds=(5, 15, 5),
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
            invalid_output_policy=InvalidOutputPolicy.RETRY,
        ),
        "default_priority": 50,
        "execution_max_attempts": 3,
        "execution_budget_level": "standard",
        "user_selectable": True,
        "status": PlanningProfileStatus.ACTIVE,
        "planner_secret_refs": ("secret://opencode/api-key",),
    }
    defaults.update(overrides)
    return PlanningProfile(**defaults)  # type: ignore[arg-type]


def test_profile_snapshot_is_immutable_and_complete() -> None:
    profile = planning_profile()

    snapshot = profile.snapshot(
        planner_capabilities(),
        module_instance_config_version=7,
    )

    assert snapshot.profile_version == profile.version
    assert snapshot.profile_id == profile.id
    assert snapshot.planner_module_instance_id == profile.planner_module_instance_id
    assert snapshot.planner_module_config_version == 7
    assert snapshot.proposal_schema_version == 1
    assert snapshot.allowed_capabilities == ("code", "test")
    assert snapshot.planner_capabilities.structured_output == "json_schema"
    assert snapshot.planner_secret_refs == ("secret://opencode/api-key",)
    with pytest.raises(TypeError):
        snapshot.system_policy["approval"]["required"] = False  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.system_policy["labels"] += ("changed",)  # type: ignore[index]


def test_profile_normalizes_tuples_and_rejects_secret_values() -> None:
    profile = planning_profile(
        planner_secret_refs=(" secret://one ", "secret://one"),
    )

    assert profile.allowed_capabilities == ("code", "test")
    assert profile.adapter_preferences == ("opencode", "fake")
    assert profile.adapter_forbidden == ("unsafe",)
    assert profile.planner_secret_refs == ("secret://one",)
    assert "api-key-value" not in repr(profile.snapshot(planner_capabilities()))


def test_profile_selection_requires_active_user_selectable_profile() -> None:
    with pytest.raises(ValidationError, match="disabled"):
        replace(planning_profile(), status=PlanningProfileStatus.DISABLED).assert_user_selectable()
    with pytest.raises(ValidationError, match="user-selectable"):
        replace(planning_profile(), user_selectable=False).assert_user_selectable()


@pytest.mark.parametrize(
    "field",
    (
        "max_context_bytes",
        "max_tokens",
        "max_cost_microunits",
        "max_runtime_seconds",
        "max_revisions",
        "max_nodes",
        "max_concurrency",
    ),
)
def test_budget_limits_must_be_positive(field: str) -> None:
    budget = {
        "max_context_bytes": 1,
        "max_tokens": 1,
        "max_cost_microunits": 1,
        "max_runtime_seconds": 1,
        "max_revisions": 1,
        "max_nodes": 1,
        "max_concurrency": 1,
    }
    budget[field] = 0

    with pytest.raises(ValidationError, match=field):
        PlanningBudgetPolicy(**budget)  # type: ignore[arg-type]


def test_retry_policy_normalizes_backoff_and_validates_policies() -> None:
    policy = PlanningRetryPolicy(
        max_retries=2,
        backoff_seconds=(15, 5, 15),
        unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
        invalid_output_policy=InvalidOutputPolicy.RETRY,
    )

    assert policy.backoff_seconds == (15, 5)
    assert policy.unknown_outcome_policy.value == "block"
    assert policy.invalid_output_policy.value == "retry"

    with pytest.raises(ValidationError, match="max_retries"):
        PlanningRetryPolicy(
            max_retries=-1,
            backoff_seconds=(5,),
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
            invalid_output_policy=InvalidOutputPolicy.RETRY,
        )


def test_snapshot_rejects_incompatible_module_and_capabilities() -> None:
    profile = planning_profile()

    with pytest.raises(ValidationError, match="module type"):
        profile.snapshot(
            planner_capabilities(),
            module_type="worker_adapter",
            module_instance_config_version=7,
        )
    with pytest.raises(ValidationError, match="config version"):
        profile.snapshot(planner_capabilities(), module_instance_config_version=8)
    with pytest.raises(ValidationError, match="interface"):
        profile.snapshot(
            planner_capabilities(interface_version="sidera.planner.v2"),
            module_instance_config_version=7,
        )
    with pytest.raises(ValidationError, match="proposal schema"):
        profile.snapshot(
            replace(planner_capabilities(), proposal_schema_versions=(2,)),
            module_instance_config_version=7,
        )
    with pytest.raises(ValidationError, match="context"):
        profile.snapshot(
            planner_capabilities(max_context_bytes=32_000),
            module_instance_config_version=7,
        )
