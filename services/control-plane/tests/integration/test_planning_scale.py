from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.adapters.fake_planner import (
    FakePlannerAdapter,
    FakePlannerConfig,
    FakeScenario,
)
from sidera_control_plane.application.planning_runner import (
    PlanningRunner,
    RegistryPlannerAdapterFactory,
)
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.planning import PlanningRequest, PlanningRequestStatus
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
pytestmark = pytest.mark.integration


def test_real_planning_runner_persists_the_bounded_scale_proposal(
    database, workspace_id: UUID, uow_factory
) -> None:
    definition_id = uuid4()
    instance_id = uuid4()
    profile_id = uuid4()
    definition = ModuleDefinition(
        id=definition_id,
        workspace_id=workspace_id,
        module_type="planner_adapter",
        provider="fake",
        interface_version="sidera.planner.v1",
        config_schema_version=1,
        capabilities=_capabilities(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    instance = ModuleInstance(
        id=instance_id,
        workspace_id=workspace_id,
        definition_id=definition_id,
        instance_name="scale-integration-planner",
        config_version=1,
        config_namespace="planning",
        config={"planning.scenario": "scale"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    profile = PlanningProfile(
        id=profile_id,
        workspace_id=workspace_id,
        profile_key="scale-integration",
        version=1,
        display_name="Scale integration planner",
        planner_module_instance_id=instance_id,
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="r5.1-integration",
        system_policy={},
        proposal_schema_version=2,
        allowed_capabilities=("code",),
        adapter_preferences=("fake",),
        adapter_forbidden=(),
        budget=PlanningBudgetPolicy(
            max_context_bytes=64_000,
            max_tokens=100_000,
            max_cost_microunits=1_000_000_000,
            max_runtime_seconds=3_600,
            max_revisions=20,
            max_nodes=500,
            max_concurrency=4,
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=1,
            backoff_seconds=(1,),
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
            invalid_output_policy=InvalidOutputPolicy.RETRY,
        ),
        default_priority=50,
        execution_max_attempts=2,
        execution_budget_level="standard",
        user_selectable=True,
        status=PlanningProfileStatus.ACTIVE,
        planner_secret_refs=(),
    )
    snapshot = profile.snapshot(_capabilities())
    request = PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=workspace_id,
        profile_snapshot=snapshot,
        objective="Exercise the bounded large proposal path",
        constraints=("Keep graph edges deterministic",),
        acceptance_criteria=("Store 200 subtasks and 200 WorkerItems",),
        context_refs=("repo://sidera",),
        requested_outputs=("proposal",),
        source_type="e2e",
        source_ref="planning-scale",
        actor="e2e",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )

    with uow_factory() as uow:
        uow.modules.add_definition(definition)
        uow.modules.add_instance(instance)
        uow.planning_profiles.add(profile)
        uow.planning_requests.add(request)

    factory = RegistryPlannerAdapterFactory(
        {
            "fake": lambda config, _secrets: FakePlannerAdapter(
                FakePlannerConfig(
                    scenario=cast(FakeScenario, config["planning.scenario"])
                )
            )
        }
    )
    result = PlanningRunner(
        uow_factory=uow_factory,
        adapter_factory=factory,
        owner_id="planning-worker:scale-integration",
        lease_ttl_seconds=60,
        lease_renew_interval_seconds=10,
        clock=lambda: NOW,
    ).run_once(1)

    assert result.succeeded == 1
    with uow_factory() as uow:
        stored_request = uow.planning_requests.get(request.id, workspace_id)
        proposals = uow.planning_records.list_proposals(request.id, workspace_id)

    assert stored_request is not None
    assert stored_request.status is PlanningRequestStatus.PROPOSAL_READY
    assert len(proposals) == 1
    proposal = proposals[0].payload
    assert len(proposal.subtasks) == 200
    assert len(proposal.worker_items) == 200
    assert len(proposal.execution_intents) == 200
    assert len(proposal.subtask_dependencies) == 199
    assert len(proposal.worker_item_dependencies) == 199


def _capabilities() -> PlannerAdapterCapabilityProfile:
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
