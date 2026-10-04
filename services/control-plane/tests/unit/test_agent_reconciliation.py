import importlib
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest


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
        planning_run_id=UUID(int=100), start_operation_id=uuid4(), run_id=uuid4()
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
