from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from sidera_control_plane import gateway_owner, handoff_worker, input_dispatcher_worker
from sidera_control_plane.gateway_worker import GatewayRuntime


def _runtime() -> GatewayRuntime:
    return GatewayRuntime(
        gateway=object(),  # type: ignore[arg-type]
        worker_gateway=object(),  # type: ignore[arg-type]
        database=object(),  # type: ignore[arg-type]
        catalog=object(),  # type: ignore[arg-type]
    )


def test_input_dispatcher_uses_injected_gateway_runtime(monkeypatch) -> None:
    runtime = _runtime()
    captured: dict[str, object] = {}

    class _Dispatcher:
        def __init__(self, uow_factory, gateway, *, clock):
            captured["uow_factory"] = uow_factory
            captured["gateway"] = gateway
            captured["clock"] = clock

    monkeypatch.setattr(input_dispatcher_worker, "InputDispatcher", _Dispatcher)
    settings = SimpleNamespace(projection_change_retention_seconds=42)

    result = input_dispatcher_worker.build_input_dispatcher(
        settings, gateway_runtime=runtime
    )

    assert result.gateway_runtime is runtime
    assert captured["gateway"] is runtime.worker_gateway


@pytest.mark.parametrize("shared", [False, True])
def test_handoff_worker_uses_injected_gateway_runtime(monkeypatch, shared) -> None:
    runtime = _runtime()
    captured: dict[str, object] = {}
    provider = object()
    if shared:
        runtime.workspace_provider = provider  # type: ignore[assignment]

    class _UnitOfWorkFactory:
        def __call__(self):
            return object()

    class _Coordinator:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(handoff_worker, "HandoffCoordinator", _Coordinator)

    def manager(*args, **kwargs):
        assert not shared, "a supplied workspace provider must not be replaced"
        return provider

    monkeypatch.setattr(handoff_worker, "GitWorktreeWorkspaceManager", manager)
    monkeypatch.setattr(handoff_worker, "Scheduler", lambda **kwargs: object())
    monkeypatch.setattr(
        handoff_worker,
        "WorkspaceHandoffService",
        lambda supplied, **kwargs: SimpleNamespace(provider=supplied),
    )
    monkeypatch.setattr(handoff_worker, "HandoffTargetService", lambda *args: object())
    monkeypatch.setattr(
        handoff_worker,
        "HandoffContextService",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        handoff_worker,
        "HandoffInputTransferService",
        lambda *args: object(),
    )

    settings = SimpleNamespace(
        projection_change_retention_seconds=42,
        workspace_root="/tmp/sidera-test-workspaces",
        workspace_retention_seconds=60,
        git_executable="git",
        assignment_lease_seconds=30,
        worker_heartbeat_timeout_seconds=60,
        context_package_cutover_at=None,
        execution_context_ttl_seconds=600,
        local_actor_id=uuid4(),
        handoff_lease_ttl_seconds=30,
        handoff_backoff_max_seconds=300,
    )

    result = handoff_worker.build_handoff_worker(settings, gateway_runtime=runtime)

    assert result.database is runtime.database
    assert result.catalog is runtime.catalog
    assert captured["gateway"] is runtime.worker_gateway
    assert captured["workspace_handoff"].provider is provider


def test_native_handoff_factory_requires_owned_workspace_provider() -> None:
    with pytest.raises(
        ValueError, match="^native_handoff_workspace_provider_required$"
    ):
        handoff_worker.build_handoff_worker(
            SimpleNamespace(),
            gateway_runtime=_runtime(),
            native_services=object(),  # type: ignore[arg-type]
        )


def test_gateway_owner_builds_consumers_from_one_runtime(monkeypatch) -> None:
    runtime = _runtime()
    calls: list[tuple[str, object]] = []

    class _Database:
        def dispose(self) -> None:
            return None

    class _Lock:
        def __init__(self, database) -> None:
            del database

        def acquire(self) -> bool:
            return True

        def close(self) -> None:
            return None

    monkeypatch.setattr(gateway_owner, "Database", lambda _url: _Database())
    monkeypatch.setattr(gateway_owner, "GatewayOwnerLock", _Lock)
    monkeypatch.setattr(gateway_owner, "build_gateway", lambda _settings: runtime)
    monkeypatch.setattr(
        gateway_owner,
        "build_input_dispatcher",
        lambda _settings, *, gateway_runtime: (
            calls.append(("input", gateway_runtime)) or SimpleNamespace()
        ),
    )
    monkeypatch.setattr(
        gateway_owner,
        "build_handoff_worker",
        lambda _settings, *, gateway_runtime: (
            calls.append(("handoff", gateway_runtime)) or SimpleNamespace()
        ),
    )

    owner = gateway_owner.build_gateway_owner(
        SimpleNamespace(database_url="postgresql+psycopg://test")
    )

    assert calls == [("input", runtime), ("handoff", runtime)]
    assert owner.gateway_runtime is runtime


def test_gateway_owner_acquires_database_lock_before_consumable_runtime(
    monkeypatch,
) -> None:
    events: list[str] = []

    class _Database:
        def dispose(self) -> None:
            events.append("lock-database-dispose")

    class _Lock:
        def __init__(self, database) -> None:
            assert isinstance(database, _Database)
            events.append("lock-created")

        def acquire(self) -> bool:
            events.append("lock-acquired")
            return True

        def close(self) -> None:
            events.append("lock-closed")

    class _Runtime:
        def close(self) -> None:
            events.append("runtime-closed")

    runtime = _Runtime()
    monkeypatch.setattr(gateway_owner, "Database", lambda _url: _Database())
    monkeypatch.setattr(gateway_owner, "GatewayOwnerLock", _Lock)
    monkeypatch.setattr(
        gateway_owner,
        "build_gateway",
        lambda _settings: events.append("runtime-built") or runtime,
    )
    monkeypatch.setattr(
        gateway_owner,
        "build_input_dispatcher",
        lambda _settings, *, gateway_runtime: events.append("input-built") or object(),
    )
    monkeypatch.setattr(
        gateway_owner,
        "build_handoff_worker",
        lambda _settings, *, gateway_runtime: (
            events.append("handoff-built") or object()
        ),
    )

    owner = gateway_owner.build_gateway_owner(
        SimpleNamespace(database_url="postgresql+psycopg://test")
    )

    assert events[:3] == ["lock-created", "lock-acquired", "runtime-built"]
    owner.close()
    assert events[-2:] == ["runtime-closed", "lock-closed"]


def test_gateway_owner_refuses_when_database_lock_is_held(monkeypatch) -> None:
    class _Database:
        pass

    class _Lock:
        def __init__(self, database) -> None:
            del database

        def acquire(self) -> bool:
            return False

        def close(self) -> None:
            return None

    built = False

    def build(_settings):
        nonlocal built
        built = True
        return _runtime()

    monkeypatch.setattr(gateway_owner, "Database", lambda _url: _Database())
    monkeypatch.setattr(gateway_owner, "GatewayOwnerLock", _Lock)
    monkeypatch.setattr(gateway_owner, "build_gateway", build)

    with pytest.raises(gateway_owner.GatewayOwnerAlreadyRunning):
        gateway_owner.build_gateway_owner(
            SimpleNamespace(database_url="postgresql+psycopg://test")
        )
    assert built is False
