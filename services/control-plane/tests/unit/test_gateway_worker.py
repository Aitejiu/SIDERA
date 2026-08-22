from types import SimpleNamespace
from typing import Any

from sidera_control_plane import gateway_worker


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def run_once(self, limit: int) -> Any:
        self.calls.append(limit)
        return (2, 1, 1)


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
        workspace_root=None,
        workspace_retention_seconds=86_400,
        git_executable="git",
        opencode_executable="opencode",
    )


def patch_runtime(monkeypatch: Any, runner: Any) -> RecordingDatabase:
    database = RecordingDatabase()
    runtime = SimpleNamespace(gateway=runner, database=database)
    monkeypatch.setattr(gateway_worker, "Settings", fake_settings)
    monkeypatch.setattr(gateway_worker, "build_gateway", lambda _settings: runtime)
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
