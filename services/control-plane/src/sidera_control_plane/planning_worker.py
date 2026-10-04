from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sidera_agents.ports import ExecutionAuthorizer

from sidera_control_plane.acceptance_guard import load_acceptance_guard
from sidera_control_plane.adapters.knowledge_http import KnowledgeHttpAdapter
from sidera_control_plane.application.agent_reconciliation import AgentRunReconciler
from sidera_control_plane.application.agent_worker_adapter import (
    NativeWorkerServices,
    require_gateway_owner,
)
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.knowledge_candidate_submission import (
    FileCandidateProposalSpool,
    KnowledgeCandidateSubmissionService,
)
from sidera_control_plane.application.planning_reconciler import (
    PlanningReconciler,
    PlanningReconcileResult,
)
from sidera_control_plane.application.planning_runner import (
    PlanningBatchResult,
    PlanningRunner,
    RegistryPlannerAdapterFactory,
)
from sidera_control_plane.application.ports import PlanningWorkspaceProvider
from sidera_control_plane.composition.planner_adapters import (
    build_planner_adapter_builders,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


@dataclass(frozen=True, slots=True)
class PlanningWorkerSummary:
    claimed: int
    succeeded: int
    clarifications: int
    retried: int
    blocked: int
    failed: int


class PlanningReconcilerPort(Protocol):
    def run_once(self, limit: int) -> PlanningReconcileResult:
        raise NotImplementedError


class PlanningRunnerPort(Protocol):
    def run_once(self, limit: int) -> PlanningBatchResult:
        raise NotImplementedError


class PlanningWorker:
    def __init__(
        self,
        *,
        reconciler: PlanningReconcilerPort,
        runner: PlanningRunnerPort,
        native_reconciler: AgentRunReconciler | None = None,
        native_workspace_id: UUID | None = None,
        owner_check: Callable[[], bool] | None = None,
    ) -> None:
        self._reconciler = reconciler
        self._runner = runner
        self._native_reconciler = native_reconciler
        self._native_workspace_id = native_workspace_id
        self._owner_check = owner_check

    def run_once(self, limit: int) -> PlanningWorkerSummary:
        require_gateway_owner(self._owner_check)
        recovery = self._reconciler.run_once(limit)
        require_gateway_owner(self._owner_check)
        if (
            self._native_reconciler is not None
            and self._native_workspace_id is not None
        ):
            self._native_reconciler.run_once(self._native_workspace_id, min(limit, 200))
        require_gateway_owner(self._owner_check)
        execution = self._runner.run_once(limit)
        return _summary(recovery, execution)


@dataclass(slots=True)
class PlanningWorkerRuntime:
    worker: PlanningWorker
    database: Database
    catalog: KnowledgeHttpAdapter
    runner: PlanningRunner | None = None
    native_reconciler: AgentRunReconciler | None = None


def build_planning_worker(
    settings: Settings,
    *,
    native_services: NativeWorkerServices | None = None,
    planning_workspace_provider: PlanningWorkspaceProvider | None = None,
    planning_authorizer: ExecutionAuthorizer | None = None,
    native_clock: Callable[[], datetime] | None = None,
    native_wait: Callable[[float], None] | None = None,
    native_poll_interval_seconds: float = 1.0,
    owner_check: Callable[[], bool] | None = None,
) -> PlanningWorkerRuntime:
    database = Database(settings.database_url)
    catalog = KnowledgeHttpAdapter(
        base_url=settings.knowledge_base_url,
        control_plane_token=settings.knowledge_control_plane_token,
        metadata_timeout_seconds=settings.knowledge_metadata_timeout_seconds,
        materialization_timeout_seconds=settings.knowledge_materialization_timeout_seconds,
    )
    acceptance_guard = load_acceptance_guard()
    builders = build_planner_adapter_builders(
        settings,
        acceptance_guard=acceptance_guard,
    )
    try:
        candidate_submission_service = KnowledgeCandidateSubmissionService(
            FileCandidateProposalSpool(
                settings.knowledge_candidate_spool_dir,
                quota_bytes=settings.knowledge_candidate_spool_quota_bytes,
            )
        )
        uow_factory = lambda: SqlAlchemyUnitOfWork(
            database,
            projection_change_retention_seconds=settings.projection_change_retention_seconds,
            before_commit=None
            if owner_check is None
            else lambda: require_gateway_owner(owner_check),
        )
        materializer = ContextMaterializer(catalog)
        run_clock = native_clock or (lambda: datetime.now(UTC))
        factory = RegistryPlannerAdapterFactory(
            builders,
            temp_root=settings.planning_temp_root,
            native_services=native_services,
            uow_factory=uow_factory,
            planning_workspace_provider=planning_workspace_provider,
            planning_context_materializer=materializer,
            planning_authorizer=planning_authorizer,
            native_clock=native_clock,
            native_wait=native_wait,
            native_poll_interval_seconds=native_poll_interval_seconds,
            owner_check=owner_check,
        )
        runner = PlanningRunner(
            uow_factory=uow_factory,
            adapter_factory=factory,
            owner_id=f"planning-worker:{settings.local_actor_id}",
            lease_ttl_seconds=settings.planning_lease_ttl_seconds,
            lease_renew_interval_seconds=settings.planning_lease_renew_interval_seconds,
            max_diagnostic_bytes=settings.planning_max_diagnostic_bytes,
            planner_temp_root=settings.planning_temp_root,
            clock=run_clock,
            context_materializer=materializer,
            context_package_cutover_at=settings.context_package_cutover_at,
            candidate_submission_service=candidate_submission_service,
            acceptance_guard=acceptance_guard,
            owner_check=owner_check,
        )
        reconciler = PlanningReconciler(
            uow_factory=uow_factory,
            owner_id=f"planning-reconciler:{settings.local_actor_id}",
            clock=run_clock,
            planning_context_ttl_seconds=settings.planning_context_ttl_seconds,
        )
        native_reconciler = (
            None
            if native_services is None
            else AgentRunReconciler(
                uow_factory,
                gateway=None,
                services=native_services,
                planning_runner=runner,
                clock=run_clock,
                owner_check=owner_check,
            )
        )
        return PlanningWorkerRuntime(
            worker=PlanningWorker(
                reconciler=reconciler,
                runner=runner,
                native_reconciler=native_reconciler,
                native_workspace_id=settings.gateway_sync_workspace_id,
                owner_check=owner_check,
            ),
            database=database,
            catalog=catalog,
            runner=runner,
            native_reconciler=native_reconciler,
        )
    except BaseException:
        catalog.close()
        database.dispose()
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SIDERA Planning Worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one planning recovery and execution batch and exit",
    )
    args = parser.parse_args(argv)
    runtime: PlanningWorkerRuntime | None = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        runtime = build_planning_worker(settings)
        while True:
            result = runtime.worker.run_once(settings.planning_batch_size)
            print(summary_json(result))
            if args.once:
                return 0
            time.sleep(settings.planning_poll_interval_seconds)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "component": "planning-worker",
                    "status": "error",
                    "error": f"{type(exc).__name__}: planning worker failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if runtime is not None:
            runtime.catalog.close()
            runtime.database.dispose()


def summary_json(summary: PlanningWorkerSummary) -> str:
    return json.dumps(
        {
            "component": "planning-worker",
            "status": "ok",
            "claimed": summary.claimed,
            "succeeded": summary.succeeded,
            "clarifications": summary.clarifications,
            "retried": summary.retried,
            "blocked": summary.blocked,
            "failed": summary.failed,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _summary(
    recovery: PlanningReconcileResult, execution: PlanningBatchResult
) -> PlanningWorkerSummary:
    return PlanningWorkerSummary(
        claimed=execution.claimed,
        succeeded=execution.succeeded,
        clarifications=execution.clarifications,
        retried=recovery.retried,
        blocked=recovery.blocked + execution.blocked,
        failed=recovery.failed + execution.failed,
    )


if __name__ == "__main__":
    raise SystemExit(main())
