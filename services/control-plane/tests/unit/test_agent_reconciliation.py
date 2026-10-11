import importlib
from contextlib import nullcontext
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sidera_agents.contracts import ProfileRef, StartLookup, StartReceipt

from sidera_control_plane.domain.agent_bindings import AgentExecutionLink
from sidera_control_plane.domain.planning import (
    PlanningRunLease,
    PlanningRunLeaseStatus,
)


def new_reconciler(uow_factory):
    module = importlib.import_module(
        "sidera_control_plane.application.agent_reconciliation"
    )
    return module.AgentRunReconciler(
        uow_factory,
        gateway=SimpleNamespace(),
        services=SimpleNamespace(
            principal_id="operator", authorizer=None, execution=None
        ),
    )


def unadmitted_link(status):
    workspace_id, operation_id = uuid4(), uuid4()
    receipt_status = {"waiting_capacity": "busy", "failed": "rejected"}.get(status)
    return AgentExecutionLink(
        workspace_id=workspace_id,
        execution_id=uuid4(),
        start_operation_id=operation_id,
        profile_ref=ProfileRef(
            workspace_id=workspace_id, profile_id=uuid4(), revision=1
        ),
        run_id=None,
        event_cursor=0,
        status=status,
        authority_id=uuid4(),
        binding_version=1,
        request_sha256=None if status == "pending" else "a" * 64,
        start_receipt=None
        if receipt_status is None
        else StartReceipt(
            operation_id=operation_id,
            status=receipt_status,
            run_id=None,
            reason_code=None,
        ),
    )


@pytest.mark.parametrize(
    "status, code, recovery",
    (
        ("pending", "native_admission_pending", False),
        ("waiting_capacity", "native_capacity_wait", False),
        ("failed", "native_admission_rejected", True),
    ),
)
def test_known_unadmitted_observer_checks_original_without_native_lookup(
    monkeypatch,
    status,
    code,
    recovery,
):
    link = unadmitted_link(status)
    calls = []

    def no_transaction():
        pytest.fail("Known unadmitted observation opened a write transaction")

    service = new_reconciler(no_transaction)

    def require_original(expected):
        assert expected is link
        calls.append(("require", expected))
        return SimpleNamespace()

    def get_start(workspace_id, operation_id, *, grant_id):
        calls.append(("lookup", workspace_id, operation_id, grant_id))
        return StartLookup(found=False, receipt=None)

    monkeypatch.setattr(service, "_require_original", require_original)
    service._services.execution = SimpleNamespace(get_start=get_start)
    result = service._observe_worker(link)
    assert result.code == code and result.recovery_required is recovery
    assert result.observed == 0 and result.run_id is None
    assert calls == [("require", link)]


@pytest.mark.parametrize("status", ("starting", "unknown"))
def test_uncertain_original_still_queries_exact_start(monkeypatch, status):
    link = unadmitted_link(status)
    calls = []
    service = new_reconciler(
        lambda: pytest.fail("Not-found original opened a write transaction")
    )

    def require_original(expected):
        assert expected is link
        calls.append("require")
        return SimpleNamespace()

    def get_start(workspace_id, operation_id, *, grant_id):
        calls.append((workspace_id, operation_id, grant_id))
        return StartLookup(found=False, receipt=None)

    monkeypatch.setattr(service, "_require_original", require_original)
    service._services.execution = SimpleNamespace(get_start=get_start)
    result = service._observe_worker(link)
    assert result.code == "native_admission_not_found" and result.recovery_required
    assert result.observed == 0
    assert calls == [
        "require",
        (link.workspace_id, link.start_operation_id, link.authority_id),
    ]


@pytest.mark.parametrize(
    "status, changes",
    (
        ("pending", {"request_sha256": "a" * 64}),
        ("pending", {"event_cursor": 1}),
        ("pending", {"run_id": uuid4()}),
        ("failed", {"request_sha256": None}),
        ("failed", {"start_receipt": None}),
        ("failed", {"event_cursor": 1}),
        ("failed", {"run_id": uuid4()}),
        ("starting", {}),
        ("unknown", {}),
    ),
)
def test_contradictory_or_uncertain_link_is_not_known_unadmitted(status, changes):
    assert not replace(unadmitted_link(status), **changes).known_unadmitted


@pytest.mark.parametrize("limit", (0, -1, True, 1.5, 201))
def test_invalid_limit_rejects_before_query_or_transaction(limit):
    def no_transaction():
        pytest.fail("invalid limit opened a transaction")

    service = new_reconciler(no_transaction)
    with pytest.raises(ValueError):
        service.run_once(uuid4(), limit=limit)


def test_empty_original_selection_has_no_business_effect():
    uow = SimpleNamespace(
        execution_authorities=SimpleNamespace(
            list_reconciliation_links=lambda workspace_id, **kwargs: ()
        )
    )
    result = new_reconciler(lambda: nullcontext(uow)).run_once(uuid4(), limit=2)
    assert result.examined == result.observed == result.recovery_required == 0
    assert result.outcomes == ()


def test_owner_wide_scan_passes_saved_workspace_to_original_action(monkeypatch):
    from sidera_control_plane.application.agent_reconciliation import ReconcileOutcome

    workspaces = (uuid4(), uuid4())
    links = tuple(
        SimpleNamespace(
            workspace_id=workspace,
            execution_id=uuid4(),
            start_operation_id=uuid4(),
            run_id=uuid4(),
        )
        for workspace in workspaces
    )
    selections, observed = [], []

    def select_originals(scope, **kwargs):
        selections.append(scope)
        return links

    service = new_reconciler(
        lambda: nullcontext(
            SimpleNamespace(
                execution_authorities=SimpleNamespace(
                    list_reconciliation_links=select_originals
                ),
            )
        )
    )

    def observe(link):
        observed.append(link.workspace_id)
        return ReconcileOutcome(
            link.execution_id, link.start_operation_id, link.run_id, "observed"
        )

    monkeypatch.setattr(service, "_reconcile_worker", observe)
    assert service.run_once(None, limit=2).examined == 2
    assert selections == [None] and observed == list(workspaces)


@pytest.mark.parametrize("uncertain", [False, True])
def test_bounded_scan_visits_later_original_even_after_uncertainty(
    monkeypatch, uncertain
):
    from sidera_control_plane.application.agent_reconciliation import ReconcileOutcome

    ids = sorted((uuid4(), uuid4()))
    links = [
        SimpleNamespace(execution_id=id, start_operation_id=uuid4(), run_id=uuid4())
        for id in ids
    ]
    selections, queried = [], []

    def select(workspace_id, *, limit, after_execution_id=None):
        selections.append((limit, after_execution_id))
        return tuple(
            link
            for link in links
            if after_execution_id is None or link.execution_id > after_execution_id
        )[:limit]

    service = new_reconciler(
        lambda: nullcontext(
            SimpleNamespace(
                execution_authorities=SimpleNamespace(list_reconciliation_links=select)
            )
        )
    )

    def observe(link):
        queried.append(link.execution_id)
        if uncertain and link.execution_id == ids[0]:
            raise TimeoutError("controlled original uncertainty")
        return ReconcileOutcome(
            link.execution_id, link.start_operation_id, link.run_id, "observed"
        )

    monkeypatch.setattr(service, "_reconcile_worker", observe)
    workspace = uuid4()
    for _ in range(3):
        assert service.run_once(workspace, limit=1).examined == 1
    assert queried == [ids[0], ids[1], ids[0]]
    assert len(selections) <= 4 and all(limit == 1 for limit, _ in selections)


@pytest.mark.parametrize("uncertain_family", [None, "worker", "planner"])
def test_mixed_families_alternate_first_priority(monkeypatch, uncertain_family):
    from sidera_control_plane.application.agent_reconciliation import ReconcileOutcome

    workers = [
        SimpleNamespace(
            execution_id=UUID(int=id), start_operation_id=uuid4(), run_id=uuid4()
        )
        for id in (1, 2, 3)
    ]
    planner = SimpleNamespace(
        planning_run_id=UUID(int=100), start_operation_id=uuid4(), run_id=uuid4(), status="running",
    )
    calls, queried = [], []

    def worker_select(workspace, *, limit, after_execution_id=None):
        calls.append(("worker", limit))
        assert limit > 0
        return tuple(
            link
            for link in workers
            if after_execution_id is None or link.execution_id > after_execution_id
        )[:limit]

    def planner_select(workspace, *, limit, after_planning_run_id=None):
        calls.append(("planner", limit))
        assert limit > 0
        return () if after_planning_run_id is not None else (planner,)

    uow = SimpleNamespace(
        execution_authorities=SimpleNamespace(list_reconciliation_links=worker_select),
        planning_authorities=SimpleNamespace(list_reconciliation_links=planner_select),
    )
    service = new_reconciler(lambda: nullcontext(uow))

    def observe_worker(link):
        queried.append(("worker", link.execution_id))
        if uncertain_family == "worker":
            raise TimeoutError()
        return ReconcileOutcome(
            link.execution_id, link.start_operation_id, link.run_id, "observed"
        )

    def observe_planner(link):
        queried.append(("planner", link.planning_run_id))
        if uncertain_family == "planner":
            raise TimeoutError()
        return SimpleNamespace(status="succeeded")

    monkeypatch.setattr(service, "_reconcile_worker", observe_worker)
    monkeypatch.setattr(service, "_require_planning_original", lambda link: None)
    service._planning_runner = SimpleNamespace(reconcile_original_run=observe_planner)
    workspace = uuid4()
    for _ in range(4):
        assert service.run_once(workspace, limit=1).examined == 1
    assert queried == [
        ("worker", workers[0].execution_id),
        ("planner", planner.planning_run_id),
        ("worker", workers[1].execution_id),
        ("planner", planner.planning_run_id),
    ]


def test_no_worker_owner_excludes_worker_family():
    def forbidden(*args, **kwargs):
        pytest.fail("no-owner Worker family selected")

    service = new_reconciler(
        lambda: nullcontext(
            SimpleNamespace(
                execution_authorities=SimpleNamespace(
                    list_reconciliation_links=forbidden
                ),
                planning_authorities=SimpleNamespace(
                    list_reconciliation_links=lambda *args, **kwargs: ()
                ),
            )
        )
    )
    service._gateway = None
    service._planning_runner = SimpleNamespace()
    assert service.run_once(uuid4(), limit=1).examined == 0


@pytest.mark.parametrize("complete", (False, True))
def test_cleanup_pending_planner_observer_only_queries_drain_not_business_result(
    monkeypatch, complete
):
    calls = []
    link = SimpleNamespace(
        planning_run_id=uuid4(),
        start_operation_id=uuid4(),
        run_id=uuid4(),
        status="cleanup_pending",
    )
    uow = SimpleNamespace(
        planning_authorities=SimpleNamespace(
            list_reconciliation_links=lambda *args, **kwargs: (link,),
        )
    )
    service = new_reconciler(lambda: nullcontext(uow))
    service._gateway = None

    def forbidden_result(expected):
        calls.append("result")
        pytest.fail("Cleanup intent was passed to business result recovery")

    def drain(expected):
        assert expected is link
        calls.append("original_drain")
        return complete

    service._planning_runner = SimpleNamespace(
        reconcile_original_run=forbidden_result,
        drain_original_run=drain,
    )
    monkeypatch.setattr(service, "_require_planning_original", lambda expected: None)

    def observe_original(expected):
        assert expected is link
        calls.append("original_observation")

    monkeypatch.setattr(service, "_observe_planning_original", observe_original)
    result = service.run_once(uuid4(), limit=1)
    assert calls == (
        ["original_drain"] if complete else ["original_drain", "original_observation"]
    )
    assert result.examined == 1 and result.recovery_required == int(not complete)


@pytest.mark.parametrize(
    "case, eligible",
    (
        ("missing", False),
        ("workspace", False),
        ("id", False),
        ("run", False),
        ("owner", False),
        ("fence", False),
        ("run_workspace", False),
        ("authority_run", False),
        ("expired", False),
        ("elapsed_active", False),
        ("unsupported", False),
        ("live", True),
        ("released", True),
    ),
)
def test_planner_original_drain_checks_exact_lease_before_adapter(
    monkeypatch, case, eligible
):
    from sidera_control_plane.application.agent_planner_adapter import (
        AgentPlannerAdapter,
    )
    from sidera_control_plane.application.planning_runner import PlanningRunner

    now = datetime(2026, 10, 4, tzinfo=UTC)
    workspace_id, run_id, request_id, lease_id, authority_id, operation_id = (
        uuid4() for _ in range(6)
    )
    profile_ref = ProfileRef(workspace_id=workspace_id, profile_id=uuid4(), revision=1)
    expected = SimpleNamespace(
        workspace_id=workspace_id,
        planning_run_id=run_id,
        authority_id=authority_id,
        start_operation_id=operation_id,
        profile_ref=profile_ref,
    )
    authority = SimpleNamespace(
        id=authority_id,
        workspace_id=workspace_id,
        planning_request_id=request_id,
        planning_run_id=run_id,
        lease_id=lease_id,
        owner_id="original-owner",
        fencing_token=3,
        revoked=case == "released",
    )
    profile = SimpleNamespace(
        execution_mode="native_profile", planner_agent_ref=profile_ref
    )
    run = SimpleNamespace(
        id=run_id,
        workspace_id=workspace_id,
        planning_request_id=request_id,
        operation_id=operation_id,
        profile_snapshot=profile,
        lease_id=None,
        fencing_token=0,
    )
    lease = PlanningRunLease(
        id=lease_id,
        workspace_id=workspace_id,
        planning_run_id=run_id,
        owner_id="original-owner",
        status=PlanningRunLeaseStatus.ACTIVE,
        fencing_token=3,
        acquired_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=60),
        released_at=None,
        version=1,
    )
    if case in {"workspace", "id", "run"}:
        setattr(
            lease,
            {"workspace": "workspace_id", "id": "id", "run": "planning_run_id"}[case],
            uuid4(),
        )
    elif case == "owner":
        lease.owner_id = "foreign-owner"
    elif case == "fence":
        lease.fencing_token += 1
    elif case == "run_workspace":
        run.workspace_id = uuid4()
    elif case == "authority_run":
        authority.planning_run_id = uuid4()
    elif case == "expired":
        lease.status = PlanningRunLeaseStatus.EXPIRED
    elif case == "elapsed_active":
        lease.expires_at = now
    elif case == "unsupported":
        lease.status = "unsupported"
    elif case == "released":
        lease.status = PlanningRunLeaseStatus.RELEASED
        lease.expires_at = now - timedelta(seconds=1)
        lease.released_at = now
    before_lease = asdict(lease)
    before_run = dict(vars(run))
    calls = []

    def get_lease(actual_id, actual_workspace):
        assert (actual_id, actual_workspace) == (lease_id, workspace_id)
        calls.append("lease")
        return None if case == "missing" else lease

    uow = SimpleNamespace(
        planning_authorities=SimpleNamespace(get=lambda *args, **kwargs: authority),
        planning_runs=SimpleNamespace(get=lambda *args: run),
        planning_leases=SimpleNamespace(get=get_lease),
    )
    adapter = object.__new__(AgentPlannerAdapter)

    def create(definition, instance, snapshot):
        assert eligible and definition is instance is None and snapshot is profile
        calls.append("adapter")
        return adapter

    def drain(self, actual):
        assert self is adapter and actual is expected
        calls.append("original_stop")
        return True

    monkeypatch.setattr(AgentPlannerAdapter, "drain_original_run", drain)
    runner = PlanningRunner(
        uow_factory=lambda: nullcontext(uow),
        adapter_factory=SimpleNamespace(create=create),
        owner_id="restarted-owner",
        clock=lambda: now,
    )
    assert runner.drain_original_run(expected) is eligible
    assert calls == (["lease", "adapter", "original_stop"] if eligible else ["lease"])
    assert asdict(lease) == before_lease and vars(run) == before_run
    assert (run.lease_id, run.fencing_token) == (None, 0)


def test_uow_commit_guard_failure_rolls_back_and_closes():
    from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

    calls = []

    def guard():
        calls.append("guard")
        raise RuntimeError("detected owner loss")

    uow = SqlAlchemyUnitOfWork(SimpleNamespace(), before_commit=guard)
    uow.session = SimpleNamespace(
        commit=lambda: calls.append("commit"),
        rollback=lambda: calls.append("rollback"),
        close=lambda: calls.append("close"),
    )
    with pytest.raises(RuntimeError, match="detected owner loss"):
        uow.__exit__(None, None, None)
    assert calls == ["guard", "rollback", "close"]
