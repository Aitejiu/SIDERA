from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Lock
from uuid import uuid4

import pytest

from sidera_control_plane.adapters.fake_planner import (
    FakePlannerAdapter,
    FakePlannerConfig,
)
from sidera_control_plane.application.planner_runtime import (
    PlannerRateLimited,
    PlannerUnknownOutcome,
)
from sidera_control_plane.application.planning_reconciler import PlanningReconciler
from sidera_control_plane.application.planning_runner import PlanningRunner
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
    PlanningRequest,
    PlanningRequestStatus,
    PlanningRunStatus,
)
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
        proposal_schema_versions=(2, 3),
    )


def module_facts() -> tuple[ModuleDefinition, ModuleInstance]:
    definition_id = uuid4()
    instance_id = uuid4()
    return (
        ModuleDefinition(
            id=definition_id,
            workspace_id=uuid4(),
            module_type="planner_adapter",
            provider="fake",
            interface_version="sidera.planner.v1",
            config_schema_version=1,
            capabilities=planner_capabilities(),
            lifecycle=ModuleLifecycle.ACTIVE,
        ),
        ModuleInstance(
            id=instance_id,
            workspace_id=uuid4(),
            definition_id=definition_id,
            instance_name="integration-planner",
            config_version=1,
            config_namespace="planning",
            config={"planning.scenario": "proposal"},
            secret_refs=(),
            lifecycle=ModuleLifecycle.ACTIVE,
        ),
    )


def profile(
    instance_id,
    *,
    unknown_outcome_policy: UnknownOutcomePolicy = UnknownOutcomePolicy.BLOCK,
):
    return PlanningProfile(
        id=uuid4(),
        workspace_id=uuid4(),
        profile_key="default",
        version=1,
        display_name="Integration planner",
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
            max_retries=1,
            backoff_seconds=(1,),
            unknown_outcome_policy=unknown_outcome_policy,
            invalid_output_policy=InvalidOutputPolicy.RETRY,
        ),
        default_priority=50,
        execution_max_attempts=2,
        execution_budget_level="standard",
        user_selectable=True,
        status=PlanningProfileStatus.ACTIVE,
        planner_secret_refs=(),
    )


class CountingAdapter:
    def __init__(self) -> None:
        self._lock = Lock()
        self.calls = 0
        self._delegate = FakePlannerAdapter(FakePlannerConfig(scenario="proposal"))

    def probe(self, config):
        return self._delegate.probe(config)

    def accept_context(self, materialization, *, operation_id):
        return self._delegate.accept_context(
            materialization,
            operation_id=operation_id,
        )

    def plan(self, planner_input, control):
        with self._lock:
            self.calls += 1
        return self._delegate.plan(planner_input, control)

    def cancel(self, operation_id):
        return self._delegate.cancel(operation_id)


class RateLimitedAdapter(CountingAdapter):
    def plan(self, planner_input, control):
        with self._lock:
            self.calls += 1
        return PlannerRateLimited(
            code="rate_limited",
            safe_message="planner rate limit reached",
            retryable=True,
            unknown_outcome=False,
            provider_ref="fake-planner",
        )


class RaisingAdapter(CountingAdapter):
    def plan(self, planner_input, control):
        with self._lock:
            self.calls += 1
        raise RuntimeError("adapter transport failed after dispatch")


class UnknownOutcomeAdapter(CountingAdapter):
    def plan(self, planner_input, control):
        with self._lock:
            self.calls += 1
        return PlannerUnknownOutcome(
            code="unknown_outcome",
            safe_message="planner prompt outcome could not be confirmed",
            retryable=False,
            unknown_outcome=True,
            provider_ref="fake-planner",
        )


class StaticFactory:
    def __init__(self, adapter: CountingAdapter) -> None:
        self.adapter = adapter

    def create(self, definition, instance, profile_snapshot):
        return self.adapter


def seed_request(
    uow_factory,
    workspace_id,
    *,
    idempotency: IdempotencyCapability = IdempotencyCapability.OPERATION_ID,
    unknown_outcome_policy: UnknownOutcomePolicy = UnknownOutcomePolicy.BLOCK,
):
    definition, instance = module_facts()
    definition = replace(definition, workspace_id=workspace_id)
    instance = replace(
        instance,
        workspace_id=workspace_id,
        definition_id=definition.id,
    )
    assert isinstance(definition.capabilities, PlannerAdapterCapabilityProfile)
    definition = replace(
        definition,
        capabilities=replace(definition.capabilities, idempotency=idempotency),
    )
    stored_profile = replace(
        profile(
            instance.id,
            unknown_outcome_policy=unknown_outcome_policy,
        ),
        workspace_id=workspace_id,
    )
    snapshot = stored_profile.snapshot(definition.capabilities)
    request = PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=workspace_id,
        profile_snapshot=snapshot,
        objective="Run a planner in the background",
        constraints=("The API must return before planning finishes",),
        acceptance_criteria=("A typed proposal is durable",),
        context_refs=("repo://sidera",),
        requested_outputs=("proposal",),
        source_type="web",
        source_ref="conversation:integration",
        actor="operator",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )
    with uow_factory() as uow:
        uow.modules.add_definition(definition)
        uow.modules.add_instance(instance)
        uow.planning_profiles.add(stored_profile)
        uow.planning_requests.add(request)
    return request.id


def runner(uow_factory, adapter, *, clock=lambda: NOW):
    return PlanningRunner(
        uow_factory=uow_factory,
        adapter_factory=StaticFactory(adapter),
        owner_id="planning-worker:integration",
        lease_ttl_seconds=60,
        lease_renew_interval_seconds=10,
        clock=clock,
    )


def test_two_runners_claim_a_queued_run_once(
    database, workspace_id, uow_factory
) -> None:
    request_id = seed_request(uow_factory, workspace_id)
    adapter = CountingAdapter()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(runner(uow_factory, adapter).run_once, 1),
            executor.submit(runner(uow_factory, adapter).run_once, 1),
        ]
        results = [future.result() for future in futures]

    assert sum(item.claimed for item in results) == 1
    assert adapter.calls == 1
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        assert request.status is PlanningRequestStatus.PROPOSAL_READY
        assert len(uow.planning_records.list_proposals(request_id, workspace_id)) == 1
        assert request.runs[-1].status is PlanningRunStatus.SUCCEEDED


def test_rate_limit_uses_bounded_backoff_for_a_new_attempt(
    uow_factory, workspace_id
) -> None:
    request_id = seed_request(uow_factory, workspace_id)
    adapter = RateLimitedAdapter()

    first = runner(uow_factory, adapter).run_once(1)
    assert first.failed == 1

    recovery = PlanningReconciler(
        uow_factory=uow_factory,
        owner_id="planning-reconciler:integration",
        clock=lambda: NOW.replace(second=2),
    ).run_once(10)

    assert recovery.retried == 1
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        assert request.status is PlanningRequestStatus.PLANNING
        assert request.runs[-1].attempt == 2


def test_unclassified_planner_exception_is_not_automatically_retried(
    uow_factory, workspace_id
) -> None:
    request_id = seed_request(uow_factory, workspace_id)
    adapter = RaisingAdapter()

    assert runner(uow_factory, adapter).run_once(1).failed == 1
    recovery = PlanningReconciler(
        uow_factory=uow_factory,
        owner_id="planning-reconciler:integration",
        clock=lambda: NOW.replace(second=2),
    ).run_once(10)

    assert recovery.retried == 0
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        assert request.status is PlanningRequestStatus.FAILED
        assert request.runs[-1].error_code == "planner_exception"
        assert len(request.runs) == 1
    assert adapter.calls == 1


def test_unknown_planner_outcome_is_blocked_without_retry(
    uow_factory, workspace_id
) -> None:
    request_id = seed_request(uow_factory, workspace_id)
    adapter = UnknownOutcomeAdapter()

    result = runner(uow_factory, adapter).run_once(1)
    assert result.blocked == 1
    recovery = PlanningReconciler(
        uow_factory=uow_factory,
        owner_id="planning-reconciler:integration",
        clock=lambda: NOW.replace(second=2),
    ).run_once(10)

    assert recovery.retried == 0
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        assert request.status is PlanningRequestStatus.FAILED
        assert request.runs[-1].status is PlanningRunStatus.BLOCKED
        assert request.runs[-1].error_code == "unknown_outcome"
        assert len(request.runs) == 1
    assert adapter.calls == 1


def test_unknown_non_idempotent_planner_is_not_retried_even_when_policy_allows_it(
    uow_factory, workspace_id
) -> None:
    request_id = seed_request(
        uow_factory,
        workspace_id,
        idempotency=IdempotencyCapability.NONE,
        unknown_outcome_policy=UnknownOutcomePolicy.RETRY,
    )
    adapter = UnknownOutcomeAdapter()

    result = runner(uow_factory, adapter).run_once(1)
    assert result.blocked == 1
    recovery = PlanningReconciler(
        uow_factory=uow_factory,
        owner_id="planning-reconciler:integration",
        clock=lambda: NOW.replace(second=2),
    ).run_once(10)

    assert recovery.retried == 0
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        assert request.status is PlanningRequestStatus.FAILED
        assert request.runs[-1].status is PlanningRunStatus.BLOCKED
        assert request.runs[-1].error_code == "unknown_outcome"
        assert len(request.runs) == 1
    assert adapter.calls == 1


def test_invalid_output_stops_after_profile_retry_limit(
    uow_factory, workspace_id
) -> None:
    request_id = seed_request(uow_factory, workspace_id)
    adapter = FakePlannerAdapter(FakePlannerConfig(scenario="invalid_schema"))

    assert runner(uow_factory, adapter).run_once(1).failed == 1
    assert PlanningReconciler(
        uow_factory=uow_factory,
        owner_id="planning-reconciler:integration",
        clock=lambda: NOW.replace(second=2),
    ).run_once(10).retried == 1
    assert runner(
        uow_factory, adapter, clock=lambda: NOW.replace(second=2)
    ).run_once(1).failed == 1
    final = PlanningReconciler(
        uow_factory=uow_factory,
        owner_id="planning-reconciler:integration",
        clock=lambda: NOW.replace(second=4),
    ).run_once(10)

    assert final.retried == 0
    assert final.failed == 1
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        assert request.status is PlanningRequestStatus.FAILED
        assert len(request.runs) == 2
