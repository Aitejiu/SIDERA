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

    @property
    def state(self) -> str:
        if self.unknown or self.blocked or self.failed:
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
        if self.failed:
            return "result_apply_failed"
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
        pending = sum(item.state == "pending" for item in outcomes)
        blocked = sum(
            item.state == "blocked"
            and item.failure_code != "result_operation_inspect_required"
            for item in outcomes
        )
        unknown = sum(
            item.state == "blocked"
            and item.failure_code == "result_operation_inspect_required"
            for item in outcomes
        )
        for operation in claimed_attempts:
            handler = self._operation_handlers[operation.operation_kind]
            handler(operation)
        processed += len(claimed_attempts)
        return ResultWorkerRunResult(
            claimed=len(claimed_apply) + len(unknown_apply) + len(claimed_attempts),
            processed=processed,
            completed=completed,
            pending=pending,
            blocked=blocked,
            unknown=unknown,
            failed=0,
        )


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
