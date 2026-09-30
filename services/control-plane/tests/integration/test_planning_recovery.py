from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sidera_control_plane.application.planning_reconciler import PlanningReconciler
from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
)
from sidera_control_plane.domain.context_packages import ContextPurpose
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
from sidera_control_plane.domain.planning import (
    PlanningBriefRevision,
    PlanningRequest,
    PlanningRequestStatus,
    PlanningRunLease,
    PlanningRunLeaseStatus,
    PlanningUsage,
)
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.work_context import (
    WorkContext,
    WorkContextRevisionReason,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
pytestmark = pytest.mark.integration


def module_facts() -> tuple[ModuleDefinition, ModuleInstance]:
    definition_id = uuid4()
    instance_id = uuid4()
    capabilities = PlannerAdapterCapabilityProfile(
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
    return (
        ModuleDefinition(
            id=definition_id,
            workspace_id=uuid4(),
            module_type="planner_adapter",
            provider="fake",
            interface_version="sidera.planner.v1",
            config_schema_version=1,
            capabilities=capabilities,
            lifecycle=ModuleLifecycle.ACTIVE,
        ),
        ModuleInstance(
            id=instance_id,
            workspace_id=uuid4(),
            definition_id=definition_id,
            instance_name="recovery-planner",
            config_version=1,
            config_namespace="planning",
            config={"planning.scenario": "proposal"},
            secret_refs=(),
            lifecycle=ModuleLifecycle.ACTIVE,
        ),
    )


def profile(instance_id, *, max_retries: int = 1):
    return PlanningProfile(
        id=uuid4(),
        workspace_id=uuid4(),
        profile_key="default",
        version=1,
        display_name="Recovery planner",
        planner_module_instance_id=instance_id,
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.v1",
        system_policy={},
        proposal_schema_version=2,
        allowed_capabilities=("code",),
        adapter_preferences=("fake",),
        adapter_forbidden=(),
        budget=PlanningBudgetPolicy(
            max_context_bytes=64_000,
            max_tokens=10_000,
            max_cost_microunits=1_000_000,
            max_runtime_seconds=600,
            max_revisions=3,
            max_nodes=20,
            max_concurrency=2,
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=max_retries,
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


def seed_expired_running_request(
    uow_factory,
    workspace_id,
    *,
    max_retries: int = 1,
    revision: bool = False,
):
    definition, instance = module_facts()
    definition = replace(definition, workspace_id=workspace_id)
    instance = replace(
        instance,
        workspace_id=workspace_id,
        definition_id=definition.id,
    )
    stored_profile = replace(
        profile(instance.id, max_retries=max_retries),
        workspace_id=workspace_id,
    )
    assert isinstance(definition.capabilities, PlannerAdapterCapabilityProfile)
    snapshot = stored_profile.snapshot(definition.capabilities)
    request = PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=workspace_id,
        profile_snapshot=snapshot,
        objective="Recover an interrupted planner",
        constraints=("Retry the bounded operation",),
        acceptance_criteria=("A new attempt is queued",),
        context_refs=(),
        requested_outputs=("proposal",),
        source_type="web",
        source_ref="conversation:recovery",
        actor="operator",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )
    if revision:
        first_run = request.runs[-1]
        first_lease_id = uuid4()
        request.start_planning(
            run_id=first_run.id,
            lease_id=first_lease_id,
            fencing_token=first_run.attempt,
            now=NOW,
        )
        request.succeed_run(
            run_id=first_run.id,
            lease_id=first_lease_id,
            fencing_token=first_run.attempt,
            provider_ref="provider://recovery-test",
            structured_result_type="proposal",
            usage=PlanningUsage.zero(),
            now=NOW,
        )
        request.mark_proposal_ready(
            run_id=first_run.id,
            proposal_revision=1,
            content_hash="a" * 64,
            now=NOW,
        )
        previous_brief = request.briefs[-1]
        feedback = "Add explicit rollback verification."
        revision_brief = PlanningBriefRevision.create(
            planning_request_id=request.id,
            revision=2,
            objective=previous_brief.objective,
            constraints=previous_brief.constraints,
            acceptance_criteria=previous_brief.acceptance_criteria,
            context_refs=previous_brief.context_refs,
            requested_outputs=previous_brief.requested_outputs,
            user_feedback=feedback,
            source_correlation=previous_brief.source_correlation,
            created_by="operator",
            created_at=NOW,
        )
        request.request_revision(
            expected_proposal_revision=1,
            expected_content_hash="a" * 64,
            feedback=feedback,
            brief=revision_brief,
            new_run_id=uuid4(),
            actor="operator",
            now=NOW,
        )

    run = request.runs[-1]
    lease_id = uuid4()
    request.start_planning(
        run_id=run.id,
        lease_id=lease_id,
        fencing_token=run.attempt,
        now=NOW,
    )
    lease = PlanningRunLease(
        id=lease_id,
        workspace_id=workspace_id,
        planning_run_id=run.id,
        owner_id="planning-worker:dead",
        status=PlanningRunLeaseStatus.ACTIVE,
        fencing_token=run.attempt,
        acquired_at=NOW,
        expires_at=NOW - timedelta(seconds=1),
        released_at=None,
        version=1,
    )
    with uow_factory() as uow:
        uow.modules.add_definition(definition)
        uow.modules.add_instance(instance)
        uow.planning_profiles.add(stored_profile)
        uow.planning_requests.add(request)
        uow.session.flush()
        uow.planning_leases.add(lease)
    return request.id


def test_expired_lease_is_retried_as_a_new_attempt(
    uow_factory, workspace_id
) -> None:
    request_id = seed_expired_running_request(uow_factory, workspace_id)
    reconciler = PlanningReconciler(
        uow_factory=uow_factory,
        owner_id="planning-reconciler:integration",
        clock=lambda: NOW,
    )

    result = reconciler.run_once(limit=10)

    assert result.expired == 1
    assert result.retried == 1
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        assert request.status is PlanningRequestStatus.PLANNING
        assert len(request.runs) == 2
        assert request.runs[0].status.value == "failed"
        assert request.runs[1].status.value == "queued"


def test_expired_lease_persists_failed_request_when_retry_budget_is_exhausted(
    uow_factory, workspace_id
) -> None:
    request_id = seed_expired_running_request(
        uow_factory,
        workspace_id,
        max_retries=0,
    )
    reconciler = PlanningReconciler(
        uow_factory=uow_factory,
        owner_id="planning-reconciler:integration",
        clock=lambda: NOW,
    )

    result = reconciler.run_once(limit=10)

    assert result.expired == 1
    assert result.retried == 0
    assert result.failed == 1
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        assert request.status is PlanningRequestStatus.FAILED
        assert request.runs[-1].status.value == "failed"


def test_new_brief_revision_has_its_own_retry_budget(
    uow_factory, workspace_id
) -> None:
    request_id = seed_expired_running_request(
        uow_factory,
        workspace_id,
        revision=True,
    )
    reconciler = PlanningReconciler(
        uow_factory=uow_factory,
        owner_id="planning-reconciler:integration",
        clock=lambda: NOW,
    )

    result = reconciler.run_once(limit=10)

    assert result.expired == 1
    assert result.retried == 1
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        assert request.status is PlanningRequestStatus.PLANNING
        assert len(request.runs) == 3
        assert request.runs[-1].brief_revision == 2
        assert request.runs[-1].status.value == "queued"


def test_expired_lease_retry_rebuilds_planning_context_assembly(
    uow_factory, workspace_id
) -> None:
    request_id = seed_expired_running_request(uow_factory, workspace_id)
    context_id = uuid4()
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        brief = request.briefs[-1]
        revision = WorkContextRevisionFactory().create(
            work_context_id=context_id,
            workspace_id=workspace_id,
            brief=brief,
            revision_number=1,
            reason=WorkContextRevisionReason.REQUEST_CREATED,
            actor="integration",
            created_at=NOW,
        )
        context = WorkContext.create(
            work_context_id=context_id,
            workspace_id=workspace_id,
            planning_request_id=request.id,
            first_revision=revision,
            created_at=NOW,
        )
        uow.work_contexts.add(context, revision)

    reconciler = PlanningReconciler(
        uow_factory=uow_factory,
        owner_id="planning-reconciler:integration",
        clock=lambda: NOW,
        planning_context_ttl_seconds=3600,
    )

    result = reconciler.run_once(limit=10)

    assert result.retried == 1
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        retry = request.runs[-1]
        assembly = uow.context_assemblies.get_for_consumer(
            ContextPurpose.PLANNING_RUN,
            retry.id,
            workspace_id,
        )
        assert assembly is not None
        assert assembly.status.value == "queued"
        assert assembly.work_context_revision_id == revision.revision_id
