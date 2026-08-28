from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sidera_control_plane.application.plan_transition_coordinator import (
    PlanTransitionCoordinator,
    PlanTransitionRunResult,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


class PlanTransitionCoordinatorPort(Protocol):
    def run_once(self, limit: int) -> PlanTransitionRunResult:
        raise NotImplementedError


@dataclass(slots=True)
class PlanTransitionWorkerRuntime:
    coordinator: PlanTransitionCoordinatorPort
    database: Database


def build_plan_transition_worker(
    settings: Settings,
) -> PlanTransitionWorkerRuntime:
    database = Database(settings.database_url)
    try:
        coordinator = PlanTransitionCoordinator(
            uow_factory=lambda: SqlAlchemyUnitOfWork(
                database,
                projection_change_retention_seconds=(
                    settings.projection_change_retention_seconds
                ),
            ),
            owner_id=f"plan-transition-coordinator:{settings.local_actor_id}",
            lease_ttl_seconds=settings.plan_transition_lease_ttl_seconds,
            backoff_max_seconds=settings.plan_transition_backoff_max_seconds,
            clock=lambda: datetime.now(UTC),
        )
    except BaseException:
        database.dispose()
        raise
    return PlanTransitionWorkerRuntime(coordinator=coordinator, database=database)


def summary_json(result: PlanTransitionRunResult) -> str:
    return json.dumps(
        {
            "component": "plan-transition-worker",
            "status": "ok",
            "claimed": result.claimed,
            "converged": result.converged,
            "deferred": result.deferred,
            "failed": result.failed,
            "reconciled": result.reconciled,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the SIDERA Plan Transition Coordinator"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one plan transition batch and exit",
    )
    args = parser.parse_args(argv)
    runtime: PlanTransitionWorkerRuntime | None = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        runtime = build_plan_transition_worker(settings)
        while True:
            result = runtime.coordinator.run_once(
                settings.plan_transition_batch_size
            )
            print(summary_json(result))
            if args.once:
                return 0
            time.sleep(settings.plan_transition_poll_interval_seconds)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "component": "plan-transition-worker",
                    "status": "error",
                    "error": f"{type(exc).__name__}: plan transition worker failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        _dispose_runtime(runtime)


def _dispose_runtime(runtime: PlanTransitionWorkerRuntime | None) -> None:
    if runtime is not None:
        runtime.database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
