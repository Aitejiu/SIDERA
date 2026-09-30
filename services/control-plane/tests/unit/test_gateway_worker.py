from types import SimpleNamespace
from typing import Any
from uuid import UUID

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
