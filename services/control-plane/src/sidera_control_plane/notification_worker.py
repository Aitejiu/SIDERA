from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select

from sidera_control_plane.application.notification_policy import (
    POLICY_VERSION,
    NotificationEvaluationMode,
    NotificationPolicyV1,
)
from sidera_control_plane.application.notification_reconciler import (
    NotificationDueScanResult,
    NotificationReconciler,
    NotificationReconciliationResult,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    NotificationReconciliationCursorModel,
    WorkspaceModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


class NotificationReconcilerPort(Protocol):
    def reconcile_workspace(
        self,
        workspace_id: UUID,
        *,
        owner: str,
        limit: int,
        mode: NotificationEvaluationMode | str,
    ) -> NotificationReconciliationResult:
        raise NotImplementedError

    def scan_due(
        self,
        workspace_id: UUID,
        owner_id: str,
        now: datetime,
        limit: int,
    ) -> NotificationDueScanResult:
        raise NotImplementedError


WorkspaceProvider = Callable[[], tuple[UUID, ...]]
Clock = Callable[[], datetime]
Sleep = Callable[[float], None]
ExpiredLeaseMaintenance = Callable[[UUID, datetime, int], int]


@dataclass(frozen=True, slots=True)
class NotificationWorkerRunSummary:
    workspaces_seen: int = 0
    reconciled_workspaces: int = 0
    due_states_scanned: int = 0
    facts_created: int = 0
    threads_changed: int = 0
    intents_created: int = 0
    expired_leases: int = 0
    failed_workspaces: int = 0
    diagnostic_codes: tuple[str, ...] = ()


class NotificationWorker:
    """Run bounded live, due and maintenance phases per active Workspace."""

    def __init__(
        self,
        *,
        reconciler: NotificationReconcilerPort,
        workspace_provider: WorkspaceProvider,
        owner_id: str,
        batch_size: int = 50,
        clock: Clock | None = None,
        baseline_required: bool = True,
        baseline_complete: Callable[[UUID], bool] | None = None,
        enabled: bool = True,
        expire_leases: ExpiredLeaseMaintenance | None = None,
        due_sweep_seconds: float = 15.0,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if due_sweep_seconds <= 0:
            raise ValueError("due_sweep_seconds must be positive")
        self._reconciler = reconciler
        self._workspace_provider = workspace_provider
        self._owner_id = owner_id.strip()
        self._batch_size = batch_size
        self._clock = clock or (lambda: datetime.now(UTC))
        self._baseline_required = baseline_required
        self._baseline_complete = baseline_complete or (lambda _workspace_id: True)
        self._enabled = enabled
        self._expire_leases = expire_leases or (lambda _workspace_id, _now, _limit: 0)
        self._due_sweep_seconds = due_sweep_seconds
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._next_due_sweep = 0.0

    def run_once(self, limit: int | None = None) -> NotificationWorkerRunSummary:
        batch_size = self._batch_size if limit is None else limit
        if batch_size < 1:
            raise ValueError("limit must be positive")
        if not self._enabled:
            return NotificationWorkerRunSummary(
                failed_workspaces=1,
                diagnostic_codes=("notification_disabled",),
            )

        workspaces = tuple(self._workspace_provider())
        summary = NotificationWorkerRunSummary(workspaces_seen=len(workspaces))
        monotonic_now = self._monotonic_clock()
        run_due = monotonic_now >= self._next_due_sweep
        if run_due:
            self._next_due_sweep = monotonic_now + self._due_sweep_seconds

        for workspace_id in workspaces:
            if self._baseline_required:
                try:
                    if not self._baseline_complete(workspace_id):
                        summary = self._failed(summary, "notification_baseline_required")
                        continue
                except Exception:  # noqa: BLE001 - isolate Workspace readiness
                    summary = self._failed(
                        summary, "notification_baseline_check_failed"
                    )
                    continue
            try:
                now = self._clock()
                live = self._reconciler.reconcile_workspace(
                    workspace_id,
                    owner=self._owner_id,
                    limit=batch_size,
                    mode=NotificationEvaluationMode.LIVE,
                )
                summary = self._add_live(summary, live)
                if live.diagnostic_code is not None or not live.acquired:
                    summary = self._with_diagnostic(summary, live.diagnostic_code)
                    continue
                if run_due:
                    due = self._reconciler.scan_due(
                        workspace_id,
                        owner_id=self._owner_id,
                        now=now,
                        limit=batch_size,
                    )
                    summary = self._add_due(summary, due)
                    if due.diagnostic_code is not None:
                        summary = self._with_diagnostic(
                            summary, due.diagnostic_code
                        )
                summary = replace(
                    summary,
                    expired_leases=summary.expired_leases
                    + int(self._expire_leases(workspace_id, now, batch_size)),
                )
            except Exception:  # noqa: BLE001 - isolate Workspace failures
                summary = self._failed(
                    summary, "notification_worker_workspace_failed"
                )
        return summary

    def run_forever(
        self,
        *,
        poll_seconds: float,
        sleep: Sleep = time.sleep,
        on_summary: Callable[[NotificationWorkerRunSummary], None] | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        while True:
            summary = self.run_once()
            if on_summary is not None:
                on_summary(summary)
            sleep(poll_seconds)

    @staticmethod
    def _add_live(
        summary: NotificationWorkerRunSummary,
        result: NotificationReconciliationResult,
    ) -> NotificationWorkerRunSummary:
        return replace(
            summary,
            reconciled_workspaces=summary.reconciled_workspaces
            + int(result.acquired and result.diagnostic_code is None),
            facts_created=summary.facts_created + result.facts_created,
            threads_changed=summary.threads_changed + result.threads_changed,
            intents_created=summary.intents_created + result.intents_created,
        )

    @staticmethod
    def _add_due(
        summary: NotificationWorkerRunSummary,
        result: NotificationDueScanResult,
    ) -> NotificationWorkerRunSummary:
        return replace(
            summary,
            due_states_scanned=summary.due_states_scanned + result.scanned,
            facts_created=summary.facts_created + result.facts_created,
            threads_changed=summary.threads_changed + result.threads_changed,
            intents_created=summary.intents_created + result.intents_created,
        )

    @staticmethod
    def _with_diagnostic(
        summary: NotificationWorkerRunSummary, diagnostic_code: str | None
    ) -> NotificationWorkerRunSummary:
        if diagnostic_code is None:
            return summary
        return replace(
            summary,
            diagnostic_codes=summary.diagnostic_codes + (diagnostic_code,),
        )

    @classmethod
    def _failed(
        cls,
        summary: NotificationWorkerRunSummary,
        diagnostic_code: str,
    ) -> NotificationWorkerRunSummary:
        return replace(
            cls._with_diagnostic(summary, diagnostic_code),
            failed_workspaces=summary.failed_workspaces + 1,
        )


@dataclass(slots=True)
class NotificationWorkerRuntime:
    worker: NotificationWorker
    reconciler: NotificationReconciler
    database: Database


def build_notification_worker(settings: Settings) -> NotificationWorkerRuntime:
    database = Database(settings.database_url)
    uow_factory: Callable[[], SqlAlchemyUnitOfWork] = lambda: SqlAlchemyUnitOfWork(
        database,
        projection_change_retention_seconds=settings.projection_change_retention_seconds,
    )
    owner_id = f"notification-worker:{settings.local_actor_id}"
    reconciler = NotificationReconciler(
        uow_factory,
        policy=NotificationPolicyV1(
            blocked_grace_seconds=settings.notification_blocked_grace_seconds
        ),
        owner=owner_id,
        batch_size=settings.notification_reconcile_batch_size,
        lease_seconds=settings.notification_cursor_lease_seconds,
    )

    def workspace_provider() -> tuple[UUID, ...]:
        with database.session() as session:
            return tuple(
                session.scalars(
                    select(WorkspaceModel.id)
                    .where(WorkspaceModel.status == "active")
                    .order_by(WorkspaceModel.id.asc())
                ).all()
            )

    def baseline_complete(workspace_id: UUID) -> bool:
        with database.session() as session:
            return (
                session.scalar(
                    select(NotificationReconciliationCursorModel.workspace_id).where(
                        NotificationReconciliationCursorModel.workspace_id
                        == workspace_id,
                        NotificationReconciliationCursorModel.policy_version
                        == POLICY_VERSION,
                    )
                )
                is not None
            )

    try:
        worker = NotificationWorker(
            reconciler=reconciler,
            workspace_provider=workspace_provider,
            owner_id=owner_id,
            batch_size=settings.notification_reconcile_batch_size,
            baseline_required=settings.notification_baseline_required,
            baseline_complete=baseline_complete,
            enabled=settings.notification_enabled,
            due_sweep_seconds=settings.notification_due_sweep_seconds,
        )
    except BaseException:
        database.dispose()
        raise
    return NotificationWorkerRuntime(
        worker=worker,
        reconciler=reconciler,
        database=database,
    )


def summary_json(summary: NotificationWorkerRunSummary) -> str:
    return json.dumps(
        {
            "component": "notification-worker",
            "status": "ok",
            "workspaces_seen": summary.workspaces_seen,
            "reconciled_workspaces": summary.reconciled_workspaces,
            "due_states_scanned": summary.due_states_scanned,
            "facts_created": summary.facts_created,
            "threads_changed": summary.threads_changed,
            "intents_created": summary.intents_created,
            "expired_leases": summary.expired_leases,
            "failed_workspaces": summary.failed_workspaces,
            "diagnostic_codes": list(summary.diagnostic_codes),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SIDERA notification worker")
    parser.add_argument("--once", action="store_true", help="run one bounded pass")
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="run the explicit current-state baseline rebuild",
    )
    parser.add_argument("--workspace-id")
    parser.add_argument("--owner-id")
    args = parser.parse_args(argv)
    runtime: NotificationWorkerRuntime | None = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        runtime = build_notification_worker(settings)
        if args.baseline:
            if args.workspace_id is None:
                parser.error("--baseline requires --workspace-id")
            result = runtime.reconciler.baseline_workspace(
                UUID(args.workspace_id), owner=args.owner_id
            )
            print(_reconciliation_json(result, baseline=True))
            return 0 if result.diagnostic_code is None else 2
        if args.workspace_id is not None:
            parser.error("--workspace-id is only valid with --baseline")
        if args.once:
            print(summary_json(runtime.worker.run_once()))
            return 0
        runtime.worker.run_forever(
            poll_seconds=settings.notification_poll_seconds,
            on_summary=lambda summary: print(summary_json(summary)),
        )
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001 - stable CLI failure output
        print(
            json.dumps(
                {
                    "component": "notification-worker",
                    "status": "error",
                    "error_code": "notification_worker_start_failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if runtime is not None:
            runtime.database.dispose()


def _reconciliation_json(
    result: NotificationReconciliationResult, *, baseline: bool
) -> str:
    return json.dumps(
        {
            "component": "notification-worker",
            "status": "ok" if result.diagnostic_code is None else "error",
            "mode": "baseline" if baseline else "live",
            "processed_changes": result.processed_changes,
            "relevant_changes": result.relevant_changes,
            "observations": result.observations,
            "facts_created": result.facts_created,
            "threads_changed": result.threads_changed,
            "intents_created": result.intents_created,
            "cursor": result.cursor,
            "diagnostic_code": result.diagnostic_code,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
