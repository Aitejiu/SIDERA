from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.application.activation_reconciler import ActivationReconciler
from sidera_control_plane.application.collaboration_decisions import (
    CollaborationDecisionApplicationResult,
    CollaborationDecisionService,
)
from sidera_control_plane.application.collaboration_reconciler import (
    CollaborationReconciler,
)
from sidera_control_plane.application.collaboration_runtime_invoker import (
    CollaborationRuntimeInvoker,
    RuntimeInvocationResult,
)
from sidera_control_plane.application.collaboration_runtime_resolver import (
    CollaborationRuntimeResolver,
    RegistryCollaborationRuntimeFactory,
    ResolvedCollaborationRuntime,
)
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.event_ingestor import ArtifactRegistry
from sidera_control_plane.application.execution_commands import ExecutionCommandHandler
from sidera_control_plane.application.planning_commands import PlanningCommandHandler
from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
)
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.domain.collaboration import (
    CollaborationRuntimeBindingSnapshot,
)
from sidera_control_plane.domain.collaboration_runtime import (
    RuntimeDecisionV2,
    RuntimeProjectionV2,
    canonical_json_bytes,
    parse_runtime_decision_v2,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.execution import ExecutionSession
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    StabilityLevel,
)
from sidera_control_plane.domain.planning import PlanningRequest
from sidera_control_plane.domain.planning_proposals import (
    PlanningProfileDescriptor,
    PlanningProposalPayloadV5,
    ProposalRevision,
)
from sidera_control_plane.domain.work_context import (
    WorkContext,
    WorkContextRevisionReason,
)
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    CollaborationInputSelectionModel,
    ExecutionModel,
    ExecutionRequestModel,
    RuntimeReleaseModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.integration.test_proposal_confirmation import planner_capabilities, profile
from tests.unit.result_input_fixtures import proposal_v5_mapping

FROZEN_NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


class _StaticRuntimeResolver:
    def __init__(self, runtime: ReferenceCollaborationRuntime) -> None:
        self.runtime = runtime

    def resolve(self, run: Any) -> ResolvedCollaborationRuntime:
        return ResolvedCollaborationRuntime(
            runtime=self.runtime,
            provider="reference",
            interface_version=run.runtime_interface_version,
            config_version=run.runtime_config_version,
        )


class ResultInputScenario:
    """Shared real-persistence harness grown incrementally by R6.3.4 tasks."""

    def __init__(self, database: Database, workspace_id: UUID, tmp_path: Path) -> None:
        self.database = database
        self.workspace_id = workspace_id
        self.tmp_path = tmp_path
        self.now = FROZEN_NOW
        self.clock: Callable[[], datetime] = lambda: self.now
        self.uow_factory: Callable[[], SqlAlchemyUnitOfWork] = lambda: (
            SqlAlchemyUnitOfWork(self.database)
        )
        self.artifact_store = LocalArtifactContentStore(
            tmp_path / "result-input-artifacts"
        )
        self.ids: dict[str, UUID] = {
            name: uuid4()
            for name in (
                "root_task",
                "plan_snapshot",
                "collaboration_run",
                "projection_snapshot",
                "decision",
                "intent",
                "target_worker_item",
                "target_execution_request",
            )
        }
        self.worker_item_ids: dict[str, UUID] = {}
        self.request_ids: dict[str, UUID] = {}
        self._runtime = ReferenceCollaborationRuntime(clock=self.clock)
        self._decision_service = CollaborationDecisionService(
            uow_factory=self.uow_factory,
            fact_store_factory=lambda uow: SqlAlchemyCollaborationFactStore(
                session=cast(SqlAlchemyUnitOfWork, uow).session
            ),
            owner_id="result-input-scenario",
        )
        self._collaboration_reconciler = CollaborationReconciler(
            uow_factory=self.uow_factory,
            fact_store_factory=lambda uow: SqlAlchemyCollaborationFactStore(
                session=cast(SqlAlchemyUnitOfWork, uow).session
            ),
            runtime_resolver_factory=lambda modules: CollaborationRuntimeResolver(
                modules,
                RegistryCollaborationRuntimeFactory(
                    {"reference": lambda _instance: self._runtime}
                ),
            ),
            decision_service=self._decision_service,
            evaluation_lease_ttl=timedelta(seconds=10),
        )

    def confirm_plan(
        self,
        *,
        groups: tuple[dict[str, object], ...] | None = None,
        hard_edges: tuple[tuple[str, str], ...] = (),
    ) -> None:
        planner_capability = replace(
            planner_capabilities(), proposal_schema_versions=(5,)
        )
        runtime_capability = CollaborationRuntimeCapabilityProfile(
            interface_version="collaboration-runtime-v2",
            projection_schema_versions=(2,),
            decision_schema_versions=(2,),
            strategy_ids=("all_required", "input_groups_v1"),
            idempotency=IdempotencyCapability.OPERATION_ID,
            max_projection_bytes=262_144,
            max_decision_bytes=65_536,
            max_intents_per_decision=128,
            max_strategy_state_bytes=16_384,
            stability=StabilityLevel.EXPERIMENTAL,
        )
        planner_definition = ModuleDefinition(
            id=uuid4(),
            workspace_id=self.workspace_id,
            module_type="planner_adapter",
            provider="fake",
            interface_version=planner_capability.interface_version,
            config_schema_version=1,
            capabilities=planner_capability,
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        planner_instance = ModuleInstance(
            id=uuid4(),
            workspace_id=self.workspace_id,
            definition_id=planner_definition.id,
            instance_name="result-input-planner",
            config_version=1,
            config_namespace="planning",
            config={},
            secret_refs=(),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        runtime_definition = ModuleDefinition(
            id=uuid4(),
            workspace_id=self.workspace_id,
            module_type="collaboration_runtime",
            provider="reference",
            interface_version=runtime_capability.interface_version,
            config_schema_version=1,
            capabilities=runtime_capability,
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        runtime_instance = ModuleInstance(
            id=uuid4(),
            workspace_id=self.workspace_id,
            definition_id=runtime_definition.id,
            instance_name="result-input-runtime",
            config_version=1,
            config_namespace="collaboration",
            config={},
            secret_refs=(),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        binding = CollaborationRuntimeBindingSnapshot(
            runtime_definition_id=runtime_definition.id,
            runtime_instance_id=runtime_instance.id,
            runtime_interface_version=runtime_capability.interface_version,
            runtime_config_version=runtime_instance.config_version,
            runtime_capability_snapshot=runtime_capability,
        )
        stored_profile = replace(
            profile(self.workspace_id, planner_instance.id),
            proposal_schema_version=5,
            collaboration_runtime_binding=binding,
        )
        request = PlanningRequest.create(
            request_id=uuid4(),
            workspace_id=self.workspace_id,
            profile_snapshot=stored_profile.snapshot(planner_capability),
            objective="Aggregate two completed worker results",
            constraints=("Freeze selected result inputs",),
            acceptance_criteria=("The summary receives a deterministic quorum",),
            context_refs=("context:result-input",),
            requested_outputs=("summary",),
            source_type="integration",
            source_ref="test:result-input",
            actor="operator",
            correlation_id=uuid4(),
            run_id=uuid4(),
            now=self.now,
        )
        with self.uow_factory() as uow:
            uow.modules.add_definition(planner_definition)
            uow.modules.add_instance(planner_instance)
            uow.modules.add_definition(runtime_definition)
            uow.modules.add_instance(runtime_instance)
            uow.planning_profiles.add(stored_profile)
            uow.planning_requests.add(request)
            context_revision = WorkContextRevisionFactory().create(
                work_context_id=uuid4(),
                workspace_id=self.workspace_id,
                brief=request.briefs[0],
                revision_number=1,
                reason=WorkContextRevisionReason.REQUEST_CREATED,
                actor="operator",
                created_at=self.now,
            )
            uow.work_contexts.add(
                WorkContext.create(
                    work_context_id=context_revision.work_context_id,
                    workspace_id=self.workspace_id,
                    planning_request_id=request.id,
                    first_revision=context_revision,
                    created_at=self.now,
                ),
                context_revision,
            )

        payload = PlanningProposalPayloadV5.from_mapping(
            _proposal_mapping(groups=groups, hard_edges=hard_edges)
        )
        with self.uow_factory() as uow:
            stored_request = uow.planning_requests.get(
                request.id, self.workspace_id, for_update=True
            )
            assert stored_request is not None
            run = stored_request.runs[0]
            previous_request_version = stored_request.version
            previous_run_version = run.version
            lease_id = uuid4()
            stored_request.start_planning(
                run_id=run.id,
                lease_id=lease_id,
                fencing_token=1,
                now=self.now,
            )
            stored_request.succeed_run(
                run_id=run.id,
                lease_id=lease_id,
                fencing_token=1,
                provider_ref="fake:result-input",
                structured_result_type="planning.proposal.v5",
                usage=run.usage,
                now=self.now,
            )
            proposal = ProposalRevision.create(
                planning_request_id=stored_request.id,
                revision=1,
                brief_revision=1,
                planning_run_id=run.id,
                kind=stored_request.kind,
                payload=payload,
                profile_descriptor=PlanningProfileDescriptor.from_snapshot(
                    stored_request.profile_snapshot
                ),
                profile_snapshot=stored_request.profile_snapshot,
                created_at=self.now,
            )
            stored_request.mark_proposal_ready(
                run_id=run.id,
                proposal_revision=proposal.revision,
                content_hash=proposal.content_hash,
                now=self.now,
            )
            uow.planning_records.add_proposal(proposal, self.workspace_id)
            uow.planning_runs.save(run, previous_run_version)
            uow.planning_requests.save(stored_request, previous_request_version)
            expected_version = stored_request.version

        result = CommandService(
            self.uow_factory,
            (
                PlanningCommandHandler(
                    local_actor_type="local",
                    local_actor_id="operator",
                    clock=self.clock,
                ),
            ),
        ).handle(
            CommandEnvelope(
                id=uuid4(),
                workspace_id=self.workspace_id,
                command_type="ConfirmPlanningProposal",
                target_type="planning_request",
                actor_type="local",
                actor_id="operator",
                target_id=request.id,
                payload={
                    "proposal_revision": 1,
                    "proposal_content_hash": proposal.content_hash,
                    "profile_id": str(stored_profile.id),
                    "profile_version": stored_profile.version,
                },
                expected_version=expected_version,
                idempotency_key=f"result-input-confirm:{request.id}",
                correlation_id=uuid4(),
            )
        )
        self.ids["planning_request"] = request.id
        self.ids["root_task"] = UUID(str(result.data["root_task_id"]))
        self.ids["plan_snapshot"] = UUID(str(result.data["plan_snapshot_id"]))
        worker_items = result.data["worker_item_mapping"]
        requests = result.data["execution_request_mapping"]
        assert isinstance(worker_items, dict)
        assert isinstance(requests, dict)
        self.worker_item_ids = {
            key: UUID(str(value)) for key, value in worker_items.items()
        }
        self.request_ids = {key: UUID(str(value)) for key, value in requests.items()}

    def complete_source(self, key: str, *, body: bytes, evidence: bool = True) -> UUID:
        self._ensure_source_executions()
        execution_id = self._execution_id(key)
        assert execution_id is not None
        worker_id, lease_id, session_id, execution_version = self._start_source(
            execution_id, key=key
        )
        digest = sha256(body).hexdigest()
        storage_ref = self.artifact_store.put(
            self.workspace_id, execution_id, digest, body
        )
        provenance: dict[str, object] = {
            "producer": f"scenario:{key}",
            "environment_ref": "env://result-input-scenario",
        }
        if evidence:
            provenance.update(
                {
                    "acceptance_criterion_ref": "build",
                    "conclusion": f"{key} produced its build result",
                }
            )
        report = ArtifactRegistry(self.uow_factory, clock=self.clock).register(
            workspace_id=self.workspace_id,
            task_id=self._execution_task_id(execution_id),
            execution_id=execution_id,
            session_id=session_id,
            candidates=(
                ArtifactCandidate(
                    artifact_type=ArtifactType.BUILD,
                    storage_ref=storage_ref,
                    content_hash=digest,
                    size_bytes=len(body),
                    media_type="text/plain",
                    provenance=provenance,
                ),
            ),
            acceptance_criteria=("build",),
            producer=f"scenario:{key}",
            environment_ref="env://result-input-scenario",
        )
        CommandService(self.uow_factory, (ExecutionCommandHandler(self.clock),)).handle(
            CommandEnvelope(
                id=uuid4(),
                workspace_id=self.workspace_id,
                command_type="ReportExecutionSucceeded",
                target_type="execution",
                actor_type="worker",
                actor_id=str(worker_id),
                target_id=execution_id,
                payload={
                    "worker_id": str(worker_id),
                    "lease_id": str(lease_id),
                    "fencing_token": 1,
                },
                expected_version=execution_version,
                idempotency_key=f"result-input-succeeded:{execution_id}",
                correlation_id=self.ids["root_task"],
            )
        )
        assert len(report.artifacts) == 1
        return report.artifacts[0].id

    def reconcile(self) -> None:
        for _ in range(10):
            if not self._reconcile_once():
                return
        raise AssertionError("collaboration reconciliation did not converge")

    def release_count(self, key: str) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(RuntimeReleaseModel)
                    .where(
                        RuntimeReleaseModel.workspace_id == self.workspace_id,
                        RuntimeReleaseModel.execution_request_id
                        == self.request_ids[key],
                    )
                )
                or 0
            )

    def selection_count(self, key: str) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(CollaborationInputSelectionModel)
                    .where(
                        CollaborationInputSelectionModel.workspace_id
                        == self.workspace_id,
                        CollaborationInputSelectionModel.target_execution_request_id
                        == self.request_ids[key],
                    )
                )
                or 0
            )

    def request_selection_ref(self, key: str) -> tuple[UUID | None, str | None]:
        with self.database.session() as session:
            request = session.get(ExecutionRequestModel, self.request_ids[key])
        assert request is not None
        return request.input_selection_id, request.input_selection_hash

    def prepare_runtime_result(self) -> tuple[Any, RuntimeInvocationResult]:
        prepared = self._collaboration_reconciler.prepare_next(
            owner_id="result-input-scenario",
            now=self.now,
            workspace_id=self.workspace_id,
            root_task_id=self.ids["root_task"],
        )
        assert prepared is not None
        invocation = CollaborationRuntimeInvoker(
            runtime_resolver=_StaticRuntimeResolver(self._runtime),
            evaluation_timeout=timedelta(seconds=30),
            lease_safety_margin=timedelta(seconds=1),
            monotonic_clock=iter((0.0, 0.1)).__next__,
        ).invoke(prepared, now=self.now)
        assert isinstance(invocation, RuntimeInvocationResult)
        return prepared, invocation

    def apply_runtime_result(
        self,
        prepared: Any,
        invocation: RuntimeInvocationResult,
        *,
        service: CollaborationDecisionService | None = None,
    ) -> CollaborationDecisionApplicationResult:
        assert self._collaboration_reconciler.complete_success(
            prepared, invocation, now=self.now
        )
        return self._collaboration_reconciler.complete_decision(
            prepared,
            invocation,
            now=self.now,
            decision_service=service,
        )

    def reconcile_mutated(
        self,
        mutate: Callable[[dict[str, object]], None],
    ) -> CollaborationDecisionApplicationResult:
        prepared, invocation = self.prepare_runtime_result()
        assert isinstance(invocation.decision, RuntimeDecisionV2)
        assert isinstance(prepared.projection, RuntimeProjectionV2)
        raw = deepcopy(invocation.decision.to_mapping())
        mutate(raw)
        response_bytes = canonical_json_bytes(raw)
        mutated = RuntimeInvocationResult(
            response_bytes=response_bytes,
            response_hash=sha256(response_bytes).hexdigest(),
            decision=parse_runtime_decision_v2(
                response_bytes,
                max_bytes=65_536,
                max_intents=128,
                max_state_bytes=16_384,
            ),
            duration_ms=invocation.duration_ms,
        )
        return self.apply_runtime_result(prepared, mutated)

    def decision_service(
        self,
        *,
        plan_transition_allows: Callable[[Any, Any], bool] | None = None,
    ) -> CollaborationDecisionService:
        return CollaborationDecisionService(
            uow_factory=self.uow_factory,
            fact_store_factory=lambda uow: SqlAlchemyCollaborationFactStore(
                session=cast(SqlAlchemyUnitOfWork, uow).session
            ),
            owner_id="result-input-scenario",
            plan_transition_allows=plan_transition_allows,
        )

    def selection(self, key: str):
        with self.uow_factory() as uow:
            request = uow.execution_requests.get(
                self.request_ids[key], self.workspace_id
            )
            assert request is not None
            assert request.input_selection_id is not None
            selection = uow.input_selections.get(
                self.workspace_id, request.input_selection_id
            )
            assert selection is not None
            return selection

    def _reconcile_once(self) -> bool:
        prepared = self._collaboration_reconciler.prepare_next(
            owner_id="result-input-scenario",
            now=self.now,
            workspace_id=self.workspace_id,
            root_task_id=self.ids["root_task"],
        )
        if prepared is None:
            return False
        invocation = CollaborationRuntimeInvoker(
            runtime_resolver=_StaticRuntimeResolver(self._runtime),
            evaluation_timeout=timedelta(seconds=30),
            lease_safety_margin=timedelta(seconds=1),
            monotonic_clock=iter((0.0, 0.1)).__next__,
        ).invoke(prepared, now=self.now)
        assert isinstance(invocation, RuntimeInvocationResult)
        assert self._collaboration_reconciler.complete_success(
            prepared, invocation, now=self.now
        )
        self._collaboration_reconciler.complete_decision(
            prepared, invocation, now=self.now
        )
        return True

    def _ensure_source_executions(self) -> None:
        if self._execution_id("a") is not None:
            return
        assert self._reconcile_once()
        ActivationReconciler(
            uow_factory=self.uow_factory,
            owner_id="result-input-activation",
            clock=self.clock,
        ).run_once(limit=20)
        for key in ("a", "b", "c"):
            assert self._execution_id(key) is not None

    def _execution_id(self, key: str) -> UUID | None:
        worker_item_id = self.worker_item_ids[key]
        with self.database.session() as session:
            return session.scalar(
                select(ExecutionModel.id).where(
                    ExecutionModel.workspace_id == self.workspace_id,
                    ExecutionModel.worker_item_id == worker_item_id,
                )
            )

    def _execution_task_id(self, execution_id: UUID) -> UUID:
        with self.database.session() as session:
            task_id = session.scalar(
                select(ExecutionModel.task_id).where(
                    ExecutionModel.workspace_id == self.workspace_id,
                    ExecutionModel.id == execution_id,
                )
            )
        assert task_id is not None
        return task_id

    def _start_source(
        self, execution_id: UUID, *, key: str
    ) -> tuple[UUID, UUID, UUID, int]:
        worker = Worker.register(
            worker_id=uuid4(),
            workspace_id=self.workspace_id,
            adapter_type="scenario",
            display_name=f"Scenario worker {key}",
            capabilities=frozenset({"code"}),
            capacity=1,
            cost_per_hour_microunits=0,
            startup_latency_ms=0,
            now=self.now,
        )
        lease = AssignmentLease.issue(
            lease_id=uuid4(),
            workspace_id=self.workspace_id,
            execution_id=execution_id,
            worker_id=worker.id,
            issued_at=self.now,
            expires_at=self.now + timedelta(minutes=5),
            fencing_token=1,
            assignment_reason={"source": "result-input-scenario"},
        )
        execution_session = ExecutionSession.start(
            session_id=uuid4(),
            workspace_id=self.workspace_id,
            execution_id=execution_id,
            worker_id=worker.id,
            adapter_session_id=f"scenario:{key}",
            now=self.now,
        )
        with self.uow_factory() as uow:
            execution = uow.executions.get(
                execution_id, self.workspace_id, for_update=True
            )
            assert execution is not None
            previous_execution_version = execution.version
            worker.reserve()
            worker.mark_running()
            execution.assign(
                lease_id=lease.id,
                worker_id=worker.id,
                fencing_token=lease.fencing_token,
            )
            execution.start(
                lease_id=lease.id,
                fencing_token=lease.fencing_token,
                now=self.now,
            )
            uow.workers.add(worker)
            uow.flush()
            uow.leases.add(lease)
            uow.executions.save(execution, previous_execution_version)
            uow.sessions.add(execution_session)
            return worker.id, lease.id, execution_session.id, execution.version


def _proposal_mapping(
    *,
    groups: tuple[dict[str, object], ...] | None,
    hard_edges: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    raw = proposal_v5_mapping()
    worker_items = raw["worker_items"]
    intents = raw["execution_intents"]
    assert isinstance(worker_items, list)
    assert isinstance(intents, list)
    build_worker = next(item for item in worker_items if item["key"] == "build-worker")
    summary_worker = next(
        item for item in worker_items if item["key"] == "review-worker"
    )
    build_intent = next(
        item for item in intents if item["worker_item_key"] == "build-worker"
    )
    summary_intent = next(
        item for item in intents if item["worker_item_key"] == "review-worker"
    )
    build_worker["key"] = "a"
    build_worker["title"] = "Worker A"
    build_intent["worker_item_key"] = "a"
    summary_worker["key"] = "summary"
    summary_worker["title"] = "Summary worker"
    summary_intent["worker_item_key"] = "summary"
    for key in ("b", "c"):
        worker = deepcopy(build_worker)
        worker["key"] = key
        worker["title"] = f"Worker {key.upper()}"
        worker_items.append(worker)
        intent = deepcopy(build_intent)
        intent["worker_item_key"] = key
        intents.append(intent)
    summary_intent["activation_dependencies"] = [
        {
            "upstream_worker_item_key": upstream,
            "downstream_worker_item_key": downstream,
            "kind": "hard",
            "required_artifact_refs": ["artifact_type:build"],
        }
        for upstream, downstream in hard_edges
    ]
    default_groups = (
        {
            "group_key": "research",
            "title": "Research results",
            "consumer_worker_item_key": "summary",
            "candidates": [
                {"worker_item_key": "a", "priority": 1},
                {"worker_item_key": "b", "priority": 2},
                {"worker_item_key": "c", "priority": 3},
            ],
            "requirements": {
                "execution_status": "succeeded",
                "required_artifact_refs": ["artifact_type:build"],
                "required_evidence_refs": ["acceptance:build"],
                "evidence_status": "candidate",
            },
            "mode": "at_least",
            "minimum_count": 2,
            "selection_strategy": "plan_order",
        },
    )
    policy = raw["collaboration_policy_request"]
    assert isinstance(policy, dict)
    selected_groups = default_groups if groups is None else groups
    policy["input_groups"] = [deepcopy(item) for item in selected_groups]
    return raw


@pytest.fixture
def scenario(
    database: Database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)
