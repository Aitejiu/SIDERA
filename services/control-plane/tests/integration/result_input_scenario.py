from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.application.activation_reconciler import ActivationReconciler
from sidera_control_plane.application.artifact_content import ArtifactContentService
from sidera_control_plane.application.collaboration_decisions import (
    CollaborationDecisionApplicationResult,
    CollaborationDecisionService,
)
from sidera_control_plane.application.collaboration_governance_commands import (
    CollaborationGovernanceCommandHandler,
)
from sidera_control_plane.application.collaboration_governance_release import (
    CollaborationGovernanceReleaseApplier,
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
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.event_ingestor import ArtifactRegistry
from sidera_control_plane.application.execution_commands import ExecutionCommandHandler
from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
from sidera_control_plane.application.planning_commands import PlanningCommandHandler
from sidera_control_plane.application.ports import (
    ExactRevisionManifest,
    ExactSectionBody,
    ExactSectionRef,
    KnowledgeCatalogPort,
    SearchHit,
)
from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
)
from sidera_control_plane.application.worker_gateway import (
    ASSIGNMENT_TOPIC,
    WorkerGateway,
)
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.context_package_worker import ContextPackageWorker
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
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextMaterialization,
    ContextMaterializationV1,
    ContextMaterializationV2,
    ContextPackageManifestV2,
    ContextPurpose,
)
from sidera_control_plane.domain.execution import ExecutionSession
from sidera_control_plane.domain.gateway import AdapterSessionHandle
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    CollaborationRuntimeCapabilityProfile,
    HandoffCheckpointMode,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
    WorkspaceProviderCapabilityProfile,
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
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration
from sidera_control_plane.domain.workspace import (
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
)
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    CollaborationInputSelectionModel,
    CollaborationReleaseProposalModel,
    ExecutionModel,
    ExecutionRequestModel,
    OutboxMessageModel,
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


class _EmptyCatalog(KnowledgeCatalogPort):
    def search(
        self,
        workspace_id: UUID,
        query: str,
        *,
        tags: Sequence[str] = (),
        source_types: Sequence[str] = (),
        status: str | None = "active",
        limit: int = 20,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[SearchHit, ...]:
        del workspace_id, query, tags, source_types, status, limit
        del correlation_id, caller_request_id
        return ()

    def get_revision_manifest(
        self,
        workspace_id: UUID,
        page_id: UUID,
        revision_id: UUID,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> ExactRevisionManifest:
        del workspace_id, page_id, revision_id, correlation_id, caller_request_id
        raise AssertionError("result-input scenario has no Knowledge refs")

    def materialize(
        self,
        workspace_id: UUID,
        sections: Sequence[ExactSectionRef],
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[ExactSectionBody, ...]:
        del workspace_id, sections, correlation_id, caller_request_id
        raise AssertionError("result-input scenario has no Knowledge refs")


_CAPTURE_ADAPTER_CAPABILITIES = WorkerAdapterCapabilityProfile(
    input=CapabilityInput.QUEUED,
    idempotency=IdempotencyCapability.NONE,
    resume=ResumeCapability.SAME_PROCESS,
    stop=StopCapability.EXACT_EXECUTION,
    sandbox=SandboxCapability.FILESYSTEM,
    stability=StabilityLevel.STABLE,
    context_package_versions=(1, 2),
    context_patch_versions=(1,),
    handoff_checkpoint_mode=HandoffCheckpointMode.NATIVE,
    handoff_checkpoint_versions=(1,),
    handoff_session_context_versions=(1,),
    handoff_writer_gate_versions=(1,),
    handoff_inspection=True,
)


class _CaptureWorkerAdapter(FakeWorkerAdapter):
    _CAPABILITIES = _CAPTURE_ADAPTER_CAPABILITIES

    def __init__(self) -> None:
        super().__init__()
        self.accepted_contexts: list[ContextMaterialization] = []
        self.start_calls: list[ExecutionSpec] = []

    def accept_context(
        self,
        materialization: ContextMaterialization,
        *,
        operation_id: UUID,
    ) -> ContextAcknowledgement:
        if not isinstance(materialization, (ContextMaterializationV1, ContextMaterializationV2)):
            raise TypeError("unsupported context materialization")
        self.accepted_contexts.append(materialization)
        metadata = materialization.metadata
        return ContextAcknowledgement(
            package_id=metadata.package_id,
            package_hash=metadata.package_hash,
            consumer_type=metadata.consumer_type,
            consumer_id=metadata.consumer_id,
            context_version=metadata.context_version,
            adapter_module_id=uuid4(),
            adapter_config_version=1,
            acknowledged_at=datetime.now(UTC),
            schema_version=materialization.schema_version,
            operation_id=operation_id,
        )

    def start_session(
        self,
        spec: ExecutionSpec,
        workspace: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
    ) -> AdapterSessionHandle:
        self.start_calls.append(spec)
        return super().start_session(spec, workspace, operation_id=operation_id)


class _CaptureWorkspaceProvider:
    def prepare(
        self,
        spec: ExecutionSpec,
        *,
        operation_id: UUID,
    ) -> ExecutionWorkspaceManifest:
        del operation_id
        return ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=spec.workspace_id,
            task_id=spec.task_id,
            execution_id=spec.execution_id,
            checkouts=(
                WorkspaceCheckout(
                    repository_ref="repo://sidera",
                    base_revision="a" * 40,
                    branch=f"sidera/execution/{spec.execution_id}",
                    current_head="a" * 40,
                ),
            ),
            location_ref=f"capture-workspace://{spec.execution_id}",
            retention_seconds=3600,
            fencing_token=spec.fencing_token,
            created_at=FROZEN_NOW,
        )

    def freeze(
        self, manifest: ExecutionWorkspaceManifest, *, operation_id: UUID
    ) -> ExecutionWorkspaceManifest:
        del operation_id
        return manifest

    def retain(
        self, manifest: ExecutionWorkspaceManifest, *, operation_id: UUID
    ) -> ExecutionWorkspaceManifest:
        del operation_id
        return manifest

    def release(
        self, manifest: ExecutionWorkspaceManifest, *, operation_id: UUID
    ) -> ExecutionWorkspaceManifest:
        del operation_id
        return manifest

    def inspect(self, location_ref: str) -> object:
        raise RuntimeError(f"not implemented for {location_ref}")


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
        self._artifact_root = tmp_path / "result-input-artifacts"
        self._source_storage_refs: dict[str, str] = {}
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
        self._capture_adapter: _CaptureWorkerAdapter | None = None
        self._gateway: WorkerGateway | None = None
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
        self._source_storage_refs[key] = storage_ref
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

    def remove_source_body(self, key: str) -> None:
        storage_ref = self._source_storage_refs[key]
        digest = storage_ref.rsplit("/", 1)[-1]
        path = self._artifact_root / str(self.workspace_id) / digest[:2] / digest
        path.unlink()

    def reconcile(self) -> None:
        for _ in range(10):
            if not self._reconcile_once():
                return
        raise AssertionError("collaboration reconciliation did not converge")

    def activate(self) -> None:
        ActivationReconciler(
            uow_factory=self.uow_factory,
            owner_id="result-input-activation",
            clock=self.clock,
        ).run_once(limit=20)

    def assemble_context(self, key: str) -> ContextPackageManifestV2:
        execution_id = self._execution_id(key)
        assert execution_id is not None
        with self.uow_factory() as uow:
            execution = uow.executions.get(execution_id, self.workspace_id)
            assert execution is not None
            release = uow.collaboration_ledger.get_release_for_request(
                self.workspace_id,
                self.request_ids[key],
            )
            assert release is not None
            assembly = uow.context_assemblies.get_for_consumer(
                ContextPurpose.EXECUTION,
                execution_id,
                self.workspace_id,
                manifest_schema_version=2,
            )
            assert assembly is not None

        with self.uow_factory() as uow:
            existing = uow.context_packages.get_for_consumer(
                ContextPurpose.EXECUTION,
                execution_id,
                self.workspace_id,
                manifest_schema_version=2,
            )
        if existing is None:
            result = ContextPackageWorker(
                uow_factory=self.uow_factory,
                catalog=_EmptyCatalog(),
                owner_id=f"result-input-context-{key}",
                lease_seconds=60,
                clock=self.clock,
            ).run_once(limit=20)
            assert result.ready >= 1
            assert result.failed == 0
            assert result.blocked == 0

        with self.uow_factory() as uow:
            package = uow.context_packages.get_for_consumer(
                ContextPurpose.EXECUTION,
                execution_id,
                self.workspace_id,
                manifest_schema_version=2,
            )
        assert isinstance(package, ContextPackageManifestV2)
        return package

    def start_worker(self, key: str) -> object:
        execution_id = self._execution_id(key)
        assert execution_id is not None
        adapter = _CaptureWorkerAdapter()
        workspace_provider = _CaptureWorkspaceProvider()
        self._install_capture_runtime(execution_id, adapter)

        scheduled = Scheduler(
            self.uow_factory,
            LowestCostWorkerPolicy(),
            self.clock,
            context_enforcement=True,
        ).run_once(limit=20)
        assert scheduled.assigned == 1
        message = self._assignment_message(execution_id)
        gateway = WorkerGateway(
            self.uow_factory,
            adapter_factories={"capture": lambda _instance: adapter},
            workspace_factories={
                "capture-workspace": lambda _instance: workspace_provider
            },
            artifact_content_store=self.artifact_store,
            context_materializer=ContextMaterializer(
                _EmptyCatalog(),
                result_input_content=ArtifactContentService(
                    self.database,
                    self.artifact_store,
                ),
            ),
            clock=self.clock,
        )
        self._capture_adapter = adapter
        self._gateway = gateway
        return gateway.handle(message)

    def captured_context(self, key: str) -> ContextMaterialization:
        del key
        if self._capture_adapter is None or not self._capture_adapter.accepted_contexts:
            raise AssertionError("worker context was not accepted")
        return self._capture_adapter.accepted_contexts[-1]

    def start_call_count(self, key: str) -> int:
        del key
        return len(self._capture_adapter.start_calls) if self._capture_adapter else 0

    def _install_capture_runtime(
        self, execution_id: UUID, adapter: _CaptureWorkerAdapter
    ) -> None:
        workspace_capabilities = WorkspaceProviderCapabilityProfile(
            isolation=SandboxCapability.FILESYSTEM,
            writable=True,
            git_worktree=True,
            stability=StabilityLevel.STABLE,
        )
        with self.uow_factory() as uow:
            execution = uow.executions.get(execution_id, self.workspace_id)
            assert execution is not None
            worker = Worker.register(
                worker_id=uuid4(),
                workspace_id=self.workspace_id,
                adapter_type="capture",
                display_name="Result input capture worker",
                capabilities=frozenset(execution.capability_requirements),
                capacity=1,
                cost_per_hour_microunits=0,
                startup_latency_ms=0,
                now=self.now,
            )
            adapter_definition = ModuleDefinition(
                id=uuid4(),
                workspace_id=self.workspace_id,
                module_type="worker_adapter",
                provider="capture",
                interface_version="sidera.worker-adapter.v2",
                config_schema_version=1,
                capabilities=adapter._CAPABILITIES,
                lifecycle=ModuleLifecycle.ACTIVE,
            )
            workspace_definition = ModuleDefinition(
                id=uuid4(),
                workspace_id=self.workspace_id,
                module_type="workspace_provider",
                provider="capture-workspace",
                interface_version="sidera.workspace-provider.v1",
                config_schema_version=1,
                capabilities=workspace_capabilities,
                lifecycle=ModuleLifecycle.ACTIVE,
            )
            adapter_instance = ModuleInstance(
                id=uuid4(),
                workspace_id=self.workspace_id,
                definition_id=adapter_definition.id,
                instance_name="result-input-capture-adapter",
                config_version=1,
                config_namespace="adapter",
                config={"adapter.mode": "capture"},
                secret_refs=(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
            workspace_instance = ModuleInstance(
                id=uuid4(),
                workspace_id=self.workspace_id,
                definition_id=workspace_definition.id,
                instance_name="result-input-capture-workspace",
                config_version=1,
                config_namespace="workspace",
                config={
                    "workspace.repository_ref": "repo://sidera",
                    "workspace.base_revision": "a" * 40,
                },
                secret_refs=(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
            uow.workers.add(worker)
            uow.modules.add_definition(adapter_definition)
            uow.modules.add_definition(workspace_definition)
            uow.modules.add_instance(adapter_instance)
            uow.modules.add_instance(workspace_instance)
            uow.worker_runtime_configurations.add(
                WorkerRuntimeConfiguration.create(
                    worker_id=worker.id,
                    workspace_id=self.workspace_id,
                    adapter_module_instance_id=adapter_instance.id,
                    adapter_module_config_version=1,
                    workspace_module_instance_id=workspace_instance.id,
                    workspace_module_config_version=1,
                )
            )

    def _assignment_message(self, execution_id: UUID) -> OutboxMessage:
        with self.database.session() as session:
            rows = session.scalars(
                select(OutboxMessageModel).where(
                    OutboxMessageModel.topic == ASSIGNMENT_TOPIC
                )
            ).all()
        row = next(
            (
                item
                for item in rows
                if item.payload.get("aggregate_id") == str(execution_id)
            ),
            None,
        )
        assert row is not None
        return OutboxMessage(
            id=row.id,
            event_id=row.event_id,
            topic=row.topic,
            payload=dict(row.payload),
            attempts=max(int(row.attempts), 1),
            claim_token=uuid4(),
        )

    def package_ids(self, key: str) -> tuple[UUID, ...]:
        execution_id = self._execution_id(key)
        assert execution_id is not None
        values: list[UUID] = []
        with self.uow_factory() as uow:
            for version in (1, 2):
                package = uow.context_packages.get_for_consumer(
                    ContextPurpose.EXECUTION,
                    execution_id,
                    self.workspace_id,
                    manifest_schema_version=version,
                )
                if package is not None:
                    values.append(package.package_id)
        return tuple(values)

    def execution_count(self, key: str) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(ExecutionModel)
                    .where(
                        ExecutionModel.workspace_id == self.workspace_id,
                        ExecutionModel.worker_item_id == self.worker_item_ids[key],
                    )
                )
                or 0
            )

    def request_state(self, key: str) -> str:
        with self.database.session() as session:
            value = session.scalar(
                select(ExecutionRequestModel.status).where(
                    ExecutionRequestModel.workspace_id == self.workspace_id,
                    ExecutionRequestModel.id == self.request_ids[key],
                )
            )
        assert value is not None
        return str(value)

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

    def pause_release(self, key: str) -> None:
        # Confirmation creates the Run before its first runtime reconciliation.
        # Pause exercises the public command only after that Run is current.
        self._reconcile_once()
        with self.uow_factory() as uow:
            worker_item = uow.worker_items.get(
                self.worker_item_ids[key], self.workspace_id
            )
            assert worker_item is not None
            worker_version = worker_item.version
        CommandService(
            self.uow_factory,
            (
                CollaborationGovernanceCommandHandler(
                    clock=self.clock,
                    local_actor_type="local",
                    local_actor_id="operator",
                ),
            ),
        ).handle(
            CommandEnvelope(
                id=uuid4(),
                workspace_id=self.workspace_id,
                command_type="PauseCollaborationRelease",
                target_type="worker_item",
                actor_type="local",
                actor_id="operator",
                target_id=self.worker_item_ids[key],
                payload={
                    "transport": "web",
                    "reason": "review fixed result inputs",
                    "target_plan_generation": 1,
                },
                expected_version=worker_version,
                idempotency_key=f"result-input-pause:{self.worker_item_ids[key]}",
                correlation_id=self.ids["root_task"],
            )
        )

    def pending_proposal(self, key: str):
        with self.database.session() as session:
            proposal_id = session.scalar(
                select(CollaborationReleaseProposalModel.id).where(
                    CollaborationReleaseProposalModel.workspace_id == self.workspace_id,
                    CollaborationReleaseProposalModel.execution_request_id
                    == self.request_ids[key],
                    CollaborationReleaseProposalModel.status == "pending",
                )
            )
        assert proposal_id is not None
        with self.uow_factory() as uow:
            proposal = uow.collaboration_governance.get_proposal(
                self.workspace_id, proposal_id
            )
            assert proposal is not None
            return proposal

    def governance_service(self) -> CommandService:
        applier = CollaborationGovernanceReleaseApplier(
            fact_store_factory=lambda uow: SqlAlchemyCollaborationFactStore(
                session=cast(SqlAlchemyUnitOfWork, uow).session
            )
        )
        return CommandService(
            self.uow_factory,
            (
                CollaborationGovernanceCommandHandler(
                    clock=self.clock,
                    local_actor_type="local",
                    local_actor_id="operator",
                    approver=applier,
                ),
            ),
        )

    def approval_command(
        self,
        proposal: Any,
        *,
        include_selection: bool = True,
        selection_hash: str | None = None,
    ) -> CommandEnvelope:
        payload: dict[str, object] = {
            "transport": "web",
            "reason": "reviewed fixed result inputs",
        }
        if include_selection and proposal.input_selection_id is not None:
            payload.update(
                {
                    "expected_input_selection_id": str(proposal.input_selection_id),
                    "expected_input_selection_hash": (
                        selection_hash or proposal.input_selection_hash
                    ),
                }
            )
        return CommandEnvelope(
            id=uuid4(),
            workspace_id=self.workspace_id,
            command_type="ApproveCollaborationReleaseProposal",
            target_type="collaboration_release_proposal",
            actor_type="local",
            actor_id="operator",
            target_id=proposal.id,
            payload=payload,
            expected_version=proposal.resource_version,
            idempotency_key=f"result-input-approve:{proposal.id}:{uuid4()}",
            correlation_id=self.ids["root_task"],
        )

    def approve(self, proposal: Any) -> Any:
        return self.governance_service().handle(self.approval_command(proposal))

    def rejection_command(self, proposal: Any) -> CommandEnvelope:
        return CommandEnvelope(
            id=uuid4(),
            workspace_id=self.workspace_id,
            command_type="RejectCollaborationReleaseProposal",
            target_type="collaboration_release_proposal",
            actor_type="local",
            actor_id="operator",
            target_id=proposal.id,
            payload={"transport": "web", "reason": "needs revision"},
            expected_version=proposal.resource_version,
            idempotency_key=f"result-input-reject:{proposal.id}:{uuid4()}",
            correlation_id=self.ids["root_task"],
        )

    def resume_command(self, proposal: Any) -> CommandEnvelope:
        with self.uow_factory() as uow:
            hold = uow.collaboration_governance.get_hold(
                self.workspace_id, proposal.hold_id
            )
            assert hold is not None
        return CommandEnvelope(
            id=uuid4(),
            workspace_id=self.workspace_id,
            command_type="ResumeAutomaticCollaboration",
            target_type="collaboration_governance_hold",
            actor_type="local",
            actor_id="operator",
            target_id=hold.id,
            payload={"transport": "web", "reason": "resume automatic release"},
            expected_version=hold.resource_version,
            idempotency_key=f"result-input-resume:{hold.id}:{uuid4()}",
            correlation_id=self.ids["root_task"],
        )

    def release_selection_ref(self, key: str) -> tuple[UUID | None, str | None]:
        with self.database.session() as session:
            release = session.scalar(
                select(RuntimeReleaseModel).where(
                    RuntimeReleaseModel.workspace_id == self.workspace_id,
                    RuntimeReleaseModel.execution_request_id == self.request_ids[key],
                )
            )
        assert release is not None
        return release.input_selection_id, release.input_selection_hash

    def request_version(self, key: str) -> int:
        with self.database.session() as session:
            value = session.scalar(
                select(ExecutionRequestModel.version).where(
                    ExecutionRequestModel.workspace_id == self.workspace_id,
                    ExecutionRequestModel.id == self.request_ids[key],
                )
            )
        assert value is not None
        return int(value)

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
        self._reconcile_once()
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
