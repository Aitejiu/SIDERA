from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.agent_worker_adapter import project_run_event
from sidera_control_plane.application.event_ingestor import (
    EventSequenceConflict,
    EventSequenceGap,
    SessionEventIngestor,
)
from tests.integration import test_agent_planning
from tests.integration.test_agent_dispatch import NativeScenario
from tests.support.native_agents import (
    native_cp_guarded_database,  # noqa: F401
    native_cp_test_cleanup,  # noqa: F401
    native_cp_test_target_guard,  # noqa: F401
)

pytestmark = pytest.mark.integration

native_planning_case = test_agent_planning.native_case


class SimulatedProcessExit(BaseException):
    pass


def interrupted_planning(
    case, uow_factory, tmp_path, monkeypatch, *, partial, lost_ack=False
):
    from sidera_control_plane.application.agent_planner_adapter import (
        AgentPlannerAdapter,
    )
    from sidera_control_plane.application.planning_runner import PlanningRunner
    from tests.integration.test_agent_planning import (
        PublicPlanningExecution,
        native_proposal_body,
        run_native_case,
    )

    captured = {}
    original_execute = PlanningRunner._execute
    original_save = AgentPlannerAdapter._save

    def execute(runner, item):
        captured["runner"] = runner
        return original_execute(runner, item)

    def save(adapter, link, **changes):
        saved = original_save(adapter, link, **changes)
        if partial and saved.event_cursor == 3:
            raise SimulatedProcessExit()
        return saved

    class DurablePlanningExecution(PublicPlanningExecution):
        def start(self, request):
            self.receipt = super().start(request)
            return self.receipt

        def get_start(self, workspace_id, operation_id, *, grant_id):
            from sidera_agents.contracts import StartLookup

            start = self.requests[-1]
            assert (workspace_id, operation_id, grant_id) == (
                start.profile_ref.workspace_id,
                start.operation_id,
                start.grant_id,
            )
            self.lookups.append((workspace_id, operation_id, grant_id))
            return StartLookup(found=True, receipt=self.receipt)

        def list_events(self, *args, **kwargs):
            kwargs["limit"] = 3 if partial else 100
            return super().list_events(*args, **kwargs)

    def execution_factory(runtime_ref, body):
        captured["execution"] = DurablePlanningExecution(runtime_ref, body)
        return captured["execution"]

    def crash_before_result(*args):
        raise SimulatedProcessExit()

    with monkeypatch.context() as patch:
        patch.setattr(PlanningRunner, "_execute", execute)
        patch.setattr(PlanningRunner, "_persist_result", crash_before_result)
        patch.setattr(AgentPlannerAdapter, "_save", save)
        if lost_ack:
            patch.setattr(AgentPlannerAdapter, "_record_receipt", crash_before_result)
        with pytest.raises(SimulatedProcessExit):
            run_native_case(
                case,
                uow_factory,
                tmp_path,
                native_proposal_body(case[-1]),
                execution_factory=execution_factory,
            )
    with uow_factory() as uow:
        submitted = case[-2].handle(case[-1])
        request = uow.planning_requests.get(
            UUID(submitted.data["planning_request_id"]), case[-1].workspace_id
        )
    return captured["runner"], captured["execution"], request


def restart_planning(
    case,
    old_runner,
    uow_factory,
    tmp_path,
    *,
    owner_check=None,
    clock=None,
    renew_interval=10.0,
):
    from sidera_control_plane.adapters.planning_workspace import (
        PrivatePlanningWorkspaceProvider,
    )
    from sidera_control_plane.application.agent_worker_adapter import (
        NativeWorkerServices,
    )
    from sidera_control_plane.application.context_materialization import (
        ContextMaterializer,
    )
    from sidera_control_plane.application.execution_authority import (
        NativeAuthorityReader,
        PlanningAuthorityReader,
    )
    from sidera_control_plane.application.planning_runner import (
        PlanningRunner,
        RegistryPlannerAdapterFactory,
    )
    from tests.integration.test_context_package_worker import EmptyCatalog

    _, identity, scope, catalog, runtimes, _, _ = case
    public = old_runner._adapter_factory._native_options["services"].execution
    reader = PlanningAuthorityReader(
        uow_factory,
        principal=lambda: identity.principal_id,
        authorizer=scope,
        clock=clock,
    )
    services = NativeWorkerServices(
        catalog,
        runtimes,
        public,
        NativeAuthorityReader(uow_factory, planner=reader, worker=None),
        identity.principal_id,
        identity.grant_id,
    )
    provider = PrivatePlanningWorkspaceProvider(tmp_path / "scratch")
    materializer = ContextMaterializer(EmptyCatalog())
    factory = RegistryPlannerAdapterFactory(
        {},
        native_services=services,
        uow_factory=uow_factory,
        planning_workspace_provider=provider,
        planning_context_materializer=materializer,
        planning_authorizer=scope,
        native_clock=clock,
        owner_check=owner_check,
    )
    runner = PlanningRunner(
        uow_factory=uow_factory,
        adapter_factory=factory,
        owner_id="native-planner",
        context_materializer=materializer,
        clock=clock,
        owner_check=owner_check,
        lease_renew_interval_seconds=renew_interval,
    )
    assert runner is not old_runner
    assert (
        provider
        is not old_runner._adapter_factory._native_options["workspace_provider"]
    )
    from sidera_control_plane.application.agent_reconciliation import AgentRunReconciler

    return AgentRunReconciler(
        uow_factory,
        gateway=None,
        services=services,
        planning_runner=runner,
        clock=clock,
        owner_check=owner_check,
    )


@pytest.mark.parametrize("partial", [True, False])
def test_restart_recovers_original_planning_result_once(
    native_planning_case, uow_factory, tmp_path, monkeypatch, partial
):
    from sidera_control_plane.persistence.planning_repository import (
        PlanningRequestRepository,
    )

    old, public, before = interrupted_planning(
        native_planning_case, uow_factory, tmp_path, monkeypatch, partial=partial
    )
    run_id = before.current_run_id
    with uow_factory() as uow:
        original = uow.planning_authorities.get_link(before.workspace_id, run_id)
        assert original.event_cursor == (3 if partial else 5)
        assert original.closed is not partial
        assert not uow.planning_records.list_proposals(before.id, before.workspace_id)
    restart = restart_planning(native_planning_case, old, uow_factory, tmp_path)

    def fail_commit(*args, **kwargs):
        raise RuntimeError("injected recovered proposal commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(PlanningRequestRepository, "save", fail_commit)
        failed = restart.run_once(before.workspace_id)
    assert failed.recovery_required == 1
    with uow_factory() as uow:
        assert uow.planning_requests.get(before.id, before.workspace_id) == before
        assert not uow.planning_records.list_proposals(before.id, before.workspace_id)
    observed_errors = []
    original_recovery = restart._planning_runner.reconcile_original_run

    def checked_recovery(link):
        try:
            return original_recovery(link)
        except Exception as exc:
            observed_errors.append((type(exc).__name__, str(exc)))
            raise

    monkeypatch.setattr(
        restart._planning_runner, "reconcile_original_run", checked_recovery
    )
    outcome = restart.run_once(before.workspace_id)
    assert not observed_errors, observed_errors
    assert outcome.recovery_required == 0
    with uow_factory() as uow:
        saved = uow.planning_requests.get(before.id, before.workspace_id)
        proposals = uow.planning_records.list_proposals(before.id, before.workspace_id)
        link = uow.planning_authorities.get_link(before.workspace_id, run_id)
    assert saved.run(run_id).status.value == "succeeded"
    assert len(saved.runs) == len(proposals) == 1
    assert link.run_id == original.run_id == public.run_id
    assert (link.authority_id, link.start_operation_id, link.profile_ref) == (
        original.authority_id,
        original.start_operation_id,
        original.profile_ref,
    )
    assert link.closed and link.event_cursor == 5
    assert 0 in public.cursors[1:]
    assert len(public.requests) == 1 and not public.stops and not public.lookups
    replay = restart.run_once(before.workspace_id)
    assert replay.observed == replay.recovery_required == 0
    with uow_factory() as uow:
        assert uow.planning_requests.get(before.id, before.workspace_id) == saved
        assert (
            uow.planning_records.list_proposals(before.id, before.workspace_id)
            == proposals
        )


@pytest.mark.parametrize(
    "mismatch", ["owner", "fence", "expired", "run", "workspace", "brief", "request"]
)
def test_original_planning_recovery_rejects_stale_durable_lineage(
    native_planning_case, uow_factory, tmp_path, monkeypatch, mismatch
):
    from datetime import UTC, timedelta

    from sidera_control_plane.persistence.planning_repository import (
        PlanningRequestRepository,
        PlanningRunLeaseRepository,
    )

    old, public, before = interrupted_planning(
        native_planning_case, uow_factory, tmp_path, monkeypatch, partial=False
    )
    restart = restart_planning(native_planning_case, old, uow_factory, tmp_path)
    lease_get = PlanningRunLeaseRepository.get
    request_get = PlanningRequestRepository.get

    def lease_read(repo, *args, **kwargs):
        lease = lease_get(repo, *args, **kwargs)
        changes = {
            "owner": {"owner_id": "replacement-owner"},
            "fence": {"fencing_token": lease.fencing_token + 1},
            "expired": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
            "run": {"planning_run_id": uuid4()},
            "workspace": {"workspace_id": uuid4()},
        }
        return replace(lease, **changes.get(mismatch, {}))

    def request_read(repo, *args, **kwargs):
        request = request_get(repo, *args, **kwargs)
        if mismatch == "brief":
            request.current_brief_revision += 1
        elif mismatch == "request":
            request.id = uuid4()
        return request

    with monkeypatch.context() as patch:
        patch.setattr(PlanningRunLeaseRepository, "get", lease_read)
        patch.setattr(PlanningRequestRepository, "get", request_read)
        outcome = restart.run_once(before.workspace_id)
    assert outcome.recovery_required == 1
    with uow_factory() as uow:
        assert uow.planning_requests.get(before.id, before.workspace_id) == before
        assert not uow.planning_records.list_proposals(before.id, before.workspace_id)
        assert (
            uow.planning_authorities.get_link(
                before.workspace_id, before.current_run_id
            ).run_id
            == public.run_id
        )
    assert len(public.requests) == 1 and not public.stops


def test_mixed_fair_selection_recovers_planner_beside_live_worker(
    native_planning_case, database, workspace_id, uow_factory, tmp_path, monkeypatch
):
    old, public, request = interrupted_planning(
        native_planning_case, uow_factory, tmp_path, monkeypatch, partial=False
    )
    planning = restart_planning(native_planning_case, old, uow_factory, tmp_path)
    s = NativeScenario(
        database,
        workspace_id,
        tmp_path,
        principal_id=native_planning_case[1].principal_id,
    )
    s.assign()
    assert s.schedule().assigned == 1
    s.start()
    with uow_factory() as uow:
        worker_authority = uow.execution_authorities.get(s.link().authority_id)
        planning_link = uow.planning_authorities.get_link(
            workspace_id, request.current_run_id
        )
        planning_authority = uow.planning_authorities.get(planning_link.authority_id)
        assert (
            worker_authority.principal_id
            == planning_authority.principal_id
            == s.services.principal_id
        )
        assert worker_authority.id != planning_authority.id
        assert s.link().run_id != planning_link.run_id
    with restarted_gateway(s, monkeypatch) as runtime:
        observer = reconciler(s, runtime)
        observer._planning_runner = planning._planning_runner
        first = observer.run_once(workspace_id, limit=1)
        second = observer.run_once(workspace_id, limit=1)
    assert first.examined == second.examined == 1
    assert first.outcomes[0].execution_id == s.link().execution_id
    assert second.outcomes[0].execution_id == request.current_run_id
    with uow_factory() as uow:
        assert (
            uow.planning_requests.get(request.id, workspace_id)
            .run(request.current_run_id)
            .status.value
            == "succeeded"
        )
        assert len(uow.planning_records.list_proposals(request.id, workspace_id)) == 1
    assert len(public.requests) == len(s.execution.starts) == 1


@pytest.fixture
def native_case(database, workspace_id, tmp_path):
    scenario = NativeScenario(database, workspace_id, tmp_path)
    scenario.assign()
    assert scenario.schedule().assigned == 1
    scenario.start()
    return scenario


def runtime_facts(s):
    link = s.link()
    with s.uow() as uow:
        session = uow.sessions.get_latest_for_execution(
            link.execution_id, s.workspace_id
        )
        events = uow.execution_events.list_for_session(session.id, s.workspace_id)
        authority = uow.execution_authorities.get(link.authority_id)
    return link, session, events, authority


def notification_counts(s):
    from sqlalchemy import func, select

    from sidera_control_plane.persistence.models import (
        OutboxMessageModel,
        ProjectionChangeModel,
    )

    with s.uow() as uow:
        return (
            uow.session.scalar(
                select(func.count())
                .select_from(ProjectionChangeModel)
                .where(ProjectionChangeModel.workspace_id == s.workspace_id)
            ),
            uow.session.scalar(select(func.count()).select_from(OutboxMessageModel)),
        )


def sync(s):
    link, session, _, authority = runtime_facts(s)
    return s.gateway.sync_session(
        workspace_id=s.workspace_id,
        execution_id=link.execution_id,
        session_id=session.id,
        lease_id=authority.lease_id,
        fencing_token=authority.fencing_token,
    )


def original_page(s, events, *, link=None):
    return SessionEventIngestor(s.uow, clock=s.cp.clock).ingest_original_page(
        expected=link or s.link(),
        principal_id=s.principal,
        events=tuple(project_run_event(event) for event in events),
    )


def test_normal_native_sync_commits_both_cursors(native_case):
    s = native_case
    s.execution.emit(s.link().run_id, "message")
    sync(s)
    link, session, events, _ = runtime_facts(s)
    assert link.event_cursor == session.last_sequence == 2
    assert [event.sequence for event in events] == [1, 2]
    assert len(s.execution.starts) == 1


@pytest.mark.parametrize("state", ["live", "unknown", "before_admission"])
def test_generic_lease_reaper_does_not_replace_native_original(
    database, workspace_id, tmp_path, state
):
    from datetime import timedelta

    from sqlalchemy import select

    from sidera_control_plane.application.reconciler import Reconciler
    from sidera_control_plane.persistence.models import ExecutionModel

    s = NativeScenario(database, workspace_id, tmp_path)
    s.assign()
    with s.uow() as uow:
        queued = uow.executions.get(s.cp._execution_id("a"), workspace_id)
        queued.max_attempts = 2
        uow.executions.save(queued, queued.version)
    assert s.schedule().assigned == 1
    if state != "before_admission":
        s.start()
        if state == "unknown":
            s.execution.states[s.link().run_id] = "unknown"
    execution_id = s.cp._execution_id("a")
    with s.uow() as uow:
        original = uow.executions.get(execution_id, workspace_id)
        assert original.attempt < original.max_attempts
        lease = uow.leases.get(original.active_lease_id, workspace_id)
        worker = uow.workers.get(lease.worker_id, workspace_id)
        session = uow.sessions.get_latest_for_execution(execution_id, workspace_id)
        task = uow.tasks.get(original.task_id, workspace_id)
        ids = tuple(
            uow.session.scalars(
                select(ExecutionModel.id).where(
                    ExecutionModel.workspace_id == workspace_id
                )
            )
        )
    result = Reconciler(
        s.uow, lambda: lease.expires_at + timedelta(seconds=1)
    ).run_once(100)
    assert result.expired_leases == result.retries_queued == 0
    with s.uow() as uow:
        assert uow.executions.get(execution_id, workspace_id) == original
        assert uow.leases.get(lease.id, workspace_id) == lease
        saved_worker = uow.workers.get(worker.id, workspace_id)
        assert saved_worker.current_load == worker.current_load
        assert (
            uow.sessions.get_latest_for_execution(execution_id, workspace_id) == session
        )
        assert uow.tasks.get(task.id, workspace_id) == task
        assert set(
            uow.session.scalars(
                select(ExecutionModel.id).where(
                    ExecutionModel.workspace_id == workspace_id
                )
            )
        ) == set(ids)
    assert len(s.execution.starts) == int(state != "before_admission")
    assert not any(item[0] in {"stop", "input"} for item in s.execution.reads)


@pytest.mark.parametrize("uncertain", [False, True])
def test_bounded_scan_queries_both_command_created_originals(
    native_case, monkeypatch, uncertain
):
    s = native_case
    with s.uow() as uow:
        worker = uow.workers.get(s.worker_id, s.workspace_id)
        previous = worker.version
        worker.capacity = 2
        worker.version += 1
        uow.workers.save(worker, previous)
    s.assign("b")
    assert s.schedule("b").assigned == 1
    s.start("b")
    with s.uow() as uow:
        links = sorted(
            uow.execution_authorities.list_reconciliation_links(s.workspace_id),
            key=lambda link: link.execution_id,
        )
    original_get_run = s.execution.get_run
    queries = []

    def observe(workspace, run, *, grant_id):
        queries.append(run)
        if uncertain and run == links[0].run_id:
            raise TimeoutError("controlled first original uncertainty")
        return original_get_run(workspace, run, grant_id=grant_id)

    monkeypatch.setattr(s.execution, "get_run", observe)
    with restarted_gateway(s, monkeypatch) as runtime:
        observer = reconciler(s, runtime)
        for _ in range(3):
            assert observer.run_once(s.workspace_id, limit=1).examined == 1
    assert {link.run_id for link in links} <= set(queries)
    with s.uow() as uow:
        assert uow.execution_authorities.list_reconciliation_links(
            s.workspace_id
        ) == tuple(links)
    assert len(s.execution.starts) == 2


def test_normal_native_cursor_failure_rolls_back_entire_page(native_case, monkeypatch):
    from sidera_control_plane.persistence.execution_authority_repository import (
        ExecutionAuthorityRepository,
    )

    s = native_case
    before = runtime_facts(s)
    notices_before = notification_counts(s)
    s.execution.emit(before[0].run_id, "message")

    def fail_cursor(*args, **kwargs):
        raise RuntimeError("injected original cursor write failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            ExecutionAuthorityRepository,
            "save_original_observation",
            fail_cursor,
            raising=False,
        )
        with pytest.raises(RuntimeError, match="injected original cursor"):
            sync(s)
    assert runtime_facts(s) == before
    assert notification_counts(s) == notices_before
    sync(s)
    after = runtime_facts(s)
    notices_after = notification_counts(s)
    assert after[0].event_cursor == after[1].last_sequence == 2
    assert len(after[2]) == 2
    sync(s)
    assert runtime_facts(s) == after
    assert notification_counts(s) == notices_after
    assert len(s.execution.starts) == 1


def test_restart_repairs_genuine_old_cursor_without_inventing_fingerprints(
    database, workspace_id, tmp_path, monkeypatch
):
    s = NativeScenario(database, workspace_id, tmp_path)

    def old_wrapper(ingestor, *, expected, principal_id, events):
        with s.uow() as uow:
            authority = uow.execution_authorities.get(expected.authority_id)
            session = uow.sessions.get_latest_for_execution(
                expected.execution_id, workspace_id
            )
        return ingestor.ingest(
            workspace_id=workspace_id,
            execution_id=expected.execution_id,
            session_id=session.id,
            lease_id=authority.lease_id,
            fencing_token=authority.fencing_token,
            events=tuple(replace(event, raw_ref=None) for event in events),
        )

    s.assign()
    assert s.schedule().assigned == 1
    with monkeypatch.context() as patch:
        patch.setattr(SessionEventIngestor, "ingest_original_page", old_wrapper)
        s.start()
    before = runtime_facts(s)
    assert before[0].event_cursor == 0 and before[1].last_sequence == 1
    assert before[2][0].raw_ref is None
    notices = notification_counts(s)
    with restarted_gateway(s, monkeypatch) as runtime:
        service = reconciler(s, runtime)
        assert service.run_once(workspace_id).recovery_required == 0
        service.run_once(workspace_id)
    after = runtime_facts(s)
    assert after[0].event_cursor == 1
    assert after[1:] == before[1:]
    assert notification_counts(s) == notices
    assert len(s.execution.starts) == 1


def test_restart_consumes_multiple_pages_before_one_completion(
    native_case, monkeypatch
):
    from sidera_agents.contracts import EventPage

    s = native_case
    original = s.link()
    for _ in range(205):
        s.execution.emit(original.run_id, "message")
    s.execution.emit(original.run_id, "run.closed", state="completed")
    last = s.execution.events[original.run_id][-1]
    s.execution.events[original.run_id][-1] = last.model_copy(
        update={"data": {"status": "completed"}}
    )
    Path(s.execution.starts[0].workspace.directory, "paged-result.py").write_text(
        "RESULT = 15\n"
    )
    cursors = []

    def page(workspace, run, after_sequence, limit=100, *, grant_id):
        assert (workspace, run, grant_id) == (
            s.workspace_id,
            original.run_id,
            original.authority_id,
        )
        cursors.append(after_sequence)
        available = tuple(
            event
            for event in s.execution.events[run]
            if event.sequence > after_sequence
        )
        visible = available[:limit]
        return EventPage(
            events=visible,
            next_sequence=visible[-1].sequence if visible else after_sequence,
            has_more=len(available) > len(visible),
        )

    monkeypatch.setattr(s.execution, "list_events", page)
    with restarted_gateway(s, monkeypatch) as runtime:
        service = reconciler(s, runtime)
        for cursor in (101, 201, 207):
            result = service.run_once(s.workspace_id, limit=1)
            assert result.recovery_required == 0
            facts = runtime_facts(s)
            assert facts[0].event_cursor == facts[1].last_sequence == cursor
            with s.uow() as uow:
                assert len(uow.results.retained_code_results(s.workspace_id)) == int(
                    cursor == 207
                )
        service.run_once(s.workspace_id, limit=1)
    assert cursors[:3] == [1, 101, 201]
    assert len(s.execution.starts) == 1


@pytest.mark.parametrize(
    "changed",
    [
        "workspace",
        "run",
        "profile",
        "effective_profile",
        "runtime",
        "unsupported",
        "revoked",
        "fence",
    ],
)
def test_original_observation_negative_scope_retains_writer(
    native_case, monkeypatch, changed
):
    s = native_case
    before = runtime_facts(s)
    snapshot = s.execution.get_run(
        s.workspace_id, before[0].run_id, grant_id=before[0].authority_id
    )
    values = {
        "workspace": {"workspace_id": uuid4()},
        "run": {"run_id": uuid4()},
        "profile": {
            "profile_ref": snapshot.profile_ref.model_copy(
                update={"profile_id": uuid4()}
            )
        },
        "effective_profile": {
            "effective_config": snapshot.effective_config.model_copy(
                update={
                    "profile_ref": snapshot.profile_ref.model_copy(
                        update={"profile_id": uuid4()}
                    )
                }
            )
        },
        "runtime": {
            "effective_config": snapshot.effective_config.model_copy(
                update={
                    "runtime_ref": snapshot.effective_config.runtime_ref.model_copy(
                        update={"runtime_id": uuid4()}
                    )
                }
            )
        },
        "unsupported": {
            "state": snapshot.state.__class__("unknown"),
            "reason_code": "recovery_unsupported",
        },
    }
    if changed in values:
        monkeypatch.setattr(
            s.execution,
            "get_run",
            lambda *args, **kwargs: snapshot.model_copy(update=values[changed]),
        )
    else:
        with s.uow() as uow:
            if changed == "revoked":
                uow.execution_authorities.revoke(before[0].authority_id, s.workspace_id)
            else:
                execution = uow.executions.get(before[0].execution_id, s.workspace_id)
                previous = execution.version
                execution.last_fencing_token += 1
                execution.version += 1
                uow.executions.save(execution, previous)
        before = runtime_facts(s)
    with restarted_gateway(s, monkeypatch) as runtime:
        result = reconciler(s, runtime).run_once(s.workspace_id)
    assert result.recovery_required == 1
    assert runtime_facts(s) == before
    assert len(s.execution.starts) == 1


def test_original_page_duplicate_is_noop_and_hidden_change_conflicts(native_case):
    s = native_case
    s.execution.emit(s.link().run_id, "message")
    event = s.execution.events[s.link().run_id][-1]
    event = event.model_copy(update={"data": {"token": "first-private-value"}})
    report = original_page(s, (event,))
    assert len(report.events) == 1
    after = runtime_facts(s)
    assert original_page(s, (event,)).duplicate_sequences == (2,)
    assert runtime_facts(s) == after
    changed = event.model_copy(update={"data": {"token": "second-private-value"}})
    with pytest.raises(EventSequenceConflict):
        original_page(s, (changed,))
    assert runtime_facts(s) == after
    assert after[2][-1].payload["token"] == "[redacted]"


def test_native_gap_never_consumes_a_sequence_or_commits_partial_page(native_case):
    s = native_case
    before = runtime_facts(s)
    s.execution.emit(before[0].run_id, "message")
    first = s.execution.events[before[0].run_id][-1]
    gap = first.model_copy(update={"sequence": 4})
    with pytest.raises(EventSequenceGap):
        original_page(s, (first, gap))
    assert runtime_facts(s) == before
    original_page(s, (first,))
    link, session, events, _ = runtime_facts(s)
    assert link.event_cursor == session.last_sequence == 2
    assert all(event.event_type != "event.ingestion.error" for event in events)


def test_changed_original_link_rejects_stale_page(native_case):
    from sidera_control_plane.domain.agent_bindings import AssignmentDenied

    s = native_case
    before = runtime_facts(s)
    with pytest.raises(AssignmentDenied):
        original_page(
            s,
            s.execution.events[before[0].run_id],
            link=replace(before[0], binding_version=before[0].binding_version + 1),
        )
    assert runtime_facts(s) == before


@contextmanager
def restarted_gateway(s, monkeypatch, *, owner_check=None, renew_interval=None):
    from sidera_control_plane import gateway_worker
    from sidera_control_plane.application import (
        agent_sources,
        context_materialization,
        execution_authority,
    )
    from sidera_control_plane.config import Settings

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return s.cp.now

    monkeypatch.setattr(gateway_worker, "datetime", FrozenDateTime)
    monkeypatch.setattr(execution_authority, "datetime", FrozenDateTime)
    monkeypatch.setattr(agent_sources, "datetime", FrozenDateTime)
    monkeypatch.setattr(context_materialization, "datetime", FrozenDateTime)
    runtime = gateway_worker.build_gateway(
        Settings(
            database_url=str(s.cp.database.engine.url),
            workspace_root=s.cp.tmp_path / "native-worktrees",
            artifact_root=s.cp.tmp_path / "result-input-artifacts",
            result_repository_path=s.repository,
            result_inputs_enabled=True,
            local_actor_id=s.principal,
            assignment_lease_seconds=300,
            gateway_sync_workspace_id=s.workspace_id,
            **(
                {"assignment_lease_renew_interval_seconds": renew_interval}
                if renew_interval is not None
                else {}
            ),
        ),
        native_services=s.services,
        **({"owner_check": owner_check} if owner_check is not None else {}),
    )
    assert runtime.worker_gateway is not s.gateway
    assert runtime.workspace_provider is not s.manager
    try:
        yield runtime
    finally:
        runtime.close()


def reconciler(s, runtime):
    from sidera_control_plane.application.agent_reconciliation import AgentRunReconciler
    from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

    return AgentRunReconciler(
        lambda: SqlAlchemyUnitOfWork(
            runtime.database, before_commit=runtime.before_commit
        ),
        gateway=runtime.worker_gateway,
        services=s.services,
        clock=s.cp.clock,
        owner_check=runtime.owner_check,
    )


@pytest.mark.parametrize("normal", [True, False])
def test_detected_owner_loss_during_native_query_rolls_back_observation(
    native_case, monkeypatch, normal
):
    s = native_case
    s.execution.emit(s.link().run_id, "message")
    before, counts = runtime_facts(s), notification_counts(s)
    alive = [True]
    original = s.execution.list_events

    def lose_on_read(*args, **kwargs):
        page = original(*args, **kwargs)
        alive[0] = False
        return page

    monkeypatch.setattr(s.execution, "list_events", lose_on_read)
    with restarted_gateway(s, monkeypatch, owner_check=lambda: alive[0]) as runtime:
        if normal:
            _, session, _, authority = before
            with pytest.raises(Exception, match="owner"):
                runtime.worker_gateway.sync_session(
                    workspace_id=s.workspace_id,
                    execution_id=before[0].execution_id,
                    session_id=session.id,
                    lease_id=authority.lease_id,
                    fencing_token=authority.fencing_token,
                )
        else:
            result = reconciler(s, runtime).run_once(s.workspace_id)
            assert (
                result.recovery_required == 1
                and result.outcomes[0].code == "gateway_owner_lost"
            )
    assert runtime_facts(s) == before and notification_counts(s) == counts
    assert len(s.execution.starts) == 1


def test_original_worker_keeper_renews_during_delayed_public_read(
    native_case, monkeypatch
):
    import threading

    from sidera_control_plane.application.worker_gateway import AssignmentLeaseKeeper

    s = native_case
    before = runtime_facts(s)
    with s.uow() as uow:
        lease_before = uow.leases.get(before[3].lease_id, s.workspace_id)
    renewed = threading.Event()
    original_renew = AssignmentLeaseKeeper.renew_once

    def renew(keeper):
        result = original_renew(keeper)
        if result:
            renewed.set()
        return result

    original_read = s.execution.list_events

    def delayed_read(*args, **kwargs):
        assert renewed.wait(2), (
            "original Worker read ran without its existing lease keeper"
        )
        return original_read(*args, **kwargs)

    s.cp.now += timedelta(seconds=1)
    monkeypatch.setattr(AssignmentLeaseKeeper, "renew_once", renew)
    monkeypatch.setattr(s.execution, "list_events", delayed_read)
    with restarted_gateway(s, monkeypatch, renew_interval=0.01) as runtime:
        result = reconciler(s, runtime).run_once(s.workspace_id)
    assert result.recovery_required == 0
    with s.uow() as uow:
        lease = uow.leases.get(lease_before.id, s.workspace_id)
    assert (
        lease.version > lease_before.version
        and lease.expires_at > lease_before.expires_at
    )
    assert (lease.worker_id, lease.fencing_token, lease.execution_id) == (
        lease_before.worker_id,
        lease_before.fencing_token,
        lease_before.execution_id,
    )
    assert len(s.execution.starts) == 1
    assert not any(
        thread.name.startswith("assignment-lease-") for thread in threading.enumerate()
    )


def test_original_planner_keeper_renews_during_delayed_public_read(
    native_planning_case, uow_factory, tmp_path, monkeypatch
):
    import threading

    from sidera_control_plane.application.planning_runner import PlanningLeaseKeeper

    old, public, before = interrupted_planning(
        native_planning_case, uow_factory, tmp_path, monkeypatch, partial=True
    )
    with uow_factory() as uow:
        link = uow.planning_authorities.get_link(
            before.workspace_id, before.current_run_id
        )
        authority = uow.planning_authorities.get(link.authority_id)
        original = uow.planning_leases.get(authority.lease_id, before.workspace_id)
    renewed = threading.Event()
    original_renew = PlanningLeaseKeeper.renew_once

    def renew(keeper):
        result = original_renew(keeper)
        if result:
            renewed.set()
        return result

    original_read = public.list_events

    def delayed_read(*args, **kwargs):
        assert renewed.wait(2), (
            "original Planner read ran without its existing lease keeper"
        )
        return original_read(*args, **kwargs)

    monkeypatch.setattr(PlanningLeaseKeeper, "renew_once", renew)
    monkeypatch.setattr(public, "list_events", delayed_read)
    observer = restart_planning(
        native_planning_case, old, uow_factory, tmp_path, renew_interval=0.01
    )
    assert observer.run_once(before.workspace_id).recovery_required == 0
    with uow_factory() as uow:
        lease = uow.planning_leases.get(original.id, before.workspace_id)
        proposals = uow.planning_records.list_proposals(before.id, before.workspace_id)
    assert lease.version > original.version and len(proposals) == 1
    assert len(public.requests) == 1 and not public.stops
    assert not any(
        thread.name.startswith("planning-lease-") for thread in threading.enumerate()
    )


@pytest.mark.parametrize(
    "mismatch",
    [
        "principal",
        "workspace",
        "operation",
        "profile",
        "effective_profile",
        "runtime",
        "saved_link",
        "owner",
    ],
)
def test_expired_planner_original_pin_denials_never_apply_or_disclose_new_work(
    native_planning_case, uow_factory, tmp_path, monkeypatch, mismatch
):
    from sidera_control_plane.application.planning_reconciler import PlanningReconciler
    from sidera_control_plane.persistence.planning_authority_repository import (
        PlanningAuthorityRepository,
    )

    old, public, initial = interrupted_planning(
        native_planning_case, uow_factory, tmp_path, monkeypatch, partial=True
    )
    with uow_factory() as uow:
        original = uow.planning_authorities.get_link(
            initial.workspace_id, initial.current_run_id
        )
        authority = uow.planning_authorities.get(original.authority_id)
        lease = uow.planning_leases.get(authority.lease_id, initial.workspace_id)
    now = lease.expires_at + timedelta(seconds=1)
    assert (
        PlanningReconciler(
            uow_factory=uow_factory, owner_id="expiry", clock=lambda: now
        )
        .run_once(1)
        .blocked
        == 1
    )
    with uow_factory() as uow:
        request = uow.planning_requests.get(initial.id, initial.workspace_id)
        link = uow.planning_authorities.get_link(
            request.workspace_id, original.planning_run_id
        )
        lease = uow.planning_leases.get(authority.lease_id, request.workspace_id)
    alive, queried = [True], []
    observer = restart_planning(
        native_planning_case,
        old,
        uow_factory,
        tmp_path,
        clock=lambda: now,
        owner_check=lambda: alive[0],
    )
    if mismatch == "principal":
        observer._services = replace(observer._services, principal_id="other-principal")
    get_link = PlanningAuthorityRepository.get_link

    def changed_link(repo, *args, **kwargs):
        saved = get_link(repo, *args, **kwargs)
        if mismatch == "operation":
            operation_id = uuid4()
            return replace(
                saved,
                start_operation_id=operation_id,
                start_request=saved.start_request.model_copy(
                    update={"operation_id": operation_id}
                ),
                start_receipt=None,
            )
        if mismatch == "saved_link" and queried:
            return replace(saved, event_cursor=saved.event_cursor + 1)
        return saved

    read = public.get_run

    def snapshot(workspace, run_id, *, grant_id):
        queried.append((workspace, run_id, grant_id))
        value = read(workspace, run_id, grant_id=grant_id)
        if mismatch == "owner":
            alive[0] = False
        if mismatch == "workspace":
            return value.model_copy(update={"workspace_id": uuid4()})
        if mismatch == "profile":
            return value.model_copy(
                update={
                    "profile_ref": value.profile_ref.model_copy(
                        update={"profile_id": uuid4()}
                    )
                }
            )
        if mismatch in {"effective_profile", "runtime"}:
            config = value.effective_config
            changes = (
                {
                    "profile_ref": config.profile_ref.model_copy(
                        update={"profile_id": uuid4()}
                    )
                }
                if mismatch == "effective_profile"
                else {
                    "runtime_ref": config.runtime_ref.model_copy(
                        update={"runtime_id": uuid4()}
                    )
                }
            )
            return value.model_copy(
                update={"effective_config": config.model_copy(update=changes)}
            )
        return value

    def forbidden(*args, **kwargs):
        pytest.fail(
            "expired or mismatched original assembled a result or read event text"
        )

    with monkeypatch.context() as patch:
        patch.setattr(PlanningAuthorityRepository, "get_link", changed_link)
        patch.setattr(public, "get_run", snapshot)
        patch.setattr(public, "list_events", forbidden)
        patch.setattr(observer._planning_runner, "_assemble_input", forbidden)
        result = observer.run_once(request.workspace_id, limit=1)
    assert result.examined == result.recovery_required == 1 and result.observed == 0
    assert result.outcomes[0].code == (
        "gateway_owner_lost"
        if mismatch == "owner"
        else "permission_denied"
        if mismatch in {"principal", "operation", "saved_link"}
        else "configuration_mismatch"
    )
    assert queried == (
        []
        if mismatch in {"principal", "operation"}
        else [(request.workspace_id, link.run_id, link.authority_id)]
    )
    with uow_factory() as uow:
        assert uow.planning_requests.get(request.id, request.workspace_id) == request
        assert (
            uow.planning_authorities.get_link(
                request.workspace_id, link.planning_run_id
            )
            == link
        )
        assert uow.planning_authorities.get(authority.id) == authority
        assert uow.planning_leases.get(lease.id, request.workspace_id) == lease
        assert not uow.planning_records.list_proposals(request.id, request.workspace_id)
    assert len(public.requests) == 1 and not public.stops and not public.lookups


@pytest.mark.parametrize("bad_operation", [False, True])
def test_expired_planner_lost_ack_reads_original_without_pinning_receipt(
    native_planning_case, uow_factory, tmp_path, monkeypatch, bad_operation
):
    from sidera_control_plane.application.planning_reconciler import PlanningReconciler

    old, public, initial = interrupted_planning(
        native_planning_case,
        uow_factory,
        tmp_path,
        monkeypatch,
        partial=False,
        lost_ack=True,
    )
    with uow_factory() as uow:
        original = uow.planning_authorities.get_link(
            initial.workspace_id, initial.current_run_id
        )
        authority = uow.planning_authorities.get(original.authority_id)
        lease = uow.planning_leases.get(authority.lease_id, initial.workspace_id)
    assert original.run_id is None and original.event_cursor == 0
    now = lease.expires_at + timedelta(seconds=1)
    assert (
        PlanningReconciler(
            uow_factory=uow_factory, owner_id="expiry", clock=lambda: now
        )
        .run_once(1)
        .blocked
        == 1
    )
    with uow_factory() as uow:
        request = uow.planning_requests.get(initial.id, initial.workspace_id)
        link = uow.planning_authorities.get_link(
            request.workspace_id, original.planning_run_id
        )
        lease = uow.planning_leases.get(authority.lease_id, request.workspace_id)
    assert link.status == "unknown"
    if bad_operation:
        public.receipt = public.receipt.model_copy(update={"operation_id": uuid4()})
    queries = []
    read = public.get_run

    def snapshot(workspace, run_id, *, grant_id):
        queries.append((workspace, run_id, grant_id))
        return read(workspace, run_id, grant_id=grant_id)

    def forbidden(*args, **kwargs):
        pytest.fail("expired ACK was used to assemble a result")

    observer = restart_planning(
        native_planning_case, old, uow_factory, tmp_path, clock=lambda: now
    )
    monkeypatch.setattr(public, "get_run", snapshot)
    monkeypatch.setattr(observer._planning_runner, "_assemble_input", forbidden)
    result = observer.run_once(request.workspace_id, limit=1)
    assert result.recovery_required == 1 and result.observed == 0
    assert result.outcomes[0].code == (
        "configuration_mismatch"
        if bad_operation
        else "native_planning_recovery_required"
    )
    assert public.lookups == [
        (request.workspace_id, link.start_operation_id, link.authority_id)
    ]
    assert queries == (
        []
        if bad_operation
        else [(request.workspace_id, public.run_id, link.authority_id)]
    )
    with uow_factory() as uow:
        assert uow.planning_requests.get(request.id, request.workspace_id) == request
        assert (
            uow.planning_authorities.get_link(
                request.workspace_id, link.planning_run_id
            )
            == link
        )
        assert uow.planning_leases.get(lease.id, request.workspace_id) == lease
        assert not uow.planning_records.list_proposals(request.id, request.workspace_id)
    assert len(public.requests) == 1 and not public.stops


def test_original_planner_detected_owner_loss_before_output_commit(
    native_planning_case, uow_factory, tmp_path, monkeypatch
):
    from sidera_control_plane.application.agent_worker_adapter import (
        require_gateway_owner,
    )
    from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

    old, public, before = interrupted_planning(
        native_planning_case, uow_factory, tmp_path, monkeypatch, partial=True
    )
    with uow_factory() as uow:
        original = uow.planning_authorities.get_link(
            before.workspace_id, before.current_run_id
        )
    alive = [True]
    check = lambda: alive[0]
    guarded = lambda: SqlAlchemyUnitOfWork(
        old._uow_factory()._database, before_commit=lambda: require_gateway_owner(check)
    )
    original_read = public.list_events

    def lose_after_output(*args, **kwargs):
        page = original_read(*args, **kwargs)
        alive[0] = False
        return page

    observer = restart_planning(
        native_planning_case, old, guarded, tmp_path, owner_check=check
    )
    monkeypatch.setattr(public, "list_events", lose_after_output)
    result = observer.run_once(before.workspace_id)
    assert result.outcomes[0].code == "gateway_owner_lost"
    with uow_factory() as uow:
        assert uow.planning_requests.get(before.id, before.workspace_id) == before
        assert (
            uow.planning_authorities.get_link(
                before.workspace_id, before.current_run_id
            )
            == original
        )
        assert not uow.planning_records.list_proposals(before.id, before.workspace_id)
    assert len(public.requests) == 1 and not public.stops


def test_detected_owner_loss_before_native_start_has_zero_public_starts(
    database, workspace_id, tmp_path, monkeypatch
):
    from sidera_control_plane.application.agent_execution import (
        TaskExecutionInputBuilder,
    )
    from sidera_control_plane.application.agent_worker_adapter import AgentWorkerAdapter

    s = NativeScenario(database, workspace_id, tmp_path)
    s.assign()
    assert s.schedule().assigned == 1
    alive, builds, starting = [True], [], [False]
    check = lambda: alive[0]
    original_start = AgentWorkerAdapter.start_session

    def start(adapter, *args, **kwargs):
        assert adapter._owner_check is check
        starting[0] = True
        return original_start(adapter, *args, **kwargs)

    original = TaskExecutionInputBuilder.build

    def lose_after_final_build(*args, **kwargs):
        request = original(*args, **kwargs)
        if starting[0]:
            builds.append(request.operation_id)
            if len(builds) == 2:
                alive[0] = False
        return request

    with restarted_gateway(s, monkeypatch, owner_check=check) as runtime:
        monkeypatch.setattr(TaskExecutionInputBuilder, "build", lose_after_final_build)
        monkeypatch.setattr(AgentWorkerAdapter, "start_session", start)
        s.gateway = runtime.worker_gateway
        with pytest.raises(Exception) as denied:
            s.start()
        assert denied.value.code == "gateway_owner_lost"
    link = s.link()
    assert len(builds) == 2 and not s.execution.starts
    assert link.status == "starting" and link.run_id is None and link.event_cursor == 0
    with s.uow() as uow:
        assert (
            uow.sessions.get_latest_for_execution(link.execution_id, workspace_id)
            is None
        )


@pytest.mark.parametrize("action", ["input", "stop"])
def test_detected_owner_loss_immediately_before_native_mutation(
    database, workspace_id, tmp_path, monkeypatch, action
):
    from sidera_control_plane import (
        gateway_worker,
        handoff_worker,
        input_dispatcher_worker,
    )
    from sidera_control_plane.application.agent_execution import (
        TaskExecutionInputBuilder,
    )
    from sidera_control_plane.application.execution_authority import (
        ExecutionAuthorityReader,
    )
    from sidera_control_plane.config import Settings
    from tests.integration.test_profile_intervention import (
        InterventionScenario,
        PublicStopControl,
        native_confirm_command,
        native_prepare_command,
    )

    intervention = InterventionScenario(database, workspace_id, tmp_path, monkeypatch)
    s = intervention.native
    alive = [True]
    try:
        with restarted_gateway(s, monkeypatch, owner_check=lambda: alive[0]) as runtime:
            if action == "input":
                accepted = intervention.commands.handle(intervention.command())
                original = TaskExecutionInputBuilder.build_input

                def lose_after_build(*args, **kwargs):
                    result = original(*args, **kwargs)
                    alive[0] = False
                    return result

                monkeypatch.setattr(
                    TaskExecutionInputBuilder, "build_input", lose_after_build
                )
                monkeypatch.setattr(
                    input_dispatcher_worker, "datetime", gateway_worker.datetime
                )
                dispatcher = input_dispatcher_worker.build_input_dispatcher(
                    Settings(), gateway_runtime=runtime
                )
                with pytest.raises(Exception) as denied:
                    dispatcher.dispatcher.run_once(1)
                assert denied.value.code == "gateway_owner_lost"
                assert not intervention.public.sent
                assert (
                    intervention.input(accepted.aggregate_id).status.value
                    == "dispatching"
                )
            else:
                stops = PublicStopControl(intervention)
                prepared = intervention.commands.handle(
                    native_prepare_command(intervention, "human")
                )
                with s.uow() as uow:
                    proposal = uow.handoff_proposals.get(
                        prepared.aggregate_id, workspace_id
                    )
                intervention.commands.handle(
                    native_confirm_command(intervention, proposal)
                )
                monkeypatch.setattr(handoff_worker, "datetime", gateway_worker.datetime)
                worker = handoff_worker.build_handoff_worker(
                    Settings(local_actor_id=s.principal),
                    gateway_runtime=runtime,
                    native_services=s.services,
                )
                assert worker.coordinator.run_once(1).progressed == 1
                original = ExecutionAuthorityReader.require

                def lose_after_permission(reader, grant_id, operation, target):
                    original(reader, grant_id, operation, target)
                    if operation == "run.stop":
                        alive[0] = False

                monkeypatch.setattr(
                    ExecutionAuthorityReader, "require", lose_after_permission
                )
                before = runtime_facts(s)
                result = worker.coordinator.run_once(1)
                assert not alive[0] and result.progressed == result.converged == 0
                assert not stops.sent
                assert runtime_facts(s) == before
            with s.uow() as uow:
                assert (
                    uow.sessions.get_any(
                        intervention.session.id, workspace_id
                    ).status.value
                    == "active"
                )
                assert not uow.results.retained_code_results(workspace_id)
        assert len(s.execution.starts) == 1
    finally:
        intervention.public.complete_original_turn()


def test_actual_gateway_owner_background_tick_observes_without_browser(
    native_case, monkeypatch
):
    from sidera_control_plane import (
        gateway_owner,
        gateway_worker,
        handoff_worker,
        input_dispatcher_worker,
    )
    from sidera_control_plane.application.agent_reconciliation import AgentRunReconciler
    from sidera_control_plane.config import Settings

    s = native_case
    s.execution.emit(s.link().run_id, "message")

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return s.cp.now

    for module in (gateway_worker, input_dispatcher_worker, handoff_worker):
        monkeypatch.setattr(module, "datetime", FrozenDateTime)
    calls = []
    original = AgentRunReconciler.run_once

    def observe(service, workspace, limit):
        calls.append((service, workspace, limit))
        return original(service, workspace, limit)

    monkeypatch.setattr(AgentRunReconciler, "run_once", observe)
    settings = Settings(
        database_url=str(s.cp.database.engine.url),
        workspace_root=s.cp.tmp_path / "native-worktrees",
        artifact_root=s.cp.tmp_path / "result-input-artifacts",
        result_repository_path=s.repository,
        result_inputs_enabled=True,
        local_actor_id=s.principal,
        assignment_lease_seconds=300,
        gateway_sync_workspace_id=s.workspace_id,
        gateway_batch_size=1,
    )
    owner = gateway_owner.build_gateway_owner(settings, native_services=s.services)
    try:
        assert owner.owner_lock.check()
        assert owner.gateway_runtime.owner_check == owner.owner_lock.check
        gateway_worker._run_owner_once(owner, settings)
        assert calls == [(owner.gateway_runtime.native_reconciler, s.workspace_id, 1)]
        facts = runtime_facts(s)
        assert facts[0].event_cursor == facts[1].last_sequence == 2
        assert len(facts[2]) == 2 and len(s.execution.starts) == 1
    finally:
        owner.close()
    assert not owner.owner_lock.check()


def test_actual_planning_runtime_recovers_its_own_original_on_background_tick(
    native_planning_case, database, uow_factory, tmp_path, monkeypatch
):
    from sidera_control_plane import planning_worker
    from sidera_control_plane.adapters.planning_workspace import (
        PrivatePlanningWorkspaceProvider,
    )
    from sidera_control_plane.application.agent_worker_adapter import (
        NativeWorkerServices,
    )
    from sidera_control_plane.application.execution_authority import (
        NativeAuthorityReader,
        PlanningAuthorityReader,
    )
    from sidera_control_plane.application.planning_runner import PlanningRunner
    from sidera_control_plane.config import Settings
    from sidera_control_plane.context_package_worker import ContextPackageWorker
    from tests.integration.test_agent_planning import (
        PublicPlanningExecution,
        native_proposal_body,
    )
    from tests.integration.test_context_package_worker import EmptyCatalog

    _, identity, scope, catalog, runtimes, commands, envelope = native_planning_case
    receipt = commands.handle(envelope)
    assert (
        ContextPackageWorker(uow_factory=uow_factory, catalog=EmptyCatalog())
        .run_once()
        .ready
        == 1
    )
    reader = PlanningAuthorityReader(
        uow_factory, principal=lambda: identity.principal_id, authorizer=scope
    )
    public = PublicPlanningExecution(
        runtimes.runtime.ref, native_proposal_body(envelope)
    )
    services = NativeWorkerServices(
        catalog,
        runtimes,
        public,
        NativeAuthorityReader(uow_factory, planner=reader, worker=None),
        identity.principal_id,
        identity.grant_id,
    )
    settings = Settings(
        database_url=str(database.engine.url),
        local_actor_id=identity.principal_id,
        planning_temp_root=tmp_path / "planning",
        gateway_sync_workspace_id=envelope.workspace_id,
    )

    def build():
        return planning_worker.build_planning_worker(
            settings,
            native_services=services,
            planning_workspace_provider=PrivatePlanningWorkspaceProvider(
                tmp_path / "scratch"
            ),
            planning_authorizer=scope,
            owner_check=lambda: True,
        )

    initial = build()

    def crash(*args):
        raise SimulatedProcessExit()

    try:
        with monkeypatch.context() as patch:
            patch.setattr(PlanningRunner, "_persist_result", crash)
            with pytest.raises(SimulatedProcessExit):
                initial.worker.run_once(1)
    finally:
        initial.catalog.close()
        initial.database.dispose()
    with uow_factory() as uow:
        before = uow.planning_requests.get(
            UUID(receipt.data["planning_request_id"]), envelope.workspace_id
        )
        link = uow.planning_authorities.get_link(
            envelope.workspace_id, before.current_run_id
        )
        assert (
            link.closed and before.run(link.planning_run_id).status.value == "running"
        )
    restarted = build()
    try:
        assert restarted.runner is not initial.runner
        assert restarted.native_reconciler._planning_runner is restarted.runner
        restarted.worker.run_once(1)
        with uow_factory() as uow:
            saved = uow.planning_requests.get(before.id, before.workspace_id)
            proposals = uow.planning_records.list_proposals(
                before.id, before.workspace_id
            )
        assert saved.current_run_id == before.current_run_id and len(saved.runs) == 1
        assert (
            saved.run(before.current_run_id).status.value == "succeeded"
            and len(proposals) == 1
        )
        assert len(public.requests) == 1 and not public.stops
    finally:
        restarted.catalog.close()
        restarted.database.dispose()


def test_restarted_input_consumer_queries_original_without_resend(
    database, workspace_id, tmp_path, monkeypatch
):
    from sidera_control_plane import gateway_worker, input_dispatcher_worker
    from sidera_control_plane.config import Settings
    from tests.integration.test_profile_intervention import InterventionScenario

    intervention = InterventionScenario(database, workspace_id, tmp_path, monkeypatch)
    s = intervention.native
    try:
        receipt = intervention.commands.handle(intervention.command())
        intervention.dispatcher.run_once(1)
        pending = intervention.input(receipt.aggregate_id)
        assert (
            pending.status.value == "dispatching" and len(intervention.public.sent) == 1
        )
        intervention.public.delivered(pending.operation_id)
        with restarted_gateway(s, monkeypatch, owner_check=lambda: True) as runtime:
            monkeypatch.setattr(
                input_dispatcher_worker, "datetime", gateway_worker.datetime
            )
            consumer = input_dispatcher_worker.build_input_dispatcher(
                Settings(), gateway_runtime=runtime
            )
            assert consumer.dispatcher.run_once(1).delivered == 1
            assert consumer.dispatcher.run_once(1).delivered == 0
        item, operation, events, _ = intervention.facts(pending.id)
        assert (
            item.status.value == "delivered"
            and item.operation_id == pending.operation_id
        )
        assert operation.status.value == "succeeded" and len(events) == 1
        assert len(intervention.public.sent) == len(s.execution.starts) == 1
        assert all(
            query[1:3] == (intervention.link.run_id, pending.operation_id)
            for query in intervention.public.queried
        )
    finally:
        intervention.public.complete_original_turn()


def test_restarted_stop_consumer_queries_original_and_keeps_consumed_cursor(
    database, workspace_id, tmp_path, monkeypatch
):
    from sidera_control_plane import gateway_worker, handoff_worker
    from sidera_control_plane.config import Settings
    from tests.integration.test_profile_intervention import (
        InterventionScenario,
        pending_native_stop,
    )

    intervention = InterventionScenario(database, workspace_id, tmp_path, monkeypatch)
    s = intervention.native
    try:
        stops, _, transition_id, operation_id = pending_native_stop(
            intervention, monkeypatch, target="human"
        )
        before = runtime_facts(s)
        stops.close_original()
        with restarted_gateway(s, monkeypatch, owner_check=lambda: True) as runtime:
            monkeypatch.setattr(handoff_worker, "datetime", gateway_worker.datetime)
            consumer = handoff_worker.build_handoff_worker(
                Settings(local_actor_id=s.principal),
                gateway_runtime=runtime,
                native_services=s.services,
            )
            assert consumer.coordinator.run_once(1).progressed == 1
        after = runtime_facts(s)
        assert (
            after[1].status.value == "closed"
            and after[1].last_sequence == before[1].last_sequence
        )
        assert after[0] == before[0] and after[3] == before[3]
        assert stops.queried[-1] == (
            workspace_id,
            intervention.link.run_id,
            operation_id,
            intervention.link.authority_id,
        )
        assert (
            len(stops.sent) == len(s.execution.starts) == 1
            and not intervention.public.sent
        )
        with s.uow() as uow:
            transition = uow.handoff_transitions.get(transition_id, workspace_id)
            assert transition.source_stop_facts["operation_id"] == str(operation_id)
    finally:
        intervention.public.complete_original_turn()


def test_restart_retries_pending_physical_capture_after_facts_commit(
    native_case, monkeypatch
):
    from sidera_control_plane.adapters.git_results import GitResultStore
    from sidera_control_plane.application.code_result_capture import (
        capture_operation_for_execution,
    )

    s = native_case
    s.execution.emit(s.link().run_id, "run.closed", state="completed")
    values = s.execution.events[s.link().run_id]
    values[-1] = values[-1].model_copy(update={"data": {"status": "completed"}})
    with restarted_gateway(s, monkeypatch) as runtime:
        service = reconciler(s, runtime)
        with monkeypatch.context() as patch:

            def fail(*args, **kwargs):
                raise OSError("controlled physical capture unavailable")

            patch.setattr(GitResultStore, "capture", fail)
            assert service.run_once(s.workspace_id).recovery_required == 1
        committed = runtime_facts(s)
        assert committed[0].event_cursor == committed[1].last_sequence == 2
        with s.uow() as uow:
            operation_id = capture_operation_for_execution(
                uow, execution_id=committed[0].execution_id, workspace_id=s.workspace_id
            )
            pending = uow.results.get_operation(operation_id, s.workspace_id)
            assert pending is not None and not uow.results.retained_code_results(
                s.workspace_id
            )
    with restarted_gateway(s, monkeypatch) as runtime:
        service = reconciler(s, runtime)
        assert service.run_once(s.workspace_id).recovery_required == 0
        service.run_once(s.workspace_id)
    with s.uow() as uow:
        results = uow.results.retained_code_results(s.workspace_id)
        assert len(results) == 1 and results[0].capture_operation_id == operation_id
    assert len(s.execution.starts) == 1


def test_detected_owner_loss_after_staged_native_facts_rolls_back(
    native_case, monkeypatch
):
    from sidera_control_plane.persistence.execution_authority_repository import (
        ExecutionAuthorityRepository,
    )

    s = native_case
    s.execution.emit(s.link().run_id, "message")
    before, counts = runtime_facts(s), notification_counts(s)
    alive = [True]
    original = ExecutionAuthorityRepository.save_original_observation

    def lose_after_stage(*args, **kwargs):
        link = original(*args, **kwargs)
        alive[0] = False
        return link

    with restarted_gateway(s, monkeypatch, owner_check=lambda: alive[0]) as runtime:
        monkeypatch.setattr(
            ExecutionAuthorityRepository, "save_original_observation", lose_after_stage
        )
        result = reconciler(s, runtime).run_once(s.workspace_id)
    assert result.outcomes[0].code == "gateway_owner_lost"
    assert runtime_facts(s) == before and notification_counts(s) == counts


def change_parent_default(s):
    replacement = s.profile.model_copy(
        update={"ref": s.profile.ref.model_copy(update={"profile_id": uuid4()})}
    )
    s.catalog.profiles[replacement.ref] = replacement
    with s.uow() as uow:
        parent = uow.tasks.get(s.cp.ids["root_task"], s.workspace_id)
    s.commands.handle(
        s.command(
            "SetSubtaskAssignee",
            "task",
            parent.id,
            parent.version,
            {
                "assignee_kind": "profile",
                "assignee_id": str(replacement.ref.profile_id),
                "profile_revision": 1,
            },
        )
    )
    return replacement


def lose_start_ack(s, monkeypatch):
    from sidera_control_plane.persistence.execution_authority_repository import (
        ExecutionAuthorityRepository,
    )

    s.assign()
    assert s.schedule().assigned == 1
    with monkeypatch.context() as patch:

        def lose(*args, **kwargs):
            raise RuntimeError("lost original CP admission ACK")

        patch.setattr(ExecutionAuthorityRepository, "record_start", lose)
        assert s.start().status == "starting"
    assert s.link().run_id is None
    assert len(s.execution.starts) == 1


@pytest.mark.parametrize("loss", ["expired", "revoked"])
def test_expired_original_worker_ack_is_read_only_recovery_required(
    database, workspace_id, tmp_path, monkeypatch, loss
):
    s = NativeScenario(database, workspace_id, tmp_path)
    lose_start_ack(s, monkeypatch)
    original = s.link()
    with s.uow() as uow:
        authority = uow.execution_authorities.get(original.authority_id)
        lease = uow.leases.get(authority.lease_id, workspace_id)
    if loss == "expired":
        s.cp.now = lease.expires_at + timedelta(seconds=1)
    else:
        with s.uow() as uow:
            uow.execution_authorities.revoke(authority.id, workspace_id)
    with s.uow() as uow:
        authority = uow.execution_authorities.get(original.authority_id)
        execution = uow.executions.get(original.execution_id, workspace_id)
        lease = uow.leases.get(authority.lease_id, workspace_id)
    counts = notification_counts(s)
    with restarted_gateway(s, monkeypatch) as runtime:
        result = reconciler(s, runtime).run_once(workspace_id)
    assert (
        result.outcomes[0].code == "native_recovery_required"
        and result.recovery_required == 1
    )
    with s.uow() as uow:
        assert (
            uow.execution_authorities.get_link(workspace_id, original.execution_id)
            == original
        )
        assert uow.execution_authorities.get(original.authority_id) == authority
        assert uow.executions.get(original.execution_id, workspace_id) == execution
        assert uow.leases.get(authority.lease_id, workspace_id) == lease
        assert (
            uow.sessions.get_latest_for_execution(original.execution_id, workspace_id)
            is None
        )
        assert not uow.results.retained_code_results(workspace_id)
    assert notification_counts(s) == counts and len(s.execution.starts) == 1
    assert s.execution.reads[-1] == (
        "start",
        workspace_id,
        original.start_operation_id,
        original.authority_id,
    )


@pytest.mark.parametrize("state", ["completed", "unknown"])
def test_actual_expired_planner_original_is_selected_read_only(
    native_planning_case, uow_factory, tmp_path, monkeypatch, state
):
    from sidera_agents.contracts import RunState

    from sidera_control_plane.application.planning_reconciler import PlanningReconciler

    old, public, original_request = interrupted_planning(
        native_planning_case, uow_factory, tmp_path, monkeypatch, partial=True
    )
    with uow_factory() as uow:
        original = uow.planning_authorities.get_link(
            original_request.workspace_id, original_request.current_run_id
        )
        authority = uow.planning_authorities.get(original.authority_id)
        lease = uow.planning_leases.get(
            authority.lease_id, original_request.workspace_id
        )
    now = lease.expires_at + timedelta(seconds=1)
    recovery = PlanningReconciler(
        uow_factory=uow_factory, owner_id="actual-expiry-reconciler", clock=lambda: now
    ).run_once(1)
    assert recovery.expired == recovery.blocked == 1 and recovery.retried == 0
    with uow_factory() as uow:
        request = uow.planning_requests.get(
            original_request.id, original_request.workspace_id
        )
        link = uow.planning_authorities.get_link(
            request.workspace_id, original.planning_run_id
        )
        lease = uow.planning_leases.get(authority.lease_id, request.workspace_id)
    assert (
        link.status == "cleanup_pending"
        and request.run(link.planning_run_id).error_code == "native_lease_expired"
    )
    assert lease.status.value == "expired" and len(request.runs) == 1
    queries = []
    read = public.get_run

    def snapshot(workspace, run_id, *, grant_id):
        queries.append((workspace, run_id, grant_id))
        return read(workspace, run_id, grant_id=grant_id).model_copy(
            update={"state": RunState(state)}
        )

    monkeypatch.setattr(public, "get_run", snapshot)
    observer = restart_planning(
        native_planning_case, old, uow_factory, tmp_path, clock=lambda: now
    )

    def forbidden(*args, **kwargs):
        pytest.fail("expired original assembled or applied a new Planner result")

    monkeypatch.setattr(observer._planning_runner, "_assemble_input", forbidden)
    monkeypatch.setattr(observer._planning_runner._adapter_factory, "create", forbidden)
    result = observer.run_once(request.workspace_id, limit=1)
    assert result.examined == result.recovery_required == 1
    assert result.outcomes[0].code == "native_planning_recovery_required"
    assert queries == [(request.workspace_id, link.run_id, link.authority_id)]
    with uow_factory() as uow:
        assert uow.planning_requests.get(request.id, request.workspace_id) == request
        assert (
            uow.planning_authorities.get_link(
                request.workspace_id, link.planning_run_id
            )
            == link
        )
        assert uow.planning_leases.get(lease.id, request.workspace_id) == lease
        assert not uow.planning_records.list_proposals(request.id, request.workspace_id)
    assert len(public.requests) == 1 and not public.stops


def test_restart_lost_start_ack_binds_original_despite_changed_default(
    database, workspace_id, tmp_path, monkeypatch
):
    s = NativeScenario(database, workspace_id, tmp_path)
    lose_start_ack(s, monkeypatch)
    original = s.link()
    replacement = change_parent_default(s)
    with restarted_gateway(s, monkeypatch) as runtime:
        result = reconciler(s, runtime).run_once(s.workspace_id, limit=10)
    link, session, events, _ = runtime_facts(s)
    assert result.recovery_required == 0
    assert link.run_id == s.execution.receipts[original.start_operation_id].run_id
    assert (
        link.start_operation_id,
        link.authority_id,
        link.profile_ref,
        link.execution_id,
    ) == (
        original.start_operation_id,
        original.authority_id,
        original.profile_ref,
        original.execution_id,
    )
    assert link.profile_ref != replacement.ref
    assert session.adapter_session_id == f"agent-run:{link.run_id}"
    assert link.event_cursor == session.last_sequence == 1
    assert len(events) == len(s.execution.starts) == 1
    assert (
        "start",
        s.workspace_id,
        original.start_operation_id,
        original.authority_id,
    ) in s.execution.reads


@pytest.mark.parametrize("query", ("timeout", "unknown", "not_found"))
def test_restart_unconfirmed_original_start_never_reissues(
    database, workspace_id, tmp_path, monkeypatch, query
):
    from sidera_agents.contracts import StartLookup, StartReceipt

    s = NativeScenario(database, workspace_id, tmp_path)
    lose_start_ack(s, monkeypatch)
    original = s.link()

    def get_start(workspace, operation, *, grant_id):
        assert (workspace, operation, grant_id) == (
            s.workspace_id,
            original.start_operation_id,
            original.authority_id,
        )
        if query == "timeout":
            raise TimeoutError("controlled query timeout")
        return StartLookup(
            found=query != "not_found",
            receipt=None
            if query == "not_found"
            else StartReceipt(
                operation_id=operation,
                status="unknown",
                run_id=None,
                reason_code="original_unknown",
            ),
        )

    monkeypatch.setattr(s.execution, "get_start", get_start)
    with restarted_gateway(s, monkeypatch) as runtime:
        result = reconciler(s, runtime).run_once(s.workspace_id, limit=10)
    assert result.recovery_required == 1
    assert (
        result.outcomes[0].code
        == {
            "timeout": "native_query_unconfirmed",
            "unknown": "native_admission_unknown",
            "not_found": "native_admission_not_found",
        }[query]
    )
    assert s.link().run_id is None
    assert s.link().start_operation_id == original.start_operation_id
    assert len(s.execution.starts) == 1
    with s.uow() as uow:
        assert not uow.execution_authorities.has_session(
            s.workspace_id, original.execution_id
        )


@pytest.mark.parametrize(
    ("kind", "status", "expected_type", "expected_execution"),
    (
        ("run.closed", "completed", "session.completed", "succeeded"),
        ("run.closed", "stopped", "session.closed", "running"),
        ("run.failed", "failed", "session.failed", "failed"),
    ),
)
def test_restart_terminal_event_race_retains_immutable_completion(
    native_case, monkeypatch, kind, status, expected_type, expected_execution
):
    s = native_case
    original = s.link()
    old_snapshot = s.execution.get_run(
        s.workspace_id, original.run_id, grant_id=original.authority_id
    )
    Path(s.execution.starts[0].workspace.directory, "native-result.py").write_text(
        "RESULT = 15\n"
    )
    s.execution.emit(original.run_id, kind, state=status)
    event = s.execution.events[original.run_id][-1].model_copy(
        update={"data": {"status": status}}
    )
    s.execution.events[original.run_id][-1] = event
    actual_get_run = s.execution.get_run
    snapshots = iter((old_snapshot,))

    def earlier_snapshot(*args, **kwargs):
        return next(snapshots, None) or actual_get_run(*args, **kwargs)

    monkeypatch.setattr(s.execution, "get_run", earlier_snapshot)
    with restarted_gateway(s, monkeypatch) as runtime:
        service = reconciler(s, runtime)
        service.run_once(s.workspace_id, limit=10)
        after = runtime_facts(s)
        assert after[0].event_cursor == after[1].last_sequence == 2
        assert after[2][-1].event_type == expected_type
        with s.uow() as uow:
            execution = uow.executions.get(original.execution_id, s.workspace_id)
            results = uow.results.retained_code_results(s.workspace_id)
            assert execution.status.value == expected_execution
            assert len(results) == int(status == "completed")
        service.run_once(s.workspace_id, limit=10)
        assert runtime_facts(s) == after
    assert len(s.execution.starts) == 1


def test_replacement_keeps_current_progress_after_late_original_history(
    database, workspace_id, tmp_path, monkeypatch
):
    from sidera_control_plane.application.worker_workspace_queries import (
        WorkerWorkspaceQueryService,
    )
    from sidera_control_plane.persistence.worker_workspace_query_store import (
        SqlAlchemyWorkerWorkspaceQueryStore,
    )
    from tests.integration.test_profile_intervention import (
        InterventionScenario,
        native_profile_at_ack,
    )

    intervention = InterventionScenario(database, workspace_id, tmp_path, monkeypatch)
    s = intervention.native
    stops, worker, transition_id = native_profile_at_ack(intervention, monkeypatch)
    for _ in range(4):
        worker.coordinator.run_once(1)
    with s.uow() as uow:
        transition = uow.handoff_transitions.get(transition_id, workspace_id)
        assert transition.status.value == "converged"
        target_link = uow.execution_authorities.get_link(
            workspace_id, transition.target_execution_id
        )
        original_link = uow.execution_authorities.get_link(
            workspace_id, intervention.execution.id
        )
    s.execution.emit(target_link.run_id, "progress.updated")
    target_event = s.execution.events[target_link.run_id][-1]
    s.execution.events[target_link.run_id][-1] = target_event.model_copy(
        update={"data": {"summary": "replacement work"}}
    )
    with s.uow() as uow:
        authority = uow.execution_authorities.get(target_link.authority_id)
    s.gateway.sync_session(
        workspace_id=workspace_id,
        execution_id=target_link.execution_id,
        session_id=transition.target_session_id,
        lease_id=authority.lease_id,
        fencing_token=authority.fencing_token,
    )
    query = WorkerWorkspaceQueryService(SqlAlchemyWorkerWorkspaceQueryStore(database))
    before = query.get_workspace(workspace_id, intervention.item.id)
    assert before.current_runtime.execution_id == target_link.execution_id
    assert before.current_step == "replacement work"
    s.execution.emit(original_link.run_id, "progress.updated")
    late = s.execution.events[original_link.run_id][-1]
    s.execution.events[original_link.run_id][-1] = late.model_copy(
        update={"data": {"summary": "late original history"}}
    )
    with restarted_gateway(s, monkeypatch) as runtime:
        observer = reconciler(s, runtime)
        observer.run_once(workspace_id, limit=10)
        observer.run_once(workspace_id, limit=10)
    after = query.get_workspace(workspace_id, intervention.item.id)
    assert after.current_runtime.execution_id == before.current_runtime.execution_id
    assert after.current_runtime.session_id == before.current_runtime.session_id
    assert after.current_step == before.current_step
    assert after.latest_checkpoint == before.latest_checkpoint
    loaded = query._store.load(workspace_id, intervention.item.id)
    assert any(
        event.execution_id == original_link.execution_id
        and event.payload.get("summary") == "late original history"
        for event in loaded.events
    )
    with s.uow() as uow:
        assert (
            uow.sessions.get_any(intervention.session.id, workspace_id).status.value
            == "closed"
        )
    assert len(s.execution.starts) == 2 and len(stops.sent) == 1
    assert not intervention.public.sent


def test_closed_original_replay_after_proved_stop_is_atomic_idempotent(
    database, workspace_id, tmp_path, monkeypatch
):
    from sidera_control_plane.persistence.execution_authority_repository import (
        ExecutionAuthorityRepository,
    )
    from tests.integration.test_profile_intervention import (
        InterventionScenario,
        pending_native_stop,
    )

    intervention = InterventionScenario(database, workspace_id, tmp_path, monkeypatch)
    s = intervention.native
    before_stop = runtime_facts(s)
    stops, worker, transition_id, _ = pending_native_stop(
        intervention, monkeypatch, target="human"
    )
    stops.close_original()
    assert worker.coordinator.run_once(1).progressed == 1
    before = runtime_facts(s)
    assert before[1].status.value == "closed"
    assert before[1].last_sequence == before_stop[1].last_sequence
    assert len(s.execution.events[before[0].run_id]) > before[1].last_sequence
    # Finish the already-confirmed human handoff through the actual Coordinator.
    for _ in range(8):
        worker.coordinator.run_once(1)
        with s.uow() as uow:
            transition = uow.handoff_transitions.get(transition_id, s.workspace_id)
        if transition.status.value == "converged":
            break
    assert transition.status.value == "converged"
    with s.uow() as uow:
        appointment = uow.agent_bindings.get(
            s.workspace_id, "worker_item", intervention.item.id
        )
        item_before = uow.worker_items.get(intervention.item.id, s.workspace_id)
        assert appointment.assignee.kind == "human"
    before = runtime_facts(s)
    with restarted_gateway(s, monkeypatch) as runtime:
        service = reconciler(s, runtime)
        with monkeypatch.context() as patch:

            def fail_cursor(*args, **kwargs):
                raise RuntimeError("closed-original cursor write failure")

            patch.setattr(
                ExecutionAuthorityRepository, "save_original_observation", fail_cursor
            )
            assert service.run_once(s.workspace_id, limit=10).recovery_required == 1
        assert runtime_facts(s) == before
        assert service.run_once(s.workspace_id, limit=10).recovery_required == 0
        after = runtime_facts(s)
        assert after[1].status.value == "closed"
        assert (
            after[0].event_cursor
            == after[1].last_sequence
            == len(s.execution.events[after[0].run_id])
        )
        assert [event.sequence for event in after[2]] == list(
            range(1, after[0].event_cursor + 1)
        )
        service.run_once(s.workspace_id, limit=10)
        assert runtime_facts(s) == after
    with s.uow() as uow:
        assert (
            uow.agent_bindings.get(s.workspace_id, "worker_item", intervention.item.id)
            == appointment
        )
        assert uow.worker_items.get(intervention.item.id, s.workspace_id) == item_before
        assert uow.results.retained_code_results(s.workspace_id) == ()
    assert len(stops.sent) == len(s.execution.starts) == 1
    assert intervention.public.sent == []
