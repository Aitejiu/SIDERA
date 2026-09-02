from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.adapters.fake_planner import (
    FakePlannerAdapter,
    FakePlannerConfig,
)
from sidera_control_plane.application.planner_runtime import (
    CapabilityCatalogSnapshot,
    PlannerInputAssembler,
    PlannerPolicyConstraints,
    PlannerProposalResult,
    PlannerRunControl,
)
from sidera_control_plane.application.planning_runner import (
    PlannerAdapterFactory,
    PlanningAdapterUnavailable,
    PlanningBatchResult,
    PlanningLeaseKeeper,
    PlanningRunner,
    RegistryPlannerAdapterFactory,
)
from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    PlannerAdapter,
)
from sidera_control_plane.domain.modules import (
    IdempotencyCapability,
    PlannerAdapterCapabilityProfile,
)
from sidera_control_plane.domain.planning import (
    PlanningBriefRevision,
    PlanningRequest,
    PlanningRequestKind,
    PlanningRequestStatus,
    PlanningRunLeaseStatus,
    PlanningRunStatus,
)
from sidera_control_plane.domain.revision_diff import NormalizedRevisionDiff
from tests.unit.test_planning_commands import module_facts, profile

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("70000000-0000-0000-0000-000000000001")


class StaticPlannerFactory:
    def __init__(self, adapter: PlannerAdapter) -> None:
        self.adapter = adapter
        self.calls = 0

    def create(self, definition, instance, profile_snapshot) -> PlannerAdapter:
        self.calls += 1
        return self.adapter


class MutatingPlanner(FakePlannerAdapter):
    def __init__(self, mutation: Callable[[], None]) -> None:
        super().__init__(FakePlannerConfig(scenario="proposal"))
        self._mutation = mutation

    def plan(self, planner_input, control):
        self._mutation()
        return super().plan(planner_input, control)


class MemoryState:
    def __init__(self, request: PlanningRequest) -> None:
        self.request = request
        self.leases = {}
        self.proposals = []
        self.clarifications = []
        self.events = []
        self.commands = []
        self.projection_changes = []
        self.fail_proposal_write = False


class MemoryUow:
    def __init__(self, state: MemoryState) -> None:
        self.state = state
        self.planning_requests = RequestStore(state)
        self.planning_runs = RunStore(state)
        self.planning_leases = LeaseStore(state)
        self.planning_records = RecordStore(state)
        self.modules = ModuleStore()
        self.events = EventStore(state)
        self.commands = CommandStore(state)
        self.projection_changes = ProjectionStore(state)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class RequestStore:
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    def get(self, request_id, workspace_id, *, for_update=False):
        request = self.state.request
        if request.id == request_id and request.workspace_id == workspace_id:
            return request
        return None

    def save(self, request, previous_version):
        return None


class RunStore:
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    def claim_queued(self, *, now, limit):
        return tuple(
            run
            for run in self.state.request.runs
            if run.status is PlanningRunStatus.QUEUED
            and run.queued_at <= now
        )[:limit]

    def get(self, run_id, workspace_id, *, for_update=False):
        return next(
            (
                run
                for run in self.state.request.runs
                if run.id == run_id and run.workspace_id == workspace_id
            ),
            None,
        )

    def save(self, run, previous_version):
        return None

    def add(self, run):
        self.state.request.runs = (*self.state.request.runs, run)


class LeaseStore:
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    def add(self, lease):
        self.state.leases[lease.id] = lease

    def get(self, lease_id, workspace_id, *, for_update=False):
        lease = self.state.leases.get(lease_id)
        if lease is not None and lease.workspace_id == workspace_id:
            return lease
        return None

    def get_active_for_run(self, run_id, workspace_id, *, for_update=False):
        return next(
            (
                lease
                for lease in self.state.leases.values()
                if lease.planning_run_id == run_id
                and lease.workspace_id == workspace_id
                and lease.status is PlanningRunLeaseStatus.ACTIVE
            ),
            None,
        )

    def save(self, lease, previous_version):
        self.state.leases[lease.id] = lease


class RecordStore:
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    def add_proposal(self, proposal, workspace_id):
        if self.state.fail_proposal_write:
            raise RuntimeError("proposal store unavailable")
        self.state.proposals.append(proposal)

    def list_proposals(self, request_id, workspace_id):
        return tuple(self.state.proposals)

    def add_clarification(self, clarification):
        self.state.clarifications.append(clarification)


class ModuleStore:
    def get_definition(self, module_id, workspace_id):
        definition, _ = module_facts()
        return definition

    def get_instance(self, module_id, workspace_id):
        _, instance = module_facts()
        return instance


class EventStore:
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    def append_with_outbox(self, event):
        self.state.events.append(event)


class CommandStore:
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    def record_applied(self, envelope, request_hash, result):
        self.state.commands.append((envelope, result))


class ProjectionStore:
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    def changed(self, **kwargs):
        self.state.projection_changes.append(kwargs)


def _request() -> PlanningRequest:
    definition, instance = module_facts()
    assert isinstance(definition.capabilities, PlannerAdapterCapabilityProfile)
    snapshot = profile(instance.id).snapshot(definition.capabilities)
    return PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        profile_snapshot=snapshot,
        objective="Build a recoverable planner worker",
        constraints=("Keep the API request non-blocking",),
        acceptance_criteria=("The proposal is durable",),
        context_refs=("repo://sidera",),
        requested_outputs=("proposal",),
        source_type="web",
        source_ref="conversation:1",
        actor="operator",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )


def _runner(state: MemoryState, adapter: PlannerAdapter) -> PlanningRunner:
    factory = StaticPlannerFactory(adapter)
    return PlanningRunner(
        uow_factory=cast(
            Callable[[], ControlPlaneUnitOfWork], lambda: MemoryUow(state)
        ),
        adapter_factory=cast(PlannerAdapterFactory, factory),
        owner_id="planning-worker:test",
        lease_ttl_seconds=60,
        lease_renew_interval_seconds=10,
        clock=lambda: NOW,
    )


def test_registry_does_not_fallback_for_an_unknown_provider() -> None:
    definition, instance = module_facts()
    assert isinstance(definition.capabilities, PlannerAdapterCapabilityProfile)
    snapshot = profile(instance.id).snapshot(definition.capabilities)
    factory = RegistryPlannerAdapterFactory(
        {"fake": lambda config, secrets: FakePlannerAdapter(
            FakePlannerConfig(scenario="proposal")
        )}
    )

    with pytest.raises(PlanningAdapterUnavailable) as exc_info:
        factory.create(
            replace(definition, provider="unknown-provider"),
            instance,
            snapshot,
        )

    assert exc_info.value.code == "planner_provider_unsupported"
    assert exc_info.value.blocked is True


def test_registry_secret_failure_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    definition, instance = module_facts()
    assert isinstance(definition.capabilities, PlannerAdapterCapabilityProfile)
    snapshot = replace(
        profile(instance.id).snapshot(definition.capabilities),
        planner_secret_refs=("env:PLANNING_TEST_MISSING",),
    )
    monkeypatch.delenv("PLANNING_TEST_MISSING", raising=False)
    factory = RegistryPlannerAdapterFactory(
        {"fake": lambda config, secrets: FakePlannerAdapter(
            FakePlannerConfig(scenario="proposal")
        )}
    )

    with pytest.raises(PlanningAdapterUnavailable) as exc_info:
        factory.create(definition, instance, snapshot)

    assert exc_info.value.code == "planning_profile_unavailable"
    assert "PLANNING_TEST_MISSING" not in str(exc_info.value)


def test_unknown_outcome_without_operation_id_is_blocked() -> None:
    request = _request()
    request.profile_snapshot = replace(
        request.profile_snapshot,
        planner_capabilities=replace(
            request.profile_snapshot.planner_capabilities,
            idempotency=IdempotencyCapability.NONE,
        ),
    )
    state = MemoryState(request)

    result = _runner(
        state,
        FakePlannerAdapter(FakePlannerConfig(scenario="unknown_outcome")),
    ).run_once(limit=10)

    assert result.blocked == 1
    assert result.failed == 0
    assert state.request.runs[-1].status is PlanningRunStatus.BLOCKED


def test_cancelled_request_rejects_a_late_proposal() -> None:
    state = MemoryState(_request())
    adapter = MutatingPlanner(lambda: state.request.cancel(now=NOW))

    result = _runner(state, adapter).run_once(limit=10)

    assert result.succeeded == 0
    assert state.proposals == []
    assert state.request.status is PlanningRequestStatus.CANCELLED


def test_replaced_fencing_token_rejects_a_late_proposal() -> None:
    state = MemoryState(_request())

    def replace_fence() -> None:
        lease = next(iter(state.leases.values()))
        lease.fencing_token += 1
        lease.version += 1

    result = _runner(state, MutatingPlanner(replace_fence)).run_once(limit=10)

    assert result.succeeded == 0
    assert state.proposals == []
    assert state.request.status is PlanningRequestStatus.PLANNING


def test_runner_claims_calls_fake_and_persists_proposal_atomically() -> None:
    state = MemoryState(_request())
    adapter = FakePlannerAdapter(FakePlannerConfig(scenario="proposal"))

    result = _runner(state, adapter).run_once(limit=10)

    assert isinstance(result, PlanningBatchResult)
    assert result.claimed == 1
    assert result.succeeded == 1
    assert result.clarifications == 0
    assert result.failed == 0
    assert state.request.status is PlanningRequestStatus.PROPOSAL_READY
    assert state.request.runs[-1].status is PlanningRunStatus.SUCCEEDED
    assert len(state.proposals) == 1
    assert len(state.events) >= 2
    assert next(iter(state.leases.values())).status is PlanningRunLeaseStatus.RELEASED


def test_fake_planner_emits_v3_for_a_revision_context() -> None:
    from tests.unit.test_plan_revisions import profile_snapshot
    from tests.unit.test_revision_planning_context import _build_context

    context = _build_context()
    request_id = uuid4()
    brief = PlanningBriefRevision.create(
        planning_request_id=request_id,
        revision=1,
        objective="Revise the running plan",
        constraints=(),
        acceptance_criteria=("The revision is reviewable",),
        context_refs=(),
        requested_outputs=("revision plan",),
        user_feedback=None,
        source_correlation=uuid4(),
        created_by="operator",
        created_at=NOW,
    )
    planner_profile = profile_snapshot()
    planner_input = PlannerInputAssembler().assemble(
        brief=brief,
        profile_snapshot=planner_profile,
        capability_catalog=CapabilityCatalogSnapshot(
            snapshot_version=1,
            captured_at=NOW,
            capabilities=(),
            adapter_preferences=(),
            budget_level="standard",
            max_concurrency=2,
            workspace_providers=(),
        ),
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(
            planner_profile
        ),
        operation_id=uuid4(),
        planning_request_id=request_id,
        planning_run_id=uuid4(),
        planning_context=context,
        kind=PlanningRequestKind.REVISION,
    )

    result = FakePlannerAdapter(
        FakePlannerConfig(scenario="revision")
    ).plan(
        planner_input,
        PlannerRunControl(
            operation_id=planner_input.operation_id,
            started_at=NOW,
            deadline_at=NOW + timedelta(minutes=1),
        ),
    )

    assert isinstance(result, PlannerProposalResult)
    assert result.proposal.payload.schema_version == 3
    assert isinstance(result.proposal.derived_diff, NormalizedRevisionDiff)
    assert result.proposal.kind is PlanningRequestKind.REVISION


def test_runner_does_not_mark_proposal_ready_when_proposal_write_fails() -> None:
    state = MemoryState(_request())
    state.fail_proposal_write = True
    adapter = FakePlannerAdapter(FakePlannerConfig(scenario="proposal"))

    result = _runner(state, adapter).run_once(limit=10)

    assert result.failed == 1
    assert state.request.status is not PlanningRequestStatus.PROPOSAL_READY
    assert state.request.status is PlanningRequestStatus.FAILED
    assert state.proposals == []


def test_runner_maps_clarification_without_persisting_a_proposal() -> None:
    state = MemoryState(_request())
    adapter = FakePlannerAdapter(FakePlannerConfig(scenario="clarification"))

    result = _runner(state, adapter).run_once(limit=10)

    assert result.clarifications == 1
    assert result.succeeded == 0
    assert state.request.status is PlanningRequestStatus.WAITING_FOR_USER
    assert state.request.runs[-1].status is PlanningRunStatus.NEEDS_CLARIFICATION
    assert state.proposals == []
    assert len(state.clarifications) == 1


def test_lease_keeper_renews_with_an_independent_uow() -> None:
    state = MemoryState(_request())
    run = state.request.runs[-1]
    run.start(lease_id=uuid4(), fencing_token=1, now=NOW)
    from sidera_control_plane.domain.planning import PlanningRunLease

    lease = PlanningRunLease(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        planning_run_id=run.id,
        owner_id="planning-worker:test",
        status=PlanningRunLeaseStatus.ACTIVE,
        fencing_token=1,
        acquired_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
        released_at=None,
        version=1,
    )
    state.leases[lease.id] = lease

    keeper = PlanningLeaseKeeper(
        uow_factory=cast(
            Callable[[], ControlPlaneUnitOfWork], lambda: MemoryUow(state)
        ),
        lease_id=lease.id,
        planning_run_id=run.id,
        workspace_id=WORKSPACE_ID,
        owner_id="planning-worker:test",
        fencing_token=1,
        lease_ttl_seconds=60,
        clock=lambda: NOW + timedelta(seconds=5),
    )

    assert keeper.renew_once() is True
    assert state.leases[lease.id].expires_at == NOW + timedelta(seconds=65)


@pytest.mark.parametrize("scenario", ["timeout", "unknown_outcome"])
def test_runner_persists_structured_failures(scenario: str) -> None:
    state = MemoryState(_request())
    adapter = FakePlannerAdapter(
        FakePlannerConfig(scenario=cast(object, scenario))  # type: ignore[arg-type]
    )

    result = _runner(state, adapter).run_once(limit=10)

    assert result.failed == 1 or result.blocked == 1
    assert state.request.status is PlanningRequestStatus.FAILED
    assert state.request.runs[-1].error_code in {"timeout", "unknown_outcome"}
