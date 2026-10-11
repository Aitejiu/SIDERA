from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from sidera_control_plane import gateway_worker


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def run_once(self, limit: int) -> Any:
        self.calls.append(limit)
        return (2, 1, 1)


class RecordingInputDispatcher:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def run_once(self, limit: int) -> Any:
        self.calls.append(limit)
        return SimpleNamespace(
            claimed=0,
            delivered=0,
            deferred=0,
            blocked=0,
            unknown=0,
            failed=0,
        )


class RecordingHandoffCoordinator:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def run_once(self, limit: int) -> Any:
        self.calls.append(limit)
        return SimpleNamespace(
            claimed=0,
            progressed=0,
            waiting=0,
            converged=0,
            blocked=0,
            failed=0,
            unknown=0,
        )


class RecordingDatabase:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def fake_settings() -> Any:
    return SimpleNamespace(
        database_url="postgresql+psycopg://test",
        gateway_batch_size=15,
        gateway_poll_interval_seconds=1.5,
        handoff_batch_size=7,
        workspace_root=None,
        workspace_retention_seconds=86_400,
        git_executable="git",
        opencode_executable="opencode",
        opencode_request_timeout_seconds=60.0,
        opencode_stop_grace_seconds=5.0,
    )


def test_active_session_sync_scope_uses_explicit_workspace_without_demo_manifest() -> None:
    workspace_id = UUID("6b75d8d9-6042-40d8-a393-c9969037472f")
    settings = SimpleNamespace(
        interview_demo_manifest=None,
        gateway_sync_workspace_id=workspace_id,
    )

    assert gateway_worker._resolve_active_session_sync_workspace(settings) == workspace_id


def patch_runtime(monkeypatch: Any, runner: Any) -> RecordingDatabase:
    database = RecordingDatabase()
    runtime = SimpleNamespace(
        require_owner=lambda: None,
        gateway_runtime=SimpleNamespace(native_reconciler=None),
        gateway=runner,
        input_runtime=SimpleNamespace(dispatcher=RecordingInputDispatcher()),
        handoff_runtime=SimpleNamespace(coordinator=RecordingHandoffCoordinator()),
        close=database.dispose,
    )
    monkeypatch.setattr(gateway_worker, "Settings", fake_settings)
    monkeypatch.setattr(
        gateway_worker, "_build_gateway_owner", lambda _settings: runtime
    )
    return database


def test_gateway_once_runs_one_batch_and_disposes_database(monkeypatch: Any) -> None:
    runner = RecordingRunner()
    database = patch_runtime(monkeypatch, runner)

    exit_code = gateway_worker.main(["--once"])

    assert exit_code == 0
    assert runner.calls == [15]
    assert database.disposed is True


def test_gateway_continuous_mode_sleeps_after_a_batch(monkeypatch: Any) -> None:
    runner = RecordingRunner()
    database = patch_runtime(monkeypatch, runner)
    sleeps: list[float] = []

    def stop_after_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(gateway_worker.time, "sleep", stop_after_sleep)

    exit_code = gateway_worker.main([])

    assert exit_code == 0
    assert runner.calls == [15]
    assert sleeps == [1.5]
    assert database.disposed is True


def test_gateway_failure_returns_non_zero(monkeypatch: Any) -> None:
    runner = RecordingRunner()
    database = patch_runtime(monkeypatch, runner)

    def fail(_limit: int) -> Any:
        raise RuntimeError("gateway unavailable")

    runner.run_once = fail  # type: ignore[method-assign]

    exit_code = gateway_worker.main(["--once"])

    assert exit_code == 1
    assert database.disposed is True


@pytest.mark.parametrize("lost_in_first_phase", [False, True])
def test_owner_tick_stops_phases_after_detected_loss(lost_in_first_phase):
    from sidera_control_plane.gateway_owner import GatewayOwner

    alive = [lost_in_first_phase]
    runner = RecordingRunner()
    inputs, handoffs = RecordingInputDispatcher(), RecordingHandoffCoordinator()

    def first(limit):
        runner.calls.append(limit)
        alive[0] = False
        return (0, 0, 0)

    runner.run_once = first
    owner = GatewayOwner(
        gateway_runtime=SimpleNamespace(gateway=runner, native_reconciler=None),
        input_runtime=SimpleNamespace(dispatcher=inputs),
        handoff_runtime=SimpleNamespace(coordinator=handoffs),
        owner_lock=SimpleNamespace(check=lambda: alive[0]),
    )
    with pytest.raises(Exception, match="owner"):
        gateway_worker._run_owner_once(owner, fake_settings())
    assert runner.calls == ([15] if lost_in_first_phase else [])
    assert not inputs.calls and not handoffs.calls


def test_owner_tick_runs_one_bounded_native_observer_after_consumers():
    from uuid import uuid4

    from sidera_control_plane.gateway_owner import GatewayOwner

    calls = []
    workspace = uuid4()
    owner = GatewayOwner(
        gateway_runtime=SimpleNamespace(
            gateway=SimpleNamespace(
                run_once=lambda limit: calls.append(("gateway", limit)) or (0, 0, 0)
            ),
            native_workspace_id=workspace,
            native_reconciler=SimpleNamespace(
                run_once=lambda workspace_id, limit: (
                    calls.append(("observer", workspace_id, limit))
                    or SimpleNamespace(examined=1, observed=1, recovery_required=0)
                )
            ),
        ),
        input_runtime=SimpleNamespace(
            dispatcher=SimpleNamespace(
                run_once=lambda limit: (
                    calls.append(("input", limit))
                    or RecordingInputDispatcher().run_once(limit)
                )
            )
        ),
        handoff_runtime=SimpleNamespace(
            coordinator=SimpleNamespace(
                run_once=lambda limit: (
                    calls.append(("handoff", limit))
                    or RecordingHandoffCoordinator().run_once(limit)
                )
            )
        ),
        owner_lock=SimpleNamespace(check=lambda: True),
    )
    gateway_worker._run_owner_once(owner, fake_settings())
    assert calls == [
        ("gateway", 15),
        ("input", 15),
        ("handoff", 7),
        ("observer", workspace, 15),
    ]


def test_native_owner_tick_adapts_batch_201_only_for_observer():
    from uuid import uuid4

    from sidera_control_plane.gateway_owner import GatewayOwner

    settings = fake_settings()
    settings.gateway_batch_size = 201
    gateway, inputs, handoffs = (
        RecordingRunner(),
        RecordingInputDispatcher(),
        RecordingHandoffCoordinator(),
    )
    calls = []

    def observe(workspace, limit):
        assert limit == 200
        calls.append((workspace, limit))
        return SimpleNamespace(examined=0, observed=0, recovery_required=0)

    workspace = uuid4()
    owner = GatewayOwner(
        gateway_runtime=SimpleNamespace(
            gateway=gateway,
            native_reconciler=SimpleNamespace(run_once=observe),
            native_workspace_id=workspace,
        ),
        input_runtime=SimpleNamespace(dispatcher=inputs),
        handoff_runtime=SimpleNamespace(coordinator=handoffs),
        owner_lock=SimpleNamespace(check=lambda: True),
    )
    gateway_worker._run_owner_once(owner, settings)
    assert gateway.calls == inputs.calls == [201]
    assert handoffs.calls == [7] and calls == [(workspace, 200)]


def test_native_gateway_builder_does_not_load_ambient_acceptance_guard(
    monkeypatch, tmp_path
):
    import os

    from sidera_control_plane.config import Settings

    calls = []
    monkeypatch.setenv("SIDERA_ACCEPTANCE_GUARD_REQUIRED", "1")
    monkeypatch.setenv("SIDERA_ACCEPTANCE_MANIFEST", "/unused/private-old-manifest")
    before = dict(os.environ)

    def forbidden_guard():
        calls.append("load_guard")
        raise RuntimeError("old acceptance manifest must not be loaded")

    monkeypatch.setattr(gateway_worker, "load_acceptance_guard", forbidden_guard)
    runtime = gateway_worker.build_gateway(
        Settings(
            database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused",
            workspace_root=tmp_path,
            artifact_root=tmp_path / "artifacts",
            knowledge_candidate_spool_dir=tmp_path / "spool",
        ),
        use_acceptance_guard=False,
    )
    try:
        assert not calls
        assert dict(os.environ) == before
    finally:
        runtime.close()


def test_legacy_gateway_builder_preserves_guard_failure_and_owned_cleanup(monkeypatch):
    from sidera_control_plane.config import Settings

    calls = []
    monkeypatch.setattr(
        gateway_worker,
        "Database",
        lambda url: SimpleNamespace(
            dispose=lambda: calls.append("database_disposed"),
        ),
    )
    monkeypatch.setattr(
        gateway_worker,
        "KnowledgeHttpAdapter",
        lambda **kwargs: SimpleNamespace(
            close=lambda: calls.append("catalog_closed"),
        ),
    )

    def guard():
        calls.append("guard")
        raise RuntimeError("controlled missing acceptance manifest")

    monkeypatch.setattr(gateway_worker, "load_acceptance_guard", guard)
    with pytest.raises(RuntimeError, match="controlled missing"):
        gateway_worker.build_gateway(
            Settings(database_url="postgresql+psycopg://unused")
        )
    assert calls == ["guard", "catalog_closed", "database_disposed"]


@pytest.mark.parametrize(
    "feature", ("result_inputs_enabled", "result_integration_enabled")
)
def test_native_gateway_capture_uses_lazy_injected_repository_without_settings_path(
    tmp_path, feature
):
    from sidera_control_plane.config import Settings

    calls = []

    def resolve(ref):
        calls.append(("resolve", ref))
        pytest.fail("Gateway construction resolved a repository")

    def refs():
        calls.append("refs")
        pytest.fail("Gateway construction enumerated repositories")

    runtime = gateway_worker.build_gateway(
        Settings(
            database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused",
            workspace_root=tmp_path,
            artifact_root=tmp_path / "artifacts",
            knowledge_candidate_spool_dir=tmp_path / "spool",
            **{feature: True},
        ),
        use_acceptance_guard=False,
        repository_resolver=resolve,
        repository_refs=refs,
    )
    try:
        capture = runtime.worker_gateway._code_result_capture_service
        assert capture is not None
        assert capture._code_store._resolver is resolve
        assert capture._code_store._repository_refs is refs
        assert not calls
    finally:
        runtime.close()
