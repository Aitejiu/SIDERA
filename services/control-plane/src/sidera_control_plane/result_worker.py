from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sidera_control_plane.adapters.git_results import GitResultStore, RepositoryResolver
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.result_apply import (
    ResultApplyOutcome,
    ResultApplyService,
)
from sidera_control_plane.application.result_ports import ResultOperationAttemptV1
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.result_operations import ResultOperationStatus
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

ResultOperationHandler = Callable[[ResultOperationAttemptV1], object]


@dataclass(frozen=True, slots=True)
class ResultWorkerRunResult:
    claimed: int
    processed: int
    completed: int
    pending: int
    blocked: int
    unknown: int
    failed: int
    oldest_waiting_at: datetime | None = None
    budget_exhausted: bool = False
    storage_missing: bool = False

    @property
    def state(self) -> str:
        # ``failed`` is an observation count over durable history. It must not
        # block a run that has reconciled its current operation successfully.
        if self.unknown or self.blocked:
            return "blocked"
        if self.pending or self.processed == 0:
            return "pending"
        return "completed"

    @property
    def failure_code(self) -> str | None:
        if self.unknown:
            return "result_operation_inspect_required"
        if self.blocked:
            return "result_apply_blocked"
        return None


class ResultWorker:
    """Lease and dispatch durable result operations without choosing workers."""

    _DEFAULT_OPERATION_KINDS = (
        "code_result_capture",
        "verification",
        "rework",
        "finalization",
    )

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        apply_service: ResultApplyService,
        *,
        owner_id: str = "result-worker",
        clock: Callable[[], datetime] | None = None,
        operation_handlers: Mapping[str, ResultOperationHandler] | None = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("result worker owner_id is required")
        self._uow_factory = uow_factory
        self._apply_service = apply_service
        self._owner_id = owner_id.strip()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._operation_handlers = dict(operation_handlers or {})

    def run_once(self, limit: int = 10) -> ResultWorkerRunResult:
        if type(limit) is not int or limit < 1:
            raise ValueError("result worker limit must be positive")
        now = _aware_now(self._clock())
        with self._uow_factory() as uow:
            claimed_apply = uow.results.claim_apply_operations(
                now,
                limit,
                owner_id=self._owner_id,
            )
            remaining = limit - len(claimed_apply)
            unknown_apply = (
                uow.results.list_unknown_apply_operations(remaining)
                if remaining > 0
                else ()
            )
            remaining -= len(unknown_apply)
            claimed_attempts: list[ResultOperationAttemptV1] = []
            if remaining > 0:
                for kind in self._DEFAULT_OPERATION_KINDS:
                    handler = self._operation_handlers.get(kind)
                    if handler is None:
                        continue
                    batch = uow.results.claim_operations(
                        kind,
                        now,
                        remaining,
                        owner_id=self._owner_id,
                    )
                    claimed_attempts.extend(batch)
                    remaining -= len(batch)
                    if remaining == 0:
                        break

        outcomes: list[ResultApplyOutcome] = []
        for operation in (*claimed_apply, *unknown_apply):
            outcomes.append(
                self._apply_service.reconcile(
                    operation.workspace_id,
                    operation.root_task_id,
                    operation.id,
                )
            )

        processed = len(outcomes)
        completed = sum(item.state == "completed" for item in outcomes)
        for operation in claimed_attempts:
            handler = self._operation_handlers[operation.operation_kind]
            handler(operation)
        processed += len(claimed_attempts)
        observation = self._observe()
        return ResultWorkerRunResult(
            claimed=len(claimed_apply) + len(unknown_apply) + len(claimed_attempts),
            processed=processed,
            completed=completed,
            pending=observation.pending,
            blocked=observation.blocked,
            unknown=observation.unknown,
            failed=observation.failed,
            oldest_waiting_at=observation.oldest_waiting_at,
            budget_exhausted=observation.budget_exhausted,
            storage_missing=observation.storage_missing,
        )

    def _observe(self) -> _ResultWorkerObservation:
        with self._uow_factory() as uow:
            items = uow.results.list_operations_for_observation()
        waiting = tuple(
            item
            for item in items
            if item.status in {ResultOperationStatus.PENDING, ResultOperationStatus.RUNNING}
        )
        failure_codes = {
            item.failure_code for item in items if item.failure_code is not None
        }
        return _ResultWorkerObservation(
            pending=len(waiting),
            blocked=sum(item.status is ResultOperationStatus.BLOCKED for item in items),
            unknown=sum(item.status is ResultOperationStatus.UNKNOWN for item in items),
            failed=sum(item.status is ResultOperationStatus.FAILED for item in items),
            oldest_waiting_at=(
                min(item.created_at for item in waiting) if waiting else None
            ),
            budget_exhausted=bool(
                failure_codes
                & {
                    "rework_budget_exhausted",
                    "rework_deadline_exceeded",
                    "real_run_budget_exhausted",
                }
            ),
            storage_missing=bool(
                failure_codes
                & {
                    "result_capture_receipt_corrupt",
                    "result_capture_not_found",
                    "result_apply_receipt_missing",
                    "result_apply_receipt_corrupt",
                    "result_apply_repository_unavailable",
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class _ResultWorkerObservation:
    pending: int
    blocked: int
    unknown: int
    failed: int
    oldest_waiting_at: datetime | None
    budget_exhausted: bool
    storage_missing: bool


@dataclass(slots=True)
class ResultWorkerRuntime:
    worker: ResultWorker
    database: Database


def build_result_worker(
    settings: Settings,
    *,
    repository_resolver: RepositoryResolver | None = None,
) -> ResultWorkerRuntime:
    database = Database(settings.database_url)
    uow_factory = lambda: SqlAlchemyUnitOfWork(
        database,
        projection_change_retention_seconds=settings.projection_change_retention_seconds,
    )
    owner_id = f"result-worker:{settings.local_actor_id}"
    code_store = GitResultStore(
        repository_resolver or {},
        git_executable=settings.git_executable,
    )
    apply_service = ResultApplyService(
        uow_factory,
        code_store,
        clock=lambda: datetime.now(UTC),
        owner_id=owner_id,
    )
    return ResultWorkerRuntime(
        worker=ResultWorker(
            uow_factory,
            apply_service,
            owner_id=owner_id,
            clock=lambda: datetime.now(UTC),
        ),
        database=database,
    )


def summary_json(result: ResultWorkerRunResult) -> str:
    return json.dumps(
        {
            "component": "result-worker",
            "status": "ok",
            "claimed": result.claimed,
            "processed": result.processed,
            "completed": result.completed,
            "pending": result.pending,
            "blocked": result.blocked,
            "unknown": result.unknown,
            "failed": result.failed,
            "oldest_waiting_at": (
                None
                if result.oldest_waiting_at is None
                else result.oldest_waiting_at.astimezone(UTC).isoformat()
            ),
            "budget_exhausted": result.budget_exhausted,
            "storage_missing": result.storage_missing,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SIDERA Result Worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one result batch and exit",
    )
    args = parser.parse_args(argv)
    runtime: ResultWorkerRuntime | None = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        runtime = build_result_worker(settings)
        while True:
            result = runtime.worker.run_once()
            print(summary_json(result))
            if args.once:
                return 0
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "component": "result-worker",
                    "status": "error",
                    "error": "result worker failed",
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


def _aware_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("result worker clock must return an aware datetime")
    return value.astimezone(UTC)


__all__ = [
    "ResultWorker",
    "ResultWorkerRunResult",
    "ResultWorkerRuntime",
    "build_result_worker",
    "main",
    "summary_json",
]


if __name__ == "__main__":
    raise SystemExit(main())
