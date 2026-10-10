from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
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
from tests.unit.test_agent_planner import (
    native_snapshot,
    native_start_case,  # noqa: F401
    planner_drain_case,  # noqa: F401
)
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
            if run.status is PlanningRunStatus.QUEUED and run.queued_at <= now
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


def _request(*, profile_snapshot=None) -> PlanningRequest:
    definition, instance = module_facts()
    assert isinstance(definition.capabilities, PlannerAdapterCapabilityProfile)
    snapshot = profile_snapshot or profile(instance.id).snapshot(
        definition.capabilities
    )
    return PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=snapshot.workspace_id,
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
        {
            "fake": lambda config, secrets: FakePlannerAdapter(
                FakePlannerConfig(scenario="proposal")
            )
        }
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
        {
            "fake": lambda config, secrets: FakePlannerAdapter(
                FakePlannerConfig(scenario="proposal")
            )
        }
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

    result = FakePlannerAdapter(FakePlannerConfig(scenario="revision")).plan(
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


@pytest.fixture
def native_result_case(request):
    from sidera_agents.contracts import EventPage, RunEvent

    from sidera_control_plane.application.agent_planner_adapter import (
        AgentPlannerAdapter,
    )
    from sidera_control_plane.application.agent_worker_adapter import (
        request_fingerprint,
    )
    from sidera_control_plane.domain.context_packages import (
        ContextAssemblyStatus,
        ContextPackageStatus,
    )

    original = request.getfixturevalue("planner_drain_case")
    snapshot = replace(
        native_snapshot(_request().profile_snapshot),
        workspace_id=original.authority.workspace_id,
        planner_agent_ref=original.authority.profile_ref,
    )
    state = MemoryState(_request(profile_snapshot=snapshot))
    state.owner = True
    state.authority = None
    state.link = None
    state.uow_calls = 0
    state.recover_calls = 0
    state.observations = []
    package = SimpleNamespace(
        status=ContextPackageStatus.ACTIVE,
        expires_at=NOW + timedelta(minutes=1),
        work_context_revision_id=uuid4(),
    )

    def save_link(link, **changes):
        assert link == state.link
        state.link = replace(link, **changes)
        return state.link

    def save_observation(link, *, event_cursor, response_id, closed):
        state.observations.append((event_cursor, response_id, closed))
        return save_link(
            link,
            event_cursor=event_cursor,
            response_id=response_id,
            status="closed" if closed else link.status,
            closed=closed,
        )

    @contextmanager
    def owned_uow():
        state.uow_calls += 1
        uow = MemoryUow(state)
        uow.context_assemblies = SimpleNamespace(
            get_for_consumer=lambda *args: SimpleNamespace(
                status=ContextAssemblyStatus.READY
            )
        )
        uow.context_packages = SimpleNamespace(get_for_consumer=lambda *args: package)
        uow.work_contexts = SimpleNamespace(
            get_revision=lambda *args: SimpleNamespace()
        )
        uow.planning_authorities = SimpleNamespace(
            get=lambda authority_id, **kwargs: (
                state.authority
                if state.authority is not None and authority_id == state.authority.id
                else None
            ),
            get_link=lambda workspace_id, run_id, **kwargs: (
                state.link
                if (workspace_id, run_id)
                == (state.request.workspace_id, state.request.runs[-1].id)
                else None
            ),
            save_link=save_link,
            save_original_observation=save_observation,
        )
        yield uow

    def install_original():
        run = state.request.runs[-1]
        lease = next(iter(state.leases.values()))
        start = original.state.link.start_request.model_copy(
            update={"operation_id": run.operation_id}
        )
        state.authority = replace(
            original.authority,
            planning_request_id=state.request.id,
            planning_run_id=run.id,
            planning_profile_id=snapshot.profile_id,
            planning_profile_version=snapshot.profile_version,
            operation_id=run.operation_id,
            lease_id=lease.id,
            owner_id="planner",
            fencing_token=lease.fencing_token,
            actions=("run.read", "run.stop", "profile.read"),
        )
        state.link = replace(
            original.state.link,
            planning_run_id=run.id,
            start_operation_id=run.operation_id,
            start_request=start,
            request_sha256=request_fingerprint(start),
            start_receipt=original.state.link.start_receipt.model_copy(
                update={"operation_id": run.operation_id}
            ),
        )
        original.state.snapshot = type(original.state.snapshot).model_validate(
            {
                **original.state.snapshot.model_dump(),
                "state": "completed",
                "last_event_sequence": 4,
            }
        )
        original.state.events = tuple(
            RunEvent(
                run_id=state.link.run_id,
                sequence=sequence,
                kind=kind,
                occurred_at=NOW,
                operation_id=run.operation_id,
                response_id=original.response_id if sequence < 4 else None,
                data=data,
            )
            for sequence, kind, data in (
                (1, "response.started", {}),
                (
                    2,
                    "message",
                    {
                        "update_type": "agent_message",
                        "text": '{"result_type":"clarification","clarifications":[{"question":"Which output?","rationale":"Missing output","answer_type":"text","options":[],"blocking":true}]}',
                    },
                ),
                (3, "response.completed", {}),
                (4, "run.closed", {"status": "completed"}),
            )
        )
        return state.link

    def events(workspace_id, run_id, cursor, limit=100, *, grant_id):
        assert (workspace_id, run_id, grant_id) == (
            state.link.workspace_id,
            state.link.run_id,
            state.link.authority_id,
        )
        visible = tuple(
            event for event in original.state.events if event.sequence > cursor
        )[:limit]
        return EventPage(
            events=visible,
            next_sequence=visible[-1].sequence if visible else cursor,
            has_more=False,
        )

    services = original.adapter._services
    services.execution.list_events = events
    adapter = AgentPlannerAdapter(
        services=services,
        uow_factory=owned_uow,
        workspace_provider=SimpleNamespace(),
        materializer=None,
        authorizer=SimpleNamespace(require=lambda *args: None),
        clock=lambda: NOW,
        owner_check=lambda: state.owner,
    )
    factory = StaticPlannerFactory(adapter)
    runner = PlanningRunner(
        uow_factory=owned_uow,
        adapter_factory=factory,
        owner_id="planner",
        lease_renew_interval_seconds=0,
        clock=lambda: NOW,
        owner_check=lambda: state.owner,
    )
    return SimpleNamespace(
        runner=runner,
        adapter=adapter,
        factory=factory,
        state=state,
        original=original,
        install_original=install_original,
    )


def _claim_native_original(case):
    batch = case.runner._claim(1)
    assert len(batch.items) == 1
    case.install_original()
    return batch


def test_native_live_result_owns_recovery_until_persistence(
    native_result_case, monkeypatch
):
    case = native_result_case
    claim = _claim_native_original(case)
    monkeypatch.setattr(case.runner, "_claim", lambda limit: claim)
    live_plan = case.adapter.plan
    recover = case.adapter.recover_original_result
    persist = case.runner._persist_result
    seen = []

    def observe_recovery(planner_input):
        case.state.recover_calls += 1
        return recover(planner_input)

    def competing_recovery(planner_input, control):
        before = case.state.uow_calls
        result = case.runner.reconcile_original_run(case.state.link)
        seen.append((result, case.state.uow_calls - before))
        return live_plan(planner_input, control)

    def persist_after_probe(item, result):
        before = case.state.uow_calls
        recovered = case.runner.reconcile_original_run(case.state.link)
        seen.append((recovered, case.state.uow_calls - before))
        return persist(item, result)

    monkeypatch.setattr(case.adapter, "recover_original_result", observe_recovery)
    monkeypatch.setattr(case.adapter, "plan", competing_recovery)
    monkeypatch.setattr(case.runner, "_persist_result", persist_after_probe)
    batch = case.runner.run_once(1)
    assert seen == [(None, 0), (None, 0)]
    assert batch.clarifications == 1 and len(case.state.clarifications) == 1
    assert case.state.recover_calls == 0 and case.state.observations == []
    assert case.factory.calls == 1
    assert not case.original.state.starts and not case.original.state.stops
    before = case.state.uow_calls
    assert case.runner.reconcile_original_run(case.state.link) is None
    assert case.state.uow_calls > before


def test_native_outer_failure_persistence_keeps_result_ownership(
    native_result_case, monkeypatch
):
    case = native_result_case
    claim = _claim_native_original(case)
    monkeypatch.setattr(case.runner, "_claim", lambda limit: claim)
    persist = case.runner._persist_failure
    seen = []

    def fail_execute(item):
        raise RuntimeError("controlled outer execution failure")

    def persist_after_probe(item, failure, **kwargs):
        before = case.state.uow_calls
        result = case.runner.reconcile_original_run(case.state.link)
        seen.append((result, case.state.uow_calls - before))
        return persist(item, failure, **kwargs)

    monkeypatch.setattr(case.runner, "_execute", fail_execute)
    monkeypatch.setattr(case.runner, "_persist_failure", persist_after_probe)
    batch = case.runner.run_once(1)
    assert seen == [(None, 0)]
    assert batch.failed == 1 and batch.outcomes[0].code == "planning_worker_error"
    assert case.state.observations == [] and case.factory.calls == 0
    before = case.state.uow_calls
    assert case.runner.reconcile_original_run(case.state.link) is None
    assert case.state.uow_calls > before


def test_native_duplicate_recovery_skips_business_read_and_releases(
    native_result_case, monkeypatch
):
    case = native_result_case
    _claim_native_original(case)
    recover = case.adapter.recover_original_result
    seen = []
    entered = False

    def recover_after_probe(planner_input):
        nonlocal entered
        if not entered:
            entered = True
            before = case.state.uow_calls
            result = case.runner.reconcile_original_run(case.state.link)
            seen.append((result, case.state.uow_calls - before))
        case.state.recover_calls += 1
        return recover(planner_input)

    monkeypatch.setattr(case.adapter, "recover_original_result", recover_after_probe)
    outcome = case.runner.reconcile_original_run(case.state.link)
    assert seen == [(None, 0)]
    assert outcome.status is PlanningRunStatus.NEEDS_CLARIFICATION
    assert case.state.recover_calls == 1 and len(case.state.clarifications) == 1
    assert len(case.state.observations) == 1 and case.factory.calls == 1
    before = case.state.uow_calls
    assert case.runner.reconcile_original_run(case.state.link) is None
    assert case.state.uow_calls > before
    assert not case.original.state.starts and not case.original.state.stops


@pytest.mark.parametrize("different", ("run", "workspace"))
def test_native_other_key_recovery_enters_business_read(
    native_result_case, monkeypatch, different
):
    from sidera_control_plane.application.agent_worker_adapter import (
        request_fingerprint,
    )
    from sidera_control_plane.domain.planning import planning_operation_id

    case = native_result_case
    _claim_native_original(case)
    other_request = replace(
        case.state.request,
        runs=tuple(replace(run) for run in case.state.request.runs),
    )
    other_run = other_request.runs[-1]
    if different == "run":
        other_run.id = uuid4()
    else:
        other_request.workspace_id = other_run.workspace_id = uuid4()
        other_request.profile_snapshot = other_run.profile_snapshot = replace(
            other_run.profile_snapshot,
            workspace_id=other_request.workspace_id,
            planner_agent_ref=other_run.profile_snapshot.planner_agent_ref.model_copy(
                update={"workspace_id": other_request.workspace_id}
            ),
        )
    other_run.operation_id = planning_operation_id(other_run.id, other_run.attempt)
    other_lease = replace(
        next(iter(case.state.leases.values())),
        id=uuid4(),
        workspace_id=other_request.workspace_id,
        planning_run_id=other_run.id,
    )
    other_run.lease_id = other_lease.id
    other_authority = replace(
        case.state.authority,
        id=uuid4(),
        workspace_id=other_request.workspace_id,
        planning_run_id=other_run.id,
        operation_id=other_run.operation_id,
        lease_id=other_lease.id,
        profile_ref=other_run.profile_snapshot.planner_agent_ref,
        runtime_ref=case.state.authority.runtime_ref.model_copy(
            update={"workspace_id": other_request.workspace_id}
        ),
    )
    start = case.state.link.start_request.model_copy(
        update={
            "operation_id": other_run.operation_id,
            "grant_id": other_authority.id,
            "profile_ref": other_authority.profile_ref,
        }
    )
    other_native_run_id = uuid4()
    other_link = replace(
        case.state.link,
        workspace_id=other_request.workspace_id,
        planning_run_id=other_run.id,
        authority_id=other_authority.id,
        start_operation_id=other_run.operation_id,
        profile_ref=other_authority.profile_ref,
        start_request=start,
        request_sha256=request_fingerprint(start),
        run_id=other_native_run_id,
        start_receipt=case.state.link.start_receipt.model_copy(
            update={
                "operation_id": other_run.operation_id,
                "run_id": other_native_run_id,
            }
        ),
    )
    other_events = tuple(
        event.model_copy(
            update={
                "run_id": other_native_run_id,
                "operation_id": other_run.operation_id,
            }
        )
        for event in case.original.state.events
    )
    other_snapshot = case.original.state.snapshot.model_copy(
        update={
            "run_id": other_native_run_id,
            "workspace_id": other_request.workspace_id,
            "profile_ref": other_authority.profile_ref,
            "effective_config": case.original.state.snapshot.effective_config.model_copy(
                update={
                    "profile_ref": other_authority.profile_ref,
                    "runtime_ref": other_authority.runtime_ref,
                }
            ),
        }
    )
    recover = case.adapter.recover_original_result
    seen = []
    entered = False

    def recover_after_probe(planner_input):
        nonlocal entered
        if not entered:
            entered = True
            saved = (
                case.state.request,
                case.state.leases,
                case.state.authority,
                case.state.link,
                case.original.state.events,
                case.original.state.snapshot,
            )
            try:
                case.state.request, case.state.leases = (
                    other_request,
                    {other_lease.id: other_lease},
                )
                case.state.authority, case.state.link = other_authority, other_link
                case.original.state.events, case.original.state.snapshot = (
                    other_events,
                    other_snapshot,
                )
                seen.append(case.runner.reconcile_original_run(other_link))
                assert other_lease.status is PlanningRunLeaseStatus.RELEASED
            finally:
                (
                    case.state.request,
                    case.state.leases,
                    case.state.authority,
                    case.state.link,
                    case.original.state.events,
                    case.original.state.snapshot,
                ) = saved
        return recover(planner_input)

    monkeypatch.setattr(case.adapter, "recover_original_result", recover_after_probe)
    assert (
        case.runner.reconcile_original_run(case.state.link).status
        is PlanningRunStatus.NEEDS_CLARIFICATION
    )
    assert len(seen) == 1 and seen[0].run_id == other_run.id
    assert seen[0].status is PlanningRunStatus.NEEDS_CLARIFICATION
    assert len(case.state.clarifications) == 2 and len(case.state.observations) == 2
    assert not case.original.state.starts and not case.original.state.stops


@pytest.mark.parametrize("ending", ("success", "early", "exception"))
def test_native_recovery_releases_slot_on_every_exit(
    native_result_case, monkeypatch, ending
):
    case = native_result_case
    _claim_native_original(case)
    authority = case.state.authority
    events = case.adapter._services.execution.list_events
    if ending == "early":
        case.state.authority = None
        assert case.runner.reconcile_original_run(case.state.link) is None
        case.state.authority = authority
    elif ending == "exception":

        def fail_read(*args, **kwargs):
            raise RuntimeError("controlled original read failure")

        monkeypatch.setattr(case.adapter._services.execution, "list_events", fail_read)
        with pytest.raises(RuntimeError, match="controlled original read failure"):
            case.runner.reconcile_original_run(case.state.link)
        monkeypatch.setattr(case.adapter._services.execution, "list_events", events)
    else:
        assert (
            case.runner.reconcile_original_run(case.state.link).status
            is PlanningRunStatus.NEEDS_CLARIFICATION
        )
    before = case.state.uow_calls
    outcome = case.runner.reconcile_original_run(case.state.link)
    assert case.state.uow_calls > before
    if ending != "success":
        assert outcome.status is PlanningRunStatus.NEEDS_CLARIFICATION
    assert not case.original.state.starts and not case.original.state.stops


def test_native_busy_recovery_still_requires_gateway_owner(
    native_result_case, monkeypatch
):
    from sidera_agents.errors import AgentError

    case = native_result_case
    _claim_native_original(case)
    recover = case.adapter.recover_original_result

    def lose_owner_during_recovery(planner_input):
        before = case.state.uow_calls
        case.state.owner = False
        try:
            with pytest.raises(AgentError) as exc_info:
                case.runner.reconcile_original_run(case.state.link)
            assert exc_info.value.code == "gateway_owner_lost"
            assert case.state.uow_calls == before
        finally:
            case.state.owner = True
        return recover(planner_input)

    monkeypatch.setattr(
        case.adapter, "recover_original_result", lose_owner_during_recovery
    )
    assert (
        case.runner.reconcile_original_run(case.state.link).status
        is PlanningRunStatus.NEEDS_CLARIFICATION
    )


def test_native_duplicate_claim_does_not_execute_or_persist_failure(
    native_result_case, monkeypatch
):
    case = native_result_case
    claim = _claim_native_original(case)
    monkeypatch.setattr(case.runner, "_claim", lambda limit: claim)
    live_plan = case.adapter.plan
    duplicate = []

    def plan_after_duplicate(planner_input, control):
        if not duplicate:
            duplicate.append(None)
            duplicate[0] = case.runner.run_once(1)
        return live_plan(planner_input, control)

    monkeypatch.setattr(case.adapter, "plan", plan_after_duplicate)
    result = case.runner.run_once(1)
    outcome = duplicate[0].outcomes[0]
    assert (outcome.status, outcome.code, outcome.attempt, outcome.run_id) == (
        "stale",
        "native_original_active",
        claim.items[0].attempt,
        claim.items[0].run_id,
    )
    assert result.clarifications == 1 and len(case.state.clarifications) == 1
    assert case.factory.calls == 1 and not case.state.observations
    assert not case.original.state.starts and not case.original.state.stops


def test_native_live_persistence_exception_releases_for_original_recovery(
    native_result_case, monkeypatch
):
    case = native_result_case
    claim = _claim_native_original(case)
    monkeypatch.setattr(case.runner, "_claim", lambda limit: claim)

    def fail_persistence(*args, **kwargs):
        raise RuntimeError("controlled business persistence failure")

    with monkeypatch.context() as patch:
        patch.setattr(case.runner, "_persist_result", fail_persistence)
        patch.setattr(case.runner, "_persist_failure", fail_persistence)
        with pytest.raises(
            RuntimeError, match="controlled business persistence failure"
        ):
            case.runner.run_once(1)
    outcome = case.runner.reconcile_original_run(case.state.link)
    assert outcome.status is PlanningRunStatus.NEEDS_CLARIFICATION
    assert len(case.state.clarifications) == 1


@pytest.mark.parametrize("failure", (False, True))
def test_legacy_execution_and_failure_persistence_do_not_acquire_native_slot(
    monkeypatch, failure
):
    state = MemoryState(_request())
    runner = _runner(
        state, FakePlannerAdapter(FakePlannerConfig(scenario="clarification"))
    )

    def forbidden_slot(*args):
        pytest.fail("legacy work entered the native result slot")

    monkeypatch.setattr(runner, "_native_result_slot", forbidden_slot, raising=False)
    if failure:

        def fail_execute(item):
            raise RuntimeError("controlled legacy execution failure")

        monkeypatch.setattr(runner, "_execute", fail_execute)
    result = runner.run_once(1)
    assert result.failed == int(failure) and result.clarifications == int(not failure)


@pytest.mark.parametrize("stale", ("lease", "profile", "brief", "revoked"))
def test_native_recovery_preserves_original_fences(native_result_case, stale):
    case = native_result_case
    _claim_native_original(case)
    if stale == "lease":
        next(iter(case.state.leases.values())).expires_at = NOW
    elif stale == "profile":
        case.state.request.profile_snapshot = replace(
            case.state.request.profile_snapshot, profile_version=2
        )
    elif stale == "brief":
        case.state.request.runs[-1].brief_revision += 1
    else:
        case.state.authority = replace(case.state.authority, revoked=True)
    assert case.runner.reconcile_original_run(case.state.link) is None
    assert case.factory.calls == 0 and not case.state.observations
    assert not case.original.state.starts and not case.original.state.stops
