from __future__ import annotations

from types import SimpleNamespace

from sidera_control_plane import handoff_worker
from sidera_control_plane.application.handoff_coordinator import HandoffRunResult


class _Coordinator:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def run_once(self, limit: int) -> HandoffRunResult:
        self.calls.append(limit)
        return HandoffRunResult(
            claimed=1,
            progressed=1,
            waiting=0,
            converged=0,
            blocked=0,
            failed=0,
            unknown=0,
        )


class _Database:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def _patch_runtime(monkeypatch, coordinator: _Coordinator) -> _Database:
    database = _Database()
    monkeypatch.setattr(
        handoff_worker,
        "Settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg://test",
            handoff_batch_size=12,
            handoff_poll_interval_seconds=1.5,
        ),
    )
    monkeypatch.setattr(
        handoff_worker,
        "build_handoff_worker",
        lambda _settings: SimpleNamespace(coordinator=coordinator, database=database),
    )
    return database


def test_handoff_worker_standalone_refuses_before_claiming(monkeypatch) -> None:
    coordinator = _Coordinator()
    database = _patch_runtime(monkeypatch, coordinator)

    assert handoff_worker.main(["--once"]) == 1
    assert coordinator.calls == []
    assert database.disposed is False


def test_handoff_worker_standalone_continuous_mode_refuses_before_sleep(monkeypatch) -> None:
    coordinator = _Coordinator()
    database = _patch_runtime(monkeypatch, coordinator)
    sleeps: list[float] = []

    def stop_after_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(handoff_worker.time, "sleep", stop_after_sleep)

    assert handoff_worker.main([]) == 1
    assert coordinator.calls == []
    assert sleeps == []
    assert database.disposed is False
