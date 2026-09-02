from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sidera_control_plane import collaboration_worker
from sidera_control_plane.application.collaboration_runtime_invoker import (
    RuntimeInvocationFailure,
)
from sidera_control_plane.collaboration_worker import (
    CollaborationWorker,
    CollaborationWorkerRunSummary,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


@dataclass
class FakeReconciler:
    prepared: list[object]
    applications: list[object]
    failures: list[object] = field(default_factory=list)
    successes: int = 0

    def prepare_next(self, **_: Any) -> object | None:
        return self.prepared.pop(0) if self.prepared else None

    def complete_success(self, prepared: object, result: object, *, now: datetime) -> bool:
        self.successes += 1
        return True

    def complete_decision(self, prepared: object, result: object, *, now: datetime) -> object:
        application = self.applications.pop(0)
        return application

    def complete_failure(
        self,
        prepared: object,
        failure: object,
        *,
        now: datetime,
        max_attempts: int,
        backoff_seconds: int,
        backoff_max_seconds: int,
    ) -> object:
        self.failures.append(failure)
        return SimpleNamespace(status="retry_wait" if failure.retryable else "blocked")


class FakeInvoker:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)

    def invoke(self, prepared: object, *, now: datetime) -> object:
        return self.results.pop(0)


def _result() -> object:
    return SimpleNamespace(response_hash="a" * 64)


def _failure(*, retryable: bool) -> RuntimeInvocationFailure:
    return RuntimeInvocationFailure(
        code="runtime_unavailable",
        response_hash=None,
        diagnostic_summary="bounded provider failure",
        retryable=retryable,
        duration_ms=10,
    )


def test_once_mode_prepares_invokes_and_applies_at_most_one_batch() -> None:
    reconciler = FakeReconciler(
        prepared=[SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=3)],
        applications=[
            SimpleNamespace(status="applied", releases=(object(),)),
            SimpleNamespace(status="no_change", releases=()),
        ],
    )
    worker = CollaborationWorker(
        reconciler=reconciler,
        invoker=FakeInvoker([_result(), _result(), _result()]),
        owner_id="test-collaboration-worker",
        clock=lambda: NOW,
    )

    summary = worker.run_once(2)

    assert isinstance(summary, CollaborationWorkerRunSummary)
    assert summary.evaluated == 2
    assert summary.released == 1
    assert summary.no_change == 1
    assert len(reconciler.prepared) == 1
    assert reconciler.successes == 2


def test_one_evaluation_failure_does_not_stop_the_batch() -> None:
    reconciler = FakeReconciler(
        prepared=[SimpleNamespace(id=1), SimpleNamespace(id=2)],
        applications=[SimpleNamespace(status="no_change", releases=())],
    )
    worker = CollaborationWorker(
        reconciler=reconciler,
        invoker=FakeInvoker([_failure(retryable=True), _result()]),
        owner_id="test-collaboration-worker",
        clock=lambda: NOW,
    )

    summary = worker.run_once(2)

    assert summary.evaluated == 2
    assert summary.retried == 1
    assert summary.no_change == 1
    assert summary.failed == 1
    assert len(reconciler.failures) == 1


def test_continuous_mode_runs_recovery_on_its_own_interval() -> None:
    reconciler = FakeReconciler(prepared=[], applications=[], failures=[])
    recovery_calls: list[datetime] = []
    sleeps: list[float] = []
    worker = CollaborationWorker(
        reconciler=reconciler,
        invoker=FakeInvoker([]),
        owner_id="test-collaboration-worker",
        clock=lambda: NOW,
        monotonic_clock=lambda: 0.0,
        recovery_sweep=lambda now: recovery_calls.append(now),
    )

    def stop_after_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    try:
        worker.run_forever(
            batch_size=20,
            poll_seconds=1.5,
            recovery_sweep_seconds=30,
            sleep=stop_after_sleep,
        )
    except KeyboardInterrupt:
        pass

    assert recovery_calls == [NOW]
    assert sleeps == [1.5]


def test_process_entry_returns_nonzero_for_build_failure(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        collaboration_worker,
        "Settings",
        lambda: SimpleNamespace(database_url="postgresql+psycopg://test"),
    )
    monkeypatch.setattr(
        collaboration_worker,
        "build_collaboration_worker",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    assert collaboration_worker.main(["--once"]) == 1
