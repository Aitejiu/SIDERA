from contextlib import nullcontext
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sidera_agents.errors import AgentError

from sidera_control_plane.config import Settings

FACTORIES = (
    (
        "scheduler_worker",
        "build_scheduler",
        lambda runtime: runtime.scheduler._uow_factory,
    ),
    (
        "activation_worker",
        "build_activation_worker",
        lambda runtime: runtime.worker._reconciler._uow_factory,
    ),
    (
        "context_package_worker",
        "build_context_package_worker",
        lambda runtime: runtime.worker._uow_factory,
    ),
    (
        "collaboration_worker",
        "build_collaboration_worker",
        lambda runtime: runtime.worker._reconciler._uow_factory,
    ),
    (
        "plan_transition_worker",
        "build_plan_transition_worker",
        lambda runtime: runtime.coordinator._uow_factory,
    ),
    (
        "result_worker",
        "build_result_worker",
        lambda runtime: runtime.worker._uow_factory,
    ),
    (
        "reconciler_worker",
        "build_reconciler",
        lambda runtime: runtime.reconciler._uow_factory,
    ),
    (
        "notification_worker",
        "build_notification_worker",
        lambda runtime: runtime.reconciler._reconcilers[0]._uow_factory,
    ),
)


@pytest.mark.parametrize("module_name,builder_name,unit_of_work", FACTORIES)
def test_existing_business_factory_fences_initial_and_final_owned_uow(
    module_name, builder_name, unit_of_work, monkeypatch
):
    module = import_module("sidera_control_plane." + module_name)
    disposed, transactions, checks = [], [], []
    database = SimpleNamespace(dispose=lambda: disposed.append(True))
    monkeypatch.setattr(module, "Database", lambda url: database)
    catalog = SimpleNamespace(close=lambda: None)
    if module_name == "context_package_worker":
        monkeypatch.setattr(module, "KnowledgeHttpAdapter", lambda **kwargs: catalog)

    def transaction(database, **kwargs):
        transactions.append(kwargs)
        return nullcontext(SimpleNamespace())

    monkeypatch.setattr(module, "SqlAlchemyUnitOfWork", transaction)
    alive = [True]

    def owner_check():
        checks.append(alive[0])
        return alive[0]

    settings = Settings(
        database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused"
    )
    runtime = getattr(module, builder_name)(settings, owner_check=owner_check)
    try:
        assert transactions == [] and checks == []
        factory = unit_of_work(runtime)
        with factory():
            pass
        assert checks == [True]
        fence = transactions[0]["before_commit"]
        assert callable(fence)
        alive[0] = False
        with pytest.raises(AgentError) as denied:
            fence()
        assert denied.value.code == "gateway_owner_lost"
        with pytest.raises(AgentError) as initial:
            factory()
        assert initial.value.code == "gateway_owner_lost"
        assert len(transactions) == 1
    finally:
        if module_name == "context_package_worker":
            runtime.catalog.close()
        runtime.database.dispose()
    assert disposed == [True]


def test_scheduler_factory_keeps_same_native_services_without_native_calls(monkeypatch):
    from sidera_control_plane.application.agent_worker_adapter import (
        NativeWorkerServices,
    )
    from sidera_control_plane.scheduler_worker import build_scheduler
    from tests.unit.test_native_request_authority import NoNativeCalls

    native = NoNativeCalls()
    services = NativeWorkerServices(native, native, native, native, "operator", uuid4())
    runtime = build_scheduler(
        Settings(
            database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused"
        ),
        native_services=services,
    )
    try:
        assert runtime.scheduler._runtime_resolver._native_services is services
        assert not native.calls
    finally:
        runtime.database.dispose()


@pytest.mark.parametrize("scan_all_workspaces", (False, True))
def test_collaboration_factory_preserves_legacy_hint_and_native_ignores_it(
    scan_all_workspaces,
):
    from sidera_control_plane.collaboration_worker import build_collaboration_worker

    workspace_id = uuid4()
    settings = Settings(
        database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused",
        collaboration_workspace_id=workspace_id,
    )
    runtime = build_collaboration_worker(
        settings, scan_all_workspaces=scan_all_workspaces
    )
    try:
        reconciler = runtime.worker._reconciler
        assert reconciler._scan_all_workspaces is scan_all_workspaces
        assert reconciler._default_workspace_id == (
            None if scan_all_workspaces else workspace_id
        )
        assert settings.collaboration_workspace_id == workspace_id
    finally:
        runtime.database.dispose()
