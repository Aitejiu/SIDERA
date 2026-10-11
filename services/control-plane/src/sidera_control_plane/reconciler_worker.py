import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sidera_control_plane.application.agent_worker_adapter import require_gateway_owner
from sidera_control_plane.application.reconciler import Reconciler, ReconcilerRunResult
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


@dataclass(slots=True)
class ReconcilerRuntime:
    reconciler: Reconciler
    database: Database


def build_reconciler(
    settings: Settings,
    *,
    owner_check: Callable[[], bool] | None = None,
) -> ReconcilerRuntime:
    database = Database(settings.database_url)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        require_gateway_owner(owner_check)
        return SqlAlchemyUnitOfWork(
            database,
            before_commit=None
            if owner_check is None
            else lambda: require_gateway_owner(owner_check),
        )

    try:
        reconciler = Reconciler(
            uow_factory=uow_factory,
            clock=lambda: datetime.now(UTC),
            worker_heartbeat_timeout_seconds=settings.worker_heartbeat_timeout_seconds,
        )
    except BaseException:
        database.dispose()
        raise
    return ReconcilerRuntime(reconciler=reconciler, database=database)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SIDERA Reconciler")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one reconciliation batch and exit",
    )
    args = parser.parse_args(argv)
    runtime: ReconcilerRuntime | object | None = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        runtime = build_reconciler(settings)
        reconciler = _service(runtime, "reconciler")
        while True:
            result = reconciler.run_once(settings.reconciler_batch_size)
            _print_summary(result)
            if args.once:
                return 0
            time.sleep(settings.reconciler_poll_interval_seconds)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "component": "reconciler",
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


def _print_summary(result: ReconcilerRunResult) -> None:
    summary = {
        "component": "reconciler",
        "status": "ok",
        "offline_workers": result.offline_workers,
        "expired_leases": result.expired_leases,
        "retries_queued": result.retries_queued,
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
