from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sidera_control_plane.adapters.git_workspace import GitWorktreeWorkspaceManager
from sidera_control_plane.application.handoff_context import HandoffContextService
from sidera_control_plane.application.handoff_coordinator import (
    HandoffCoordinator,
    HandoffRunResult,
)
from sidera_control_plane.application.handoff_input_transfer import (
    HandoffInputTransferService,
)
from sidera_control_plane.application.handoff_target import HandoffTargetService
from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.application.workspace_handoff import WorkspaceHandoffService
from sidera_control_plane.config import Settings
from sidera_control_plane.gateway_worker import build_gateway
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


class HandoffCoordinatorPort(Protocol):
    def run_once(self, limit: int) -> HandoffRunResult:
        raise NotImplementedError


@dataclass(slots=True)
class HandoffWorkerRuntime:
    coordinator: HandoffCoordinatorPort
    database: Database
    catalog: object


def build_handoff_worker(settings: Settings) -> HandoffWorkerRuntime:
    gateway_runtime = build_gateway(settings)
    database = gateway_runtime.database
    uow_factory = lambda: SqlAlchemyUnitOfWork(
        database,
        projection_change_retention_seconds=(
            settings.projection_change_retention_seconds
        ),
    )
    try:
        workspace_provider = GitWorktreeWorkspaceManager(
            settings.workspace_root,
            retention_seconds=settings.workspace_retention_seconds,
            git_executable=settings.git_executable,
        )
        scheduler = Scheduler(
            uow_factory=uow_factory,
            policy=LowestCostWorkerPolicy(),
            clock=lambda: datetime.now(UTC),
            assignment_lease_seconds=settings.assignment_lease_seconds,
            worker_heartbeat_timeout_seconds=settings.worker_heartbeat_timeout_seconds,
            context_enforcement=True,
            context_package_cutover_at=settings.context_package_cutover_at,
        )
        coordinator = HandoffCoordinator(
            uow_factory=uow_factory,
            gateway=gateway_runtime.worker_gateway,
            workspace_handoff=WorkspaceHandoffService(
                workspace_provider,
                uow_factory=uow_factory,
            ),
            target_service=HandoffTargetService(uow_factory),
            context_service=HandoffContextService(
                uow_factory,
                context_ttl_seconds=settings.execution_context_ttl_seconds,
            ),
            input_transfer=HandoffInputTransferService(uow_factory),
            scheduler=scheduler,
            owner_id=f"handoff-coordinator:{settings.local_actor_id}",
            lease_ttl_seconds=settings.handoff_lease_ttl_seconds,
            backoff_max_seconds=settings.handoff_backoff_max_seconds,
            clock=lambda: datetime.now(UTC),
        )
    except BaseException:
        database.dispose()
        close = getattr(gateway_runtime.catalog, "close", None)
        if callable(close):
            close()
        raise
    return HandoffWorkerRuntime(
        coordinator=coordinator,
        database=database,
        catalog=gateway_runtime.catalog,
    )


def summary_json(result: HandoffRunResult) -> str:
    return json.dumps(
        {
            "component": "handoff-worker",
            "status": "ok",
            "claimed": result.claimed,
            "progressed": result.progressed,
            "waiting": result.waiting,
            "converged": result.converged,
            "blocked": result.blocked,
            "failed": result.failed,
            "unknown": result.unknown,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SIDERA Handoff Coordinator")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one handoff batch and exit",
    )
    args = parser.parse_args(argv)
    runtime: HandoffWorkerRuntime | None = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        runtime = build_handoff_worker(settings)
        while True:
            result = runtime.coordinator.run_once(settings.handoff_batch_size)
            print(summary_json(result))
            if args.once:
                return 0
            time.sleep(settings.handoff_poll_interval_seconds)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "component": "handoff-worker",
                    "status": "error",
                    "error": f"{type(exc).__name__}: handoff worker failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        _dispose_runtime(runtime)


def _dispose_runtime(runtime: HandoffWorkerRuntime | None) -> None:
    if runtime is None:
        return
    runtime.database.dispose()
    close = getattr(runtime, "catalog", None)
    close = getattr(close, "close", None)
    if callable(close):
        close()


if __name__ == "__main__":
    raise SystemExit(main())
