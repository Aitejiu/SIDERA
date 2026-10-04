from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sidera_control_plane.application.activation_reconciler import (
    ActivationReconciler,
    ActivationReconcileResult,
)
from sidera_control_plane.application.agent_worker_adapter import require_gateway_owner
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


class ActivationReconcilerPort(Protocol):
    def run_once(self, limit: int) -> ActivationReconcileResult:
        raise NotImplementedError


class ActivationWorker:
    def __init__(self, reconciler: ActivationReconcilerPort) -> None:
        self._reconciler = reconciler

    def run_once(self, limit: int) -> ActivationReconcileResult:
        return self._reconciler.run_once(limit)


@dataclass(slots=True)
class ActivationWorkerRuntime:
    worker: ActivationWorker
    database: Database


def build_activation_worker(
    settings: Settings,
    *,
    owner_check: Callable[[], bool] | None = None,
) -> ActivationWorkerRuntime:
    database = Database(settings.database_url)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        require_gateway_owner(owner_check)
        return SqlAlchemyUnitOfWork(
            database,
            projection_change_retention_seconds=settings.projection_change_retention_seconds,
            before_commit=None
            if owner_check is None
            else lambda: require_gateway_owner(owner_check),
        )

    try:
        reconciler = ActivationReconciler(
            uow_factory=uow_factory,
            owner_id=f"activation-reconciler:{settings.local_actor_id}",
            clock=lambda: datetime.now(UTC),
            execution_context_ttl_seconds=settings.execution_context_ttl_seconds,
        )
        return ActivationWorkerRuntime(
            worker=ActivationWorker(reconciler), database=database
        )
    except BaseException:
        database.dispose()
        raise


def summary_json(result: ActivationReconcileResult) -> str:
    return json.dumps(
        {
            "component": "activation-worker",
            "status": "ok",
            "examined": result.examined,
            "readied": result.readied,
            "materialized": result.materialized,
            "deferred": result.deferred,
            "failed": result.failed,
            "execution_ids": [str(item) for item in result.execution_ids],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SIDERA Activation Worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one activation batch and exit",
    )
    args = parser.parse_args(argv)
    runtime: ActivationWorkerRuntime | None = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        runtime = build_activation_worker(settings)
        while True:
            result = runtime.worker.run_once(settings.activation_batch_size)
            print(summary_json(result))
            if args.once:
                return 0
            time.sleep(settings.activation_poll_interval_seconds)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "component": "activation-worker",
                    "status": "error",
                    "error": f"{type(exc).__name__}: activation worker failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        _dispose_runtime(runtime)


def _dispose_runtime(runtime: Any) -> None:
    if runtime is None:
        return
    database = getattr(runtime, "database", None)
    if database is not None:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
