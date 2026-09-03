"""Deterministic worker-side driver for the R6.3.1 vertical gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import select

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.application.activation_reconciler import ActivationReconciler
from sidera_control_plane.application.collaboration_decisions import (
    CollaborationDecisionService,
)
from sidera_control_plane.application.collaboration_reconciler import (
    CollaborationReconciler,
)
from sidera_control_plane.application.collaboration_runtime_invoker import (
    CollaborationRuntimeInvoker,
)
from sidera_control_plane.application.collaboration_runtime_resolver import (
    CollaborationRuntimeResolver,
    RegistryCollaborationRuntimeFactory,
)
from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.application.worker_runtime import WorkerRuntimeResolver
from sidera_control_plane.domain.artifacts import (
    Artifact,
    ArtifactCandidate,
    ArtifactType,
    EvidenceClaim,
)
from sidera_control_plane.domain.execution import ExecutionSession, ExecutionStatus
from sidera_control_plane.domain.gateway import ExecutionEvent
from sidera_control_plane.domain.modules import ModuleLifecycle
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    CollaborationRunModel,
    RuntimeReleaseModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.e2e.seed_r6_3_1_collaboration import (
    NOW,
    CollaborationSeed,
    _ids,
)


class CollaborationScenario:
    """Drive one collaboration Run through the existing execution path."""

    def __init__(
        self,
        database: Database,
        seed: CollaborationSeed,
        *,
        now: datetime,
    ) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("scenario timestamp must be timezone-aware")
        self.database = database
        self.seed = seed
        self.now = now
        self.uow_factory = lambda: SqlAlchemyUnitOfWork(database)
        self.owner_id = "r6-3-1-collaboration-worker"

        runtime_factory = RegistryCollaborationRuntimeFactory(
            {
                "reference": lambda _instance: ReferenceCollaborationRuntime(
                    clock=lambda: self.now
                )
            }
        )
        runtime_resolver_factory = lambda modules: CollaborationRuntimeResolver(
            modules,
            runtime_factory,
        )
        fact_store_factory = lambda uow: SqlAlchemyCollaborationFactStore(
            session=uow.session
        )
        decision_service = CollaborationDecisionService(
            uow_factory=self.uow_factory,
            fact_store_factory=fact_store_factory,
            owner_id=self.owner_id,
        )
        self.collaboration_reconciler = CollaborationReconciler(
            uow_factory=self.uow_factory,
            fact_store_factory=fact_store_factory,
            runtime_resolver_factory=runtime_resolver_factory,
            decision_service=decision_service,
            evaluation_lease_ttl=timedelta(seconds=5),
            recovery_limit=20,
            default_workspace_id=seed.workspace_id,
            default_root_task_id=seed.root_task_id,
        )
        self.collaboration_invoker = CollaborationRuntimeInvoker(
            runtime_resolver_factory=runtime_resolver_factory,
            resolution_uow_factory=self.uow_factory,
            evaluation_timeout=timedelta(seconds=2),
            lease_safety_margin=timedelta(seconds=1),
            max_projection_bytes=262_144,
            max_decision_bytes=65_536,
            max_intents=128,
            max_strategy_state_bytes=16_384,
        )
        from sidera_control_plane.collaboration_worker import CollaborationWorker

        self.collaboration_worker = CollaborationWorker(
            reconciler=self.collaboration_reconciler,
            invoker=self.collaboration_invoker,
            owner_id=self.owner_id,
            clock=lambda: self.now,
            max_attempts=2,
            backoff_seconds=1,
            backoff_max_seconds=2,
        )
        self.activation = ActivationReconciler(
            uow_factory=self.uow_factory,
            owner_id="r6-3-1-activation-reconciler",
            clock=lambda: self.now,
            execution_context_ttl_seconds=7_200,
        )
        self.scheduler = Scheduler(
            self.uow_factory,
            LowestCostWorkerPolicy(),
            lambda: self.now,
            assignment_lease_seconds=30,
            worker_heartbeat_timeout_seconds=45,
            runtime_resolver=WorkerRuntimeResolver(),
            monotonic_clock=lambda: 1.0,
            context_enforcement=False,
        )

    def confirm_v4_plan(self) -> None:
        """Assert the fixture is already at the confirmed collaboration plan."""
        with self.uow_factory() as uow:
            snapshot = uow.plans.get(self.seed.plan_snapshot_id, self.seed.workspace_id)
            if snapshot is None or snapshot.schema_version != 3:
                raise AssertionError("R6.3.1 fixture plan is not a v3 collaboration plan")
            if snapshot.collaboration_policy is None:
                raise AssertionError("R6.3.1 fixture plan has no collaboration policy")

    def run_collaboration_until_idle(self, *, max_batches: int = 12) -> None:
        for _ in range(max_batches):
            summary = self.collaboration_worker.run_once(limit=20)
            if summary.failed:
                raise AssertionError(f"collaboration worker failed: {summary}")
            if summary.evaluated == 0:
                return
        raise AssertionError("collaboration worker did not become idle")

    def released_keys(self) -> set[str]:
        with self.database.session() as session:
            rows = session.scalars(
                select(RuntimeReleaseModel.execution_request_id).where(
                    RuntimeReleaseModel.workspace_id == self.seed.workspace_id,
                    RuntimeReleaseModel.run_id.in_(
                        select(CollaborationRunModel.id).where(
                            CollaborationRunModel.root_task_id == self.seed.root_task_id,
                            CollaborationRunModel.workspace_id == self.seed.workspace_id,
                        )
                    ),
                )
            ).all()
        return {
            key
            for key, request_id in self.seed.request_ids.items()
            if request_id in set(rows)
        }

    def worker_state(self, key: str) -> str:
        request_id = self.seed.request_ids[key]
        with self.uow_factory() as uow:
            request = uow.execution_requests.get(request_id, self.seed.workspace_id)
            if request is None:
                raise AssertionError(f"request is missing: {key}")
            if key == "downstream":
                for dependency in uow.execution_requests.list_dependencies(
                    request.id, self.seed.workspace_id
                ):
                    if not dependency.required_evidence_refs:
                        continue
                    upstream = uow.execution_requests.get(
                        dependency.upstream_request_id,
                        self.seed.workspace_id,
                    )
                    if upstream is None or upstream.first_execution_id is None:
                        return "waiting_dependencies"
                    claims = uow.evidence_claims.list_for_executions(
                        (upstream.first_execution_id,),
                        self.seed.workspace_id,
                    )
                    refs = {
                        f"acceptance:{claim.acceptance_criterion_ref}" for claim in claims
                    }
                    if not set(dependency.required_evidence_refs).issubset(refs):
                        return "waiting_evidence"
            if request.status.value in {"materialized", "ready"}:
                return request.status.value
            return request.gate_reason

    def succeed(self, key: str, *, evidence: Iterable[str] = ()) -> None:
        execution = self._materialize_and_schedule(key)
        if execution.status is ExecutionStatus.SUCCEEDED:
            return
        if execution.status not in {ExecutionStatus.LEASED, ExecutionStatus.RUNNING}:
            raise AssertionError(
                f"execution for {key} is not runnable: {execution.status.value}"
            )

        with self.uow_factory() as uow:
            current = uow.executions.get(execution.id, self.seed.workspace_id, for_update=True)
            if current is None:
                raise AssertionError(f"execution disappeared: {execution.id}")
            lease = uow.leases.get_active_for_execution(
                current.id,
                self.seed.workspace_id,
                for_update=True,
            )
            if lease is None:
                raise AssertionError(f"active execution lease is missing: {current.id}")
            worker = uow.workers.get(
                current.assigned_worker_id,
                self.seed.workspace_id,
                for_update=True,
            ) if current.assigned_worker_id is not None else None
            previous_execution_version = current.version
            previous_worker_version = worker.version if worker is not None else None
            if current.status is ExecutionStatus.LEASED:
                current.start(
                    lease_id=lease.id,
                    fencing_token=lease.fencing_token,
                    now=self.now,
                )
                if worker is not None:
                    worker.mark_running()
            current.succeed(
                lease_id=lease.id,
                fencing_token=lease.fencing_token,
                now=self.now,
            )
            uow.executions.save(current, previous_execution_version)
            if worker is not None and previous_worker_version is not None:
                worker.release()
                uow.workers.save(worker, previous_worker_version)
            previous_lease_version = lease.version
            lease.release(
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
                now=self.now,
            )
            uow.leases.save(lease, previous_lease_version)
            artifact = self._artifact(current.id, current.task_id, key, "diff")
            uow.artifacts.add(artifact)
            uow.flush()
            for criterion in evidence:
                self._add_evidence(
                    uow,
                    execution_id=current.id,
                    task_id=current.task_id,
                    artifact_id=artifact.id,
                    criterion=criterion,
                )

    def record_required_evidence(self, key: str, criterion: str) -> None:
        request_id = self.seed.request_ids[key]
        with self.uow_factory() as uow:
            request = uow.execution_requests.get(request_id, self.seed.workspace_id)
            if request is None or request.first_execution_id is None:
                raise AssertionError(f"successful execution is missing for {key}")
            execution = uow.executions.get(
                request.first_execution_id,
                self.seed.workspace_id,
                for_update=True,
            )
            if execution is None or execution.status is not ExecutionStatus.SUCCEEDED:
                raise AssertionError(f"execution is not successful for {key}")
            existing = uow.evidence_claims.list_for_executions(
                (execution.id,),
                self.seed.workspace_id,
            )
            normalized = criterion.removeprefix("acceptance:")
            if any(item.acceptance_criterion_ref == normalized for item in existing):
                return
            artifacts = uow.artifacts.list_for_execution(
                execution.id,
                self.seed.workspace_id,
            )
            if not artifacts:
                raise AssertionError(f"artifact is missing for {key}")
            report = self._artifact(execution.id, execution.task_id, key, "report")
            uow.artifacts.add(report)
            uow.flush()
            self._add_evidence(
                uow,
                execution_id=execution.id,
                task_id=execution.task_id,
                artifact_id=report.id,
                criterion=normalized,
            )

    def run_existing_scheduler_and_workers(self) -> None:
        self._materialize_and_schedule("downstream")
        self.succeed("downstream", evidence=("downstream",))
        self.run_collaboration_until_idle()

    def prepare_browser_state(self) -> dict[str, str]:
        """Create a mixed state for the browser gate without fake API responses."""
        self.run_collaboration_until_idle()
        activation = self.activation.run_once(limit=20)
        scheduler = self.scheduler.run_once(limit=20)
        del activation, scheduler
        execution_id, session_id = self._start_browser_session("upstream-a")
        self._block_runtime()
        self.run_collaboration_until_idle()
        return {
            "run_status": self.run_status(),
            "execution_id": str(execution_id),
            "session_id": str(session_id),
        }

    def run_status(self) -> str:
        with self.database.session() as session:
            value = session.scalar(
                select(CollaborationRunModel.status).where(
                    CollaborationRunModel.workspace_id == self.seed.workspace_id,
                    CollaborationRunModel.root_task_id == self.seed.root_task_id,
                )
            )
        if value is None:
            raise AssertionError("collaboration run is missing")
        return str(value)

    def _materialize_and_schedule(self, key: str):
        request_id = self.seed.request_ids[key]
        for _ in range(8):
            activation_result = self.activation.run_once(limit=20)
            scheduler_result = self.scheduler.run_once(limit=20)
            del activation_result, scheduler_result
            with self.uow_factory() as uow:
                request = uow.execution_requests.get(request_id, self.seed.workspace_id)
                if request is None:
                    raise AssertionError(f"request is missing: {key}")
                if request.first_execution_id is None:
                    continue
                execution = uow.executions.get(
                    request.first_execution_id,
                    self.seed.workspace_id,
                )
                if execution is None:
                    continue
                if execution.status in {
                    ExecutionStatus.LEASED,
                    ExecutionStatus.RUNNING,
                    ExecutionStatus.SUCCEEDED,
                }:
                    return execution
        raise AssertionError(f"request was not scheduled: {key}")

    def _start_browser_session(self, key: str) -> tuple[UUID, UUID]:
        request_id = self.seed.request_ids[key]
        with self.uow_factory() as uow:
            request = uow.execution_requests.get(request_id, self.seed.workspace_id)
            if request is None or request.first_execution_id is None:
                raise AssertionError(f"request was not materialized: {key}")
            execution = uow.executions.get(
                request.first_execution_id,
                self.seed.workspace_id,
                for_update=True,
            )
            if execution is None or execution.assigned_worker_id is None:
                raise AssertionError(f"execution was not assigned: {key}")
            existing = uow.sessions.get_latest_for_execution(
                execution.id,
                self.seed.workspace_id,
            )
            if existing is not None:
                return execution.id, existing.id
            lease = uow.leases.get_active_for_execution(
                execution.id,
                self.seed.workspace_id,
                for_update=True,
            )
            worker = uow.workers.get(
                execution.assigned_worker_id,
                self.seed.workspace_id,
                for_update=True,
            )
            binding = uow.bindings.get_for_execution(
                execution.id,
                self.seed.workspace_id,
            )
            if lease is None or worker is None or binding is None:
                raise AssertionError(f"runtime binding is incomplete: {key}")
            if execution.status is ExecutionStatus.LEASED:
                previous_execution_version = execution.version
                execution.start(
                    lease_id=lease.id,
                    fencing_token=lease.fencing_token,
                    now=self.now,
                )
                uow.executions.save(execution, previous_execution_version)
            elif execution.status is not ExecutionStatus.RUNNING:
                raise AssertionError(
                    f"browser execution is not active: {execution.status.value}"
                )
            previous_worker_version = worker.version
            worker.mark_running()
            uow.workers.save(worker, previous_worker_version)
            session_id = uuid5(
                NAMESPACE_URL,
                f"sidera:r6.3.1:browser-session:{execution.id}",
            )
            session = ExecutionSession.start(
                session_id=session_id,
                workspace_id=self.seed.workspace_id,
                execution_id=execution.id,
                worker_id=worker.id,
                adapter_session_id=f"r6-3-1-browser-{key}",
                now=self.now,
                adapter_module_id=binding.adapter_module_id,
                adapter_config_version=binding.adapter_config_version,
                workspace_module_id=binding.workspace_module_id,
                workspace_config_version=binding.workspace_config_version,
                capability_snapshot=binding.capability_snapshot,
                process_ref=f"fixture://r6-3-1/{key}/session",
                workspace_ref="fixture://r6-3-1/browser-workspace",
            )
            session.input_mode = "queued"
            event_at = self.now + timedelta(seconds=1)
            session.record_activity(sequence=1, now=event_at)
            uow.sessions.add(session)
            uow.flush()
            uow.execution_events.add(
                ExecutionEvent(
                    id=uuid5(
                        NAMESPACE_URL,
                        f"sidera:r6.3.1:browser-event:{execution.id}:1",
                    ),
                    workspace_id=self.seed.workspace_id,
                    execution_id=execution.id,
                    session_id=session.id,
                    sequence=1,
                    event_type="tool.call",
                    payload={
                        "tool": "pytest",
                        "summary": "正在运行上游验证",
                        "duration_ms": 420,
                        "trace_usage": {
                            "input_tokens": 120,
                            "output_tokens": 64,
                            "cached_tokens": 32,
                            "cost_microunits": 7,
                        },
                    },
                    occurred_at=event_at,
                    raw_ref=None,
                )
            )
            return execution.id, session.id

    def _block_runtime(self) -> None:
        with self.uow_factory() as uow:
            instances = uow.modules.get_instances(
                (self.seed.runtime_instance_id,),
                self.seed.workspace_id,
                for_update=True,
            )
            instance = instances[0] if instances else None
            if instance is None:
                raise AssertionError("collaboration runtime instance is missing")
            if instance.lifecycle is ModuleLifecycle.ACTIVE:
                changed = instance.with_lifecycle(ModuleLifecycle.DRAINING)
                uow.modules.save_instance(changed, instance.resource_version)
                resource_version = changed.resource_version
            else:
                resource_version = instance.resource_version
            uow.projection_changes.changed(
                workspace_id=self.seed.workspace_id,
                projection_type="collaboration_runtime",
                resource_id=self.seed.root_task_id,
                change_type="fixture_runtime_draining",
                resource_version=resource_version,
                correlation_id=self.seed.runtime_instance_id,
            )

    def _artifact(
        self,
        execution_id: UUID,
        task_id: UUID,
        key: str,
        artifact_kind: str,
    ) -> Artifact:
        content = f"r6.3.1:{key}:{artifact_kind}:{execution_id}".encode()
        candidate = ArtifactCandidate(
            artifact_type=(ArtifactType.DIFF if artifact_kind == "diff" else ArtifactType.REPORT),
            storage_ref=f"fixture://r6.3.1/{execution_id}/{artifact_kind}",
            content_hash=sha256(content).hexdigest(),
            size_bytes=len(content),
            media_type="text/plain",
            provenance={"producer": "r6-3-1-fake-worker", "worker_item_key": key},
            content=content,
        )
        return Artifact.from_candidate(
            id=uuid5(
                NAMESPACE_URL,
                f"sidera:r6.3.1:artifact:{execution_id}:{artifact_kind}",
            ),
            workspace_id=self.seed.workspace_id,
            task_id=task_id,
            execution_id=execution_id,
            session_id=None,
            candidate=candidate,
        )

    def _add_evidence(
        self,
        uow: SqlAlchemyUnitOfWork,
        *,
        execution_id: UUID,
        task_id: UUID,
        artifact_id: UUID,
        criterion: str,
    ) -> None:
        normalized = criterion.removeprefix("acceptance:")
        claim = EvidenceClaim.create_candidate(
            id=uuid5(
                NAMESPACE_URL,
                f"sidera:r6.3.1:evidence:{execution_id}:{normalized}",
            ),
            workspace_id=self.seed.workspace_id,
            task_id=task_id,
            execution_id=execution_id,
            acceptance_criterion_ref=normalized,
            conclusion="The deterministic worker produced the required evidence.",
            environment_ref="r6.3.1-e2e",
            artifact_ids=(artifact_id,),
            producer="r6-3-1-fake-worker",
            created_at=self.now,
        )
        uow.evidence_claims.add(claim)


def _seed_for_workspace(workspace_id: UUID) -> CollaborationSeed:
    ids = _ids(workspace_id)
    return CollaborationSeed(
        workspace_id=workspace_id,
        root_task_id=ids["root_task_id"],
        plan_snapshot_id=ids["plan_snapshot_id"],
        planning_request_id=ids["planning_request_id"],
        proposal_id=ids["proposal_id"],
        runtime_definition_id=ids["runtime_definition_id"],
        runtime_instance_id=ids["runtime_instance_id"],
        subtask_ids={
            key: ids[f"subtask_{key}_id"]
            for key in ("upstream-a", "upstream-b", "downstream")
        },
        worker_item_ids={
            key: ids[f"worker_item_{key}_id"]
            for key in ("upstream-a", "upstream-b", "downstream")
        },
        request_ids={
            key: ids[f"request_{key}_id"]
            for key in ("upstream-a", "upstream-b", "downstream")
        },
        worker_ids={
            key: ids[f"worker_{key}_id"]
            for key in ("upstream-a", "upstream-b", "downstream")
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument(
        "--browser-state",
        action="store_true",
        help="prepare a mixed state for the browser vertical gate",
    )
    args = parser.parse_args(argv)
    database_url = os.environ.get("SIDERA_DATABASE_URL")
    if not database_url:
        raise SystemExit("SIDERA_DATABASE_URL is required for the R6.3.1 runtime")
    parsed = urlsplit(database_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("R6.3.1 runtime requires a loopback database host")
    try:
        workspace_id = UUID(args.workspace_id)
    except ValueError as exc:
        raise SystemExit("--workspace-id must be a UUID") from exc
    database = Database(database_url)
    try:
        seed = _seed_for_workspace(workspace_id)
        scenario = CollaborationScenario(database, seed, now=NOW)
        if args.browser_state:
            result = scenario.prepare_browser_state()
        else:
            scenario.run_collaboration_until_idle()
            result = {"run_status": scenario.run_status()}
    finally:
        database.dispose()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
