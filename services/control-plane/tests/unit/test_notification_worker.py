from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from sidera_control_plane.application.notification_policy import (
    NotificationEvaluationMode,
)
from sidera_control_plane.application.notification_reconciler import (
    NotificationDueScanResult,
    NotificationReconciliationResult,
)
from sidera_control_plane.notification_worker import (
    NotificationWorker,
    NotificationWorkerRunSummary,
    summary_json,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
WORKSPACE_A = UUID("00000000-0000-0000-0000-000000000001")
WORKSPACE_B = UUID("00000000-0000-0000-0000-000000000002")


class FakeReconciler:
    def __init__(self, events: list[str], *, failing_workspace: UUID | None = None):
        self.events = events
        self.failing_workspace = failing_workspace

    def reconcile_workspace(self, workspace_id, *, owner, limit, mode):
        self.events.append(f"reconcile:{workspace_id}")
        if workspace_id == self.failing_workspace:
            raise RuntimeError("database unavailable")
        return NotificationReconciliationResult(
            workspace_id=workspace_id,
            acquired=True,
            processed_changes=2,
            relevant_changes=1,
            observations=1,
            facts_created=1,
            threads_changed=1,
            intents_created=0,
            cursor=2,
        )

    def scan_due(self, workspace_id, *, owner_id, now, limit):
        self.events.append(f"due:{workspace_id}")
        return NotificationDueScanResult(
            workspace_id=workspace_id,
            scanned=2,
            facts_created=1,
            threads_changed=1,
            intents_created=0,
        )


class FakeBaselineReconciler(FakeReconciler):
    def __init__(
        self,
        events: list[str],
        complete: set[UUID],
        *,
        results: dict[UUID, NotificationReconciliationResult | Exception] | None = None,
        durable: bool = True,
    ):
        super().__init__(events)
        self.complete = complete
        self.results = results or {}
        self.durable = durable
        self.baseline_calls: list[tuple[UUID, str | None]] = []
        self.live_calls = []

    def baseline_workspace(self, workspace_id: UUID, *, owner: str | None = None):
        self.events.append(f"baseline:{workspace_id}")
        self.baseline_calls.append((workspace_id, owner))
        result = self.results.get(
            workspace_id,
            NotificationReconciliationResult(
                workspace_id=workspace_id,
                acquired=True,
                facts_created=2,
                threads_changed=3,
                intents_created=4,
                cursor=2,
            ),
        )
        if isinstance(result, Exception):
            raise result
        if self.durable and result.acquired and result.diagnostic_code is None:
            self.complete.add(workspace_id)
        return result

    def reconcile_workspace(self, workspace_id, *, owner, limit, mode):
        self.live_calls.append((workspace_id, owner, limit, mode))
        return super().reconcile_workspace(
            workspace_id, owner=owner, limit=limit, mode=mode
        )


def test_run_once_orders_live_due_and_expired_lease_maintenance() -> None:
    events: list[str] = []
    worker = NotificationWorker(
        reconciler=FakeReconciler(events),
        workspace_provider=lambda: (WORKSPACE_A,),
        owner_id="notification-worker-test",
        batch_size=10,
        clock=lambda: NOW,
        baseline_required=False,
        expire_leases=lambda workspace_id, now, limit: events.append(
            f"expired:{workspace_id}"
        )
        or 3,
    )

    summary = worker.run_once()

    assert isinstance(summary, NotificationWorkerRunSummary)
    assert events == [
        f"reconcile:{WORKSPACE_A}",
        f"due:{WORKSPACE_A}",
        f"expired:{WORKSPACE_A}",
    ]
    assert summary.reconciled_workspaces == 1
    assert summary.due_states_scanned == 2
    assert summary.expired_leases == 3


def test_run_once_records_one_workspace_failure_and_continues() -> None:
    events: list[str] = []
    worker = NotificationWorker(
        reconciler=FakeReconciler(events, failing_workspace=WORKSPACE_A),
        workspace_provider=lambda: (WORKSPACE_A, WORKSPACE_B),
        owner_id="notification-worker-test",
        batch_size=10,
        clock=lambda: NOW,
        baseline_required=False,
        expire_leases=lambda workspace_id, now, limit: events.append(
            f"expired:{workspace_id}"
        )
        or 0,
    )

    summary = worker.run_once()

    assert events == [
        f"reconcile:{WORKSPACE_A}",
        f"reconcile:{WORKSPACE_B}",
        f"due:{WORKSPACE_B}",
        f"expired:{WORKSPACE_B}",
    ]
    assert summary.failed_workspaces == 1
    assert summary.diagnostic_codes == ("notification_worker_workspace_failed",)
    assert summary.reconciled_workspaces == 1


def test_baseline_gate_does_not_implicitly_start_normal_polling() -> None:
    events: list[str] = []
    worker = NotificationWorker(
        reconciler=FakeReconciler(events),
        workspace_provider=lambda: (WORKSPACE_A,),
        owner_id="notification-worker-test",
        batch_size=10,
        clock=lambda: NOW,
        baseline_required=True,
        baseline_complete=lambda _workspace_id: False,
    )

    summary = worker.run_once()

    assert events == []
    assert summary.failed_workspaces == 1
    assert summary.diagnostic_codes == ("notification_baseline_required",)


def test_auto_baseline_rechecks_before_live_and_only_initializes_once() -> None:
    events: list[str] = []
    complete: set[UUID] = set()
    reconciler = FakeBaselineReconciler(events, complete)
    ticks = iter((0.0, 15.0))

    def baseline_complete(workspace_id: UUID) -> bool:
        events.append(f"check:{workspace_id}")
        return workspace_id in complete

    worker = NotificationWorker(
        reconciler=reconciler,
        workspace_provider=lambda: (WORKSPACE_A,),
        owner_id="notification-worker-test",
        batch_size=10,
        clock=lambda: NOW,
        baseline_required=True,
        auto_baseline=True,
        baseline_complete=baseline_complete,
        monotonic_clock=lambda: next(ticks),
        expire_leases=lambda workspace_id, now, limit: (
            events.append(f"expired:{workspace_id}") or 3
        ),
    )

    first = worker.run_once()
    second = worker.run_once()

    assert events == [
        f"check:{WORKSPACE_A}",
        f"baseline:{WORKSPACE_A}",
        f"check:{WORKSPACE_A}",
        f"reconcile:{WORKSPACE_A}",
        f"due:{WORKSPACE_A}",
        f"expired:{WORKSPACE_A}",
        f"check:{WORKSPACE_A}",
        f"reconcile:{WORKSPACE_A}",
        f"due:{WORKSPACE_A}",
        f"expired:{WORKSPACE_A}",
    ]
    assert reconciler.baseline_calls == [(WORKSPACE_A, "notification-worker-test")]
    assert reconciler.live_calls == [
        (WORKSPACE_A, "notification-worker-test", 10, NotificationEvaluationMode.LIVE),
        (WORKSPACE_A, "notification-worker-test", 10, NotificationEvaluationMode.LIVE),
    ]
    assert first == NotificationWorkerRunSummary(
        workspaces_seen=1,
        reconciled_workspaces=1,
        due_states_scanned=2,
        facts_created=4,
        threads_changed=5,
        intents_created=4,
        expired_leases=3,
    )
    assert second == NotificationWorkerRunSummary(
        workspaces_seen=1,
        reconciled_workspaces=1,
        due_states_scanned=2,
        facts_created=2,
        threads_changed=2,
        expired_leases=3,
    )


@pytest.mark.parametrize(
    "result,durable,checks,diagnostic",
    [
        pytest.param(
            NotificationReconciliationResult(
                workspace_id=WORKSPACE_A,
                acquired=False,
                diagnostic_code="notification_cursor_busy",
                facts_created=7,
                threads_changed=8,
                intents_created=9,
            ),
            True,
            1,
            "notification_cursor_busy",
            id="cursor-busy",
        ),
        pytest.param(
            NotificationReconciliationResult(
                workspace_id=WORKSPACE_A,
                acquired=False,
            ),
            True,
            1,
            "notification_baseline_required",
            id="not-acquired-without-diagnostic",
        ),
        pytest.param(
            NotificationReconciliationResult(
                workspace_id=WORKSPACE_A,
                acquired=True,
                diagnostic_code="notification_baseline_failed",
                facts_created=7,
                threads_changed=8,
                intents_created=9,
            ),
            True,
            1,
            "notification_baseline_failed",
            id="diagnostic-failure",
        ),
        pytest.param(
            RuntimeError("PRIVATE_BASELINE_SENTINEL"),
            True,
            1,
            "notification_baseline_check_failed",
            id="exception",
        ),
        pytest.param(
            NotificationReconciliationResult(
                workspace_id=WORKSPACE_A,
                acquired=True,
                facts_created=7,
                threads_changed=8,
                intents_created=9,
            ),
            False,
            2,
            "notification_baseline_required",
            id="uncommitted-baseline",
        ),
    ],
)
def test_auto_baseline_failure_blocks_workspace_and_continues_another(
    result, durable, checks, diagnostic
) -> None:
    events: list[str] = []
    complete = {WORKSPACE_B}
    reconciler = FakeBaselineReconciler(
        events, complete, results={WORKSPACE_A: result}, durable=durable
    )

    def baseline_complete(workspace_id: UUID) -> bool:
        events.append(f"check:{workspace_id}")
        return workspace_id in complete

    worker = NotificationWorker(
        reconciler=reconciler,
        workspace_provider=lambda: (WORKSPACE_A, WORKSPACE_B),
        owner_id="notification-worker-test",
        batch_size=10,
        clock=lambda: NOW,
        baseline_required=True,
        auto_baseline=True,
        baseline_complete=baseline_complete,
        expire_leases=lambda workspace_id, now, limit: (
            events.append(f"expired:{workspace_id}") or 3
        ),
    )

    summary = worker.run_once()

    assert events == [
        f"check:{WORKSPACE_A}",
        f"baseline:{WORKSPACE_A}",
        *([f"check:{WORKSPACE_A}"] if checks == 2 else []),
        f"check:{WORKSPACE_B}",
        f"reconcile:{WORKSPACE_B}",
        f"due:{WORKSPACE_B}",
        f"expired:{WORKSPACE_B}",
    ]
    assert reconciler.baseline_calls == [(WORKSPACE_A, "notification-worker-test")]
    assert summary == NotificationWorkerRunSummary(
        workspaces_seen=2,
        reconciled_workspaces=1,
        due_states_scanned=2,
        facts_created=2,
        threads_changed=2,
        expired_leases=3,
        failed_workspaces=1,
        diagnostic_codes=(diagnostic,),
    )
    assert "PRIVATE_BASELINE_SENTINEL" not in summary_json(summary)


@pytest.mark.parametrize("failed_check", [1, 2], ids=["initial-check", "recheck"])
def test_auto_baseline_check_exception_blocks_workspace_and_continues_another(
    failed_check,
) -> None:
    events: list[str] = []
    complete = {WORKSPACE_B}
    reconciler = FakeBaselineReconciler(events, complete)
    checks = 0

    def baseline_complete(workspace_id: UUID) -> bool:
        nonlocal checks
        events.append(f"check:{workspace_id}")
        if workspace_id == WORKSPACE_A:
            checks += 1
            if checks == failed_check:
                raise RuntimeError("PRIVATE_BASELINE_SENTINEL")
        return workspace_id in complete

    worker = NotificationWorker(
        reconciler=reconciler,
        workspace_provider=lambda: (WORKSPACE_A, WORKSPACE_B),
        owner_id="notification-worker-test",
        batch_size=10,
        clock=lambda: NOW,
        auto_baseline=True,
        baseline_complete=baseline_complete,
        expire_leases=lambda workspace_id, now, limit: (
            events.append(f"expired:{workspace_id}") or 3
        ),
    )

    summary = worker.run_once()

    assert events == [
        f"check:{WORKSPACE_A}",
        *(
            [f"baseline:{WORKSPACE_A}", f"check:{WORKSPACE_A}"]
            if failed_check == 2
            else []
        ),
        f"check:{WORKSPACE_B}",
        f"reconcile:{WORKSPACE_B}",
        f"due:{WORKSPACE_B}",
        f"expired:{WORKSPACE_B}",
    ]
    assert summary == NotificationWorkerRunSummary(
        workspaces_seen=2,
        reconciled_workspaces=1,
        due_states_scanned=2,
        facts_created=2,
        threads_changed=2,
        expired_leases=3,
        failed_workspaces=1,
        diagnostic_codes=("notification_baseline_check_failed",),
    )


def test_auto_baseline_does_not_initialize_when_baseline_is_not_required() -> None:
    events: list[str] = []
    reconciler = FakeBaselineReconciler(events, set())
    worker = NotificationWorker(
        reconciler=reconciler,
        workspace_provider=lambda: (WORKSPACE_A,),
        owner_id="notification-worker-test",
        baseline_required=False,
        auto_baseline=True,
        baseline_complete=lambda _workspace_id: pytest.fail(
            "Unexpected baseline check"
        ),
        expire_leases=lambda workspace_id, now, limit: (
            events.append(f"expired:{workspace_id}") or 0
        ),
    )

    summary = worker.run_once()

    assert events == [
        f"reconcile:{WORKSPACE_A}",
        f"due:{WORKSPACE_A}",
        f"expired:{WORKSPACE_A}",
    ]
    assert reconciler.baseline_calls == []
    assert summary.failed_workspaces == 0
    assert summary.reconciled_workspaces == 1


@pytest.mark.parametrize(
    "options,expected",
    [({}, False), ({"auto_baseline": False}, False), ({"auto_baseline": True}, True)],
    ids=["default-opt-in", "explicit-false", "explicit-true"],
)
def test_notification_factory_passes_opt_in_auto_baseline_to_worker(
    monkeypatch, options, expected
) -> None:
    from sidera_control_plane import notification_worker as module
    from sidera_control_plane.config import Settings

    captured = {}
    built_worker = object()
    database = SimpleNamespace(dispose=lambda: None)

    def build_worker(**kwargs):
        captured.update(kwargs)
        return built_worker

    monkeypatch.setattr(module, "Database", lambda _url: database)
    monkeypatch.setattr(module, "NotificationWorker", build_worker)
    runtime = module.build_notification_worker(
        Settings(
            database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused",
            notification_enabled=True,
        ),
        **options,
    )

    assert runtime.worker is built_worker
    assert runtime.database is database
    assert captured.get("auto_baseline") is expected
    assert captured["baseline_required"] is True


def test_run_forever_reports_before_sleep_and_exits_on_interrupt() -> None:
    events: list[str] = []
    summaries: list[NotificationWorkerRunSummary] = []
    worker = NotificationWorker(
        reconciler=FakeReconciler(events),
        workspace_provider=lambda: (WORKSPACE_A,),
        owner_id="notification-worker-test",
        batch_size=10,
        clock=lambda: NOW,
        baseline_required=False,
    )

    def sleep(seconds: float) -> None:
        events.append(f"sleep:{seconds}")
        raise KeyboardInterrupt

    try:
        worker.run_forever(
            poll_seconds=1.5,
            sleep=sleep,
            on_summary=summaries.append,
        )
    except KeyboardInterrupt:
        pass

    assert events[-1] == "sleep:1.5"
    assert len(summaries) == 1
    assert events.index("reconcile:" + str(WORKSPACE_A)) < events.index("sleep:1.5")


def test_summary_json_contains_counts_and_stable_codes_only() -> None:
    summary = NotificationWorkerRunSummary(
        workspaces_seen=1,
        reconciled_workspaces=1,
        facts_created=2,
        diagnostic_codes=("notification_cursor_busy",),
    )

    payload = json.loads(summary_json(summary))

    assert payload["component"] == "notification-worker"
    assert payload["facts_created"] == 2
    assert payload["diagnostic_codes"] == ["notification_cursor_busy"]
    assert "chat" not in summary_json(summary)
    assert "route" not in summary_json(summary)
