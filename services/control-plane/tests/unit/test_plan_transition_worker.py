from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sidera_control_plane import plan_transition_worker
from sidera_control_plane.application.plan_transition_coordinator import (
    PlanTransitionRunResult,
)


class RecordingCoordinator:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def run_once(self, limit: int) -> PlanTransitionRunResult:
        self.calls.append(limit)
        return PlanTransitionRunResult(
            claimed=1,
            converged=1,
            deferred=0,
            failed=0,
            reconciled=1,
        )


class RecordingDatabase:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def fake_settings() -> Any:
    return SimpleNamespace(
        database_url="postgresql+psycopg://test",
        plan_transition_batch_size=12,
        plan_transition_poll_interval_seconds=1.5,
    )


def patch_runtime(monkeypatch: Any, coordinator: RecordingCoordinator) -> RecordingDatabase:
    database = RecordingDatabase()
    runtime = SimpleNamespace(coordinator=coordinator, database=database)
    monkeypatch.setattr(plan_transition_worker, "Settings", fake_settings)
    monkeypatch.setattr(
        plan_transition_worker,
        "build_plan_transition_worker",
        lambda _settings: runtime,
    )
    return database


def test_plan_transition_worker_once_runs_one_batch(monkeypatch: Any) -> None:
    coordinator = RecordingCoordinator()
    database = patch_runtime(monkeypatch, coordinator)

    exit_code = plan_transition_worker.main(["--once"])

    assert exit_code == 0
    assert coordinator.calls == [12]
    assert database.disposed is True


def test_plan_transition_worker_continuous_mode_sleeps_after_a_batch(
    monkeypatch: Any,
) -> None:
    coordinator = RecordingCoordinator()
    database = patch_runtime(monkeypatch, coordinator)
    sleeps: list[float] = []

    def stop_after_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(plan_transition_worker.time, "sleep", stop_after_sleep)

    exit_code = plan_transition_worker.main([])

    assert exit_code == 0
    assert coordinator.calls == [12]
    assert sleeps == [1.5]
    assert database.disposed is True
