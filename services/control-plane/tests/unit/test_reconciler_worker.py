from types import SimpleNamespace
from typing import Any

from sidera_control_plane import reconciler_worker


class RecordingReconciler:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def run_once(self, limit: int) -> Any:
        self.calls.append(limit)
        return SimpleNamespace(
            offline_workers=0,
            expired_leases=0,
            retries_queued=0,
        )


class RecordingDatabase:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def fake_settings() -> Any:
    return SimpleNamespace(
        database_url="postgresql+psycopg://test",
        worker_heartbeat_timeout_seconds=45,
        reconciler_batch_size=50,
        reconciler_poll_interval_seconds=2.0,
    )


def patch_runtime(monkeypatch: Any, service: Any) -> RecordingDatabase:
    database = RecordingDatabase()
    runtime = SimpleNamespace(reconciler=service, database=database)
    monkeypatch.setattr(reconciler_worker, "Settings", fake_settings)
    monkeypatch.setattr(
        reconciler_worker,
        "build_reconciler",
        lambda _settings: runtime,
    )
    return database


def test_reconciler_once_runs_one_batch(monkeypatch: Any) -> None:
    reconciler = RecordingReconciler()
    database = patch_runtime(monkeypatch, reconciler)

    exit_code = reconciler_worker.main(["--once"])

    assert exit_code == 0
    assert reconciler.calls == [50]
    assert database.disposed is True


def test_reconciler_continuous_mode_sleeps_after_a_batch(monkeypatch: Any) -> None:
    reconciler = RecordingReconciler()
    database = patch_runtime(monkeypatch, reconciler)
    sleeps: list[float] = []

    def stop_after_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(reconciler_worker.time, "sleep", stop_after_sleep)

    exit_code = reconciler_worker.main([])

    assert exit_code == 0
    assert reconciler.calls == [50]
    assert sleeps == [2.0]
    assert database.disposed is True


def test_reconciler_keyboard_interrupt_during_batch_disposes_database(
    monkeypatch: Any,
) -> None:
    reconciler = RecordingReconciler()
    database = patch_runtime(monkeypatch, reconciler)

    def interrupt(_limit: int) -> Any:
        raise KeyboardInterrupt

    reconciler.run_once = interrupt  # type: ignore[method-assign]

    exit_code = reconciler_worker.main([])

    assert exit_code == 0
    assert database.disposed is True


def test_reconciler_failure_returns_non_zero(monkeypatch: Any) -> None:
    reconciler = RecordingReconciler()
    database = patch_runtime(monkeypatch, reconciler)

    def fail(_limit: int) -> Any:
        raise RuntimeError("reconciler unavailable")

    reconciler.run_once = fail  # type: ignore[method-assign]

    exit_code = reconciler_worker.main(["--once"])

    assert exit_code == 1
    assert database.disposed is True
