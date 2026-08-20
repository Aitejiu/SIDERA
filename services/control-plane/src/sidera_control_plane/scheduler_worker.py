import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sidera_control_plane.application.scheduler import Scheduler, SchedulerRunResult
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


@dataclass(slots=True)
class SchedulerRuntime:
    scheduler: Scheduler
    database: Database


def build_scheduler(settings: Settings) -> SchedulerRuntime:
    database = Database(settings.database_url)
    try:
        scheduler = Scheduler(
            uow_factory=lambda: SqlAlchemyUnitOfWork(database),
            policy=LowestCostWorkerPolicy(),
            clock=lambda: datetime.now(UTC),
            assignment_lease_seconds=settings.assignment_lease_seconds,
            worker_heartbeat_timeout_seconds=settings.worker_heartbeat_timeout_seconds,
        )
    except BaseException:
        database.dispose()
        raise
    return SchedulerRuntime(scheduler=scheduler, database=database)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SIDERA Scheduler")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one scheduling batch and exit",
    )
    args = parser.parse_args(argv)
    runtime: SchedulerRuntime | object | None = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        runtime = build_scheduler(settings)
        scheduler = _service(runtime, "scheduler")
        while True:
            result = scheduler.run_once(settings.scheduler_batch_size)
            _print_summary(result)
            if args.once:
                return 0
            time.sleep(settings.scheduler_poll_interval_seconds)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "component": "scheduler",
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}"[:1_000],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        _dispose_runtime(runtime)


def _service(runtime: object, name: str) -> Any:
    service = getattr(runtime, name, None)
    if service is None:
        service = runtime
    return service


def _dispose_runtime(runtime: object | None) -> None:
    if runtime is None:
        return
    database = getattr(runtime, "database", None)
    if database is not None:
        database.dispose()


def _print_summary(result: SchedulerRunResult) -> None:
    summary = {
        "component": "scheduler",
        "status": "ok",
        "examined": result.examined,
        "assigned": result.assigned,
        "deferred": result.deferred,
    }
    print(
        json.dumps(
            summary,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
