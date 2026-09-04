from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

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
