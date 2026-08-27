from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from pydantic import SecretStr

from sidera_control_plane.adapters.fake_planner import (
    FakePlannerAdapter,
    FakePlannerConfig,
    FakeScenario,
)
from sidera_control_plane.adapters.knowledge_http import KnowledgeHttpAdapter
from sidera_control_plane.adapters.opencode_planner import OpenCodePlannerAdapter
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
    ) -> None:
        self._reconciler = reconciler
        self._runner = runner

    def run_once(self, limit: int) -> PlanningWorkerSummary:
        recovery = self._reconciler.run_once(limit)
        execution = self._runner.run_once(limit)
        return _summary(recovery, execution)


@dataclass(slots=True)
class PlanningWorkerRuntime:
    worker: PlanningWorker
    database: Database
    catalog: KnowledgeHttpAdapter


def build_planning_worker(settings: Settings) -> PlanningWorkerRuntime:
    database = Database(settings.database_url)
    catalog = KnowledgeHttpAdapter(
        base_url=settings.knowledge_base_url,
        control_plane_token=settings.knowledge_control_plane_token,
        metadata_timeout_seconds=settings.knowledge_metadata_timeout_seconds,
        materialization_timeout_seconds=settings.knowledge_materialization_timeout_seconds,
    )

    def fake_builder(
        config: Mapping[str, object], _secrets: tuple[SecretStr, ...]
    ) -> FakePlannerAdapter:
        scenario = config.get("planning.scenario")
        if not isinstance(scenario, str):
            raise TypeError("fake planner requires planning.scenario")
        allowed = {
            "proposal",
            "clarification",
            "scale",
            "invalid_schema",
            "timeout",
            "unknown_outcome",
        }
        if scenario not in allowed:
            raise ValueError("fake planner scenario is unsupported")
        return FakePlannerAdapter(
            FakePlannerConfig(scenario=cast(FakeScenario, scenario))
        )

    def opencode_builder(
        config: Mapping[str, object], secrets: tuple[SecretStr, ...]
    ) -> OpenCodePlannerAdapter:
        configured_executable = config.get(
            "planning.executable", settings.planning_opencode_executable
        )
        if not isinstance(configured_executable, str) or not configured_executable.strip():
            raise TypeError("OpenCode planner executable is invalid")
        configured_model = config.get(
            "planning.model", settings.planning_opencode_model
        )
        if configured_model is not None and not isinstance(configured_model, str):
            raise TypeError("OpenCode planner model is invalid")
        return OpenCodePlannerAdapter(
            executable=configured_executable,
            model=configured_model,
            temp_root=settings.planning_temp_root,
            request_timeout_seconds=settings.planning_opencode_request_timeout_seconds,
            stop_grace_seconds=settings.planning_opencode_stop_grace_seconds,
            stderr_limit_bytes=settings.planning_acp_stderr_limit_bytes,
            secret_values=secrets,
        )

    factory = RegistryPlannerAdapterFactory(
        {
            "fake": fake_builder,
            "opencode-acp-planner": opencode_builder,
        },
        temp_root=settings.planning_temp_root,
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
        )
        runner = PlanningRunner(
            uow_factory=uow_factory,
            adapter_factory=factory,
            owner_id=f"planning-worker:{settings.local_actor_id}",
            lease_ttl_seconds=settings.planning_lease_ttl_seconds,
            lease_renew_interval_seconds=settings.planning_lease_renew_interval_seconds,
            max_diagnostic_bytes=settings.planning_max_diagnostic_bytes,
            planner_temp_root=settings.planning_temp_root,
            clock=lambda: datetime.now(UTC),
            context_materializer=ContextMaterializer(catalog),
            context_package_cutover_at=settings.context_package_cutover_at,
            candidate_submission_service=candidate_submission_service,
        )
        reconciler = PlanningReconciler(
            uow_factory=uow_factory,
            owner_id=f"planning-reconciler:{settings.local_actor_id}",
            clock=lambda: datetime.now(UTC),
            planning_context_ttl_seconds=settings.planning_context_ttl_seconds,
        )
        return PlanningWorkerRuntime(
            worker=PlanningWorker(reconciler=reconciler, runner=runner),
            database=database,
            catalog=catalog,
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
