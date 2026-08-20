from types import SimpleNamespace
from typing import Any

from sidera_control_plane import scheduler_worker


class RecordingScheduler:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def run_once(self, limit: int) -> Any:
        self.calls.append(limit)
        return SimpleNamespace(examined=0, assigned=0, deferred=0)


class RecordingDatabase:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def fake_settings() -> Any:
    return SimpleNamespace(
        database_url="postgresql+psycopg://test",
        assignment_lease_seconds=30,
        worker_heartbeat_timeout_seconds=45,
        scheduler_batch_size=20,
        scheduler_poll_interval_seconds=1.0,
    )


def patch_runtime(monkeypatch: Any, service: Any) -> RecordingDatabase:
    database = RecordingDatabase()
    runtime = SimpleNamespace(scheduler=service, database=database)
    monkeypatch.setattr(scheduler_worker, "Settings", fake_settings)
    monkeypatch.setattr(
        scheduler_worker,
        "build_scheduler",
        lambda _settings: runtime,
    )
    return database


def test_scheduler_once_runs_one_batch(monkeypatch: Any) -> None:
    scheduler = RecordingScheduler()
    database = patch_runtime(monkeypatch, scheduler)

    exit_code = scheduler_worker.main(["--once"])

    assert exit_code == 0
    assert scheduler.calls == [20]
    assert database.disposed is True


def test_scheduler_continuous_mode_sleeps_after_a_batch(monkeypatch: Any) -> None:
    scheduler = RecordingScheduler()
    database = patch_runtime(monkeypatch, scheduler)
    sleeps: list[float] = []

    def stop_after_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(scheduler_worker.time, "sleep", stop_after_sleep)

    exit_code = scheduler_worker.main([])

    assert exit_code == 0
    assert scheduler.calls == [20]
    assert sleeps == [1.0]
    assert database.disposed is True


def test_scheduler_keyboard_interrupt_during_batch_disposes_database(
    monkeypatch: Any,
) -> None:
    scheduler = RecordingScheduler()
    database = patch_runtime(monkeypatch, scheduler)

    def interrupt(_limit: int) -> Any:
        raise KeyboardInterrupt

    scheduler.run_once = interrupt  # type: ignore[method-assign]

    exit_code = scheduler_worker.main([])

    assert exit_code == 0
    assert database.disposed is True


def test_scheduler_failure_returns_non_zero(monkeypatch: Any) -> None:
    scheduler = RecordingScheduler()
    database = patch_runtime(monkeypatch, scheduler)

    def fail(_limit: int) -> Any:
        raise RuntimeError("scheduler unavailable")

    scheduler.run_once = fail  # type: ignore[method-assign]

    exit_code = scheduler_worker.main(["--once"])

    assert exit_code == 1
    assert database.disposed is True
