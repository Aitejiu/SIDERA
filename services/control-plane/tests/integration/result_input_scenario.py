from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.api.app import create_app
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
from sidera_control_plane.application.context_package_queries import (
    ContextPackageCommandHandler,
)
from sidera_control_plane.application.event_ingestor import ArtifactRegistry
from sidera_control_plane.application.execution_commands import (
    ExecutionCommandHandler,
    ExecutionInputCommandHandler,
)
from sidera_control_plane.application.handoff_candidates import (
    HandoffCandidateService,
)
from sidera_control_plane.application.handoff_commands import HandoffCommandHandler
from sidera_control_plane.application.handoff_context import HandoffContextService
from sidera_control_plane.application.handoff_coordinator import HandoffCoordinator
from sidera_control_plane.application.handoff_input_transfer import (
    HandoffInputTransferService,
)
from sidera_control_plane.application.handoff_target import HandoffTargetService
from sidera_control_plane.application.notification_policy import (
    NotificationPolicyV1,
    NotificationPolicyV2,
)
from sidera_control_plane.application.notification_reconciler import (
    NotificationReconciler,
)
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
    GatewayMessageError,
    WorkerGateway,
)
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.application.worker_runtime import WorkerRuntimeResolver
from sidera_control_plane.application.workspace_handoff import WorkspaceHandoffService
from sidera_control_plane.config import Settings
from sidera_control_plane.context_package_worker import ContextPackageWorker
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.domain.collaboration import (
    CollaborationRuntimeBindingSnapshot,
    resolve_collaboration_policy_v2,
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
from sidera_control_plane.domain.execution import ExecutionSession, ExecutionStatus
from sidera_control_plane.domain.gateway import AdapterSessionHandle
from sidera_control_plane.domain.handoff_evidence import (
    HandoffInspectionOutcome,
    HandoffWorkspaceInspection,
    HandoffWorkspaceTargetSpec,
)
from sidera_control_plane.domain.handoffs import (
    HandoffTransition,
    HandoffTransitionStatus,
)
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    CollaborationRuntimeCapabilityProfile,
    HandoffCheckpointMode,
    HandoffTransferMode,
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
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.planning import PlanningRequest, PlanningRequestKind
from sidera_control_plane.domain.planning_proposals import (
    PlanningProfileDescriptor,
    PlanningProposalPayloadV5,
    ProposalRevision,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV5,
)
from sidera_control_plane.domain.revision_diff import RevisionDiffNormalizer
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
    WorkspaceLifecycle,
)
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    CollaborationInputSelectionModel,
    CollaborationReleaseProposalModel,
    CommandModel,
    ContextAssemblyRequestModel,
    ExecutionModel,
    ExecutionRequestModel,
    NotificationFactModel,
    OutboxMessageModel,
    RuntimeReleaseModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.integration.test_proposal_confirmation import planner_capabilities, profile
from tests.unit.result_input_fixtures import proposal_v5_mapping

FROZEN_NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
_RESULT_INPUT_GATEWAY_CODES = frozenset(
    {
        "result_input_unavailable",
        "result_input_integrity_failed",
        "result_input_format_unsupported",
        "result_input_budget_exceeded",
    }
)


@dataclass(frozen=True, slots=True)
class PendingResultInputSeed:
    """The durable identifiers needed to resume a pending V5 confirmation."""

    workspace_id: UUID
    planning_request_id: UUID
    proposal_id: UUID
    proposal_revision: int
    proposal_content_hash: str
    planning_profile_id: UUID
    planning_profile_version: int
    expected_request_version: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "workspace_id": str(self.workspace_id),
            "planning_request_id": str(self.planning_request_id),
            "proposal_id": str(self.proposal_id),
            "proposal_revision": self.proposal_revision,
            "proposal_content_hash": self.proposal_content_hash,
            "planning_profile_id": str(self.planning_profile_id),
            "planning_profile_version": self.planning_profile_version,
            "expected_request_version": self.expected_request_version,
        }


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

    def __init__(
        self,
        capabilities: WorkerAdapterCapabilityProfile = _CAPTURE_ADAPTER_CAPABILITIES,
        *,
        raise_after_start: bool = False,
    ) -> None:
        super().__init__()
        self._CAPABILITIES = capabilities
        self._raise_after_start = raise_after_start
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
        handle = super().start_session(spec, workspace, operation_id=operation_id)
        if self._raise_after_start:
            raise RuntimeError("unknown start outcome")
        context = spec.context_package
        if context is not None:
            metadata = context.metadata
            with self._lock:
                session = self._sessions[handle.adapter_session_id]
                session.handoff_context = (
                    metadata.package_id,
                    metadata.package_hash,
                    metadata.context_version or 1,
                )
        return handle


class _CaptureWorkspaceProvider:
    def __init__(self) -> None:
        self._manifests_by_location: dict[str, ExecutionWorkspaceManifest] = {}
        self._handoff_transfers: dict[
            UUID, tuple[UUID, ExecutionWorkspaceManifest]
        ] = {}

    def prepare(
        self,
        spec: ExecutionSpec,
        *,
        operation_id: UUID,
    ) -> ExecutionWorkspaceManifest:
        del operation_id
        manifest = ExecutionWorkspaceManifest.create(
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
        self._manifests_by_location[manifest.location_ref] = manifest
        return manifest

    def freeze(
        self, manifest: ExecutionWorkspaceManifest, *, operation_id: UUID
    ) -> ExecutionWorkspaceManifest:
        del operation_id
        current = self._manifests_by_location.get(manifest.location_ref, manifest)
        if current.lifecycle is WorkspaceLifecycle.ACTIVE:
            current.freeze(now=FROZEN_NOW)
        self._manifests_by_location[current.location_ref] = current
        return current

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
        manifest = self._manifests_by_location.get(location_ref)
        if manifest is None:
            raise RuntimeError(f"unknown capture workspace {location_ref}")
        return manifest

    def transfer_handoff(
        self,
        source_manifest: ExecutionWorkspaceManifest,
        target: object,
        *,
        transition_id: UUID,
        logical_attempt: int,
        operation_id: UUID,
    ) -> ExecutionWorkspaceManifest:
        del logical_attempt
        if not isinstance(target, HandoffWorkspaceTargetSpec):
            raise TypeError("capture workspace target must be typed")
        target_manifest = ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=target.workspace_id,
            task_id=target.task_id,
            execution_id=target.target_execution_id,
            checkouts=source_manifest.checkouts,
            location_ref=source_manifest.location_ref,
            retention_seconds=source_manifest.retention_seconds,
            fencing_token=target.anticipated_fencing_token,
            created_at=FROZEN_NOW,
        )
        target_manifest.mark_preparing()
        target_manifest.mark_ready()
        self._handoff_transfers[operation_id] = (source_manifest.id, target_manifest)
        return target_manifest

    def inspect_handoff_transfer(
        self,
        *,
        transition_id: UUID,
        logical_attempt: int,
        operation_id: UUID,
    ) -> HandoffWorkspaceInspection:
        transfer = self._handoff_transfers.get(operation_id)
        source_manifest_id = transfer[0] if transfer is not None else None
        target_manifest = transfer[1] if transfer is not None else None
        return HandoffWorkspaceInspection(
            transition_id=transition_id,
            logical_attempt=logical_attempt,
            operation_id=operation_id,
            outcome=(
                HandoffInspectionOutcome.SUCCEEDED
                if target_manifest is not None
                else HandoffInspectionOutcome.ABSENT
            ),
            source_manifest_id=source_manifest_id,
            target_manifest_id=(target_manifest.id if target_manifest is not None else None),
            source_lifecycle=WorkspaceLifecycle.FROZEN.value
            if source_manifest_id is not None
            else None,
            target_lifecycle=(
                target_manifest.lifecycle.value if target_manifest is not None else None
            ),
            observed_at=FROZEN_NOW,
            evidence_refs=("capture://handoff-transfer",),
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
        self._artifact_root = tmp_path / "result-input-artifacts"
        self._source_storage_refs: dict[str, str] = {}
        self._source_artifact_ids: dict[str, UUID] = {}
        self._unavailable_artifacts: dict[UUID, Path] = {}
        self._corrupted_artifact_bodies: dict[UUID, bytes] = {}
        self._capture_workspace_provider = _CaptureWorkspaceProvider()
        self._capture_workspace_instance: ModuleInstance | None = None
        self._capture_adapters_by_instance: dict[UUID, _CaptureWorkerAdapter] = {}
        self._capture_workers_by_adapter_instance: dict[UUID, UUID] = {}
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
        self._pending_confirmation: PendingResultInputSeed | None = None
        self._capture_adapter: _CaptureWorkerAdapter | None = None
        self._capture_adapters_by_key: dict[str, list[_CaptureWorkerAdapter]] = {}
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
        max_attempts: int = 1,
        result_inputs_enabled: bool = True,
        prepare_only: bool = False,
        browser_state: bool = False,
    ) -> PendingResultInputSeed | None:
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
            _proposal_mapping(
                groups=groups,
                hard_edges=hard_edges,
                max_attempts=max_attempts,
                browser_state=browser_state,
            )
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
            pending = PendingResultInputSeed(
                workspace_id=self.workspace_id,
                planning_request_id=stored_request.id,
                proposal_id=proposal.id,
                proposal_revision=proposal.revision,
                proposal_content_hash=proposal.content_hash,
                planning_profile_id=stored_profile.id,
                planning_profile_version=stored_profile.version,
                expected_request_version=stored_request.version,
            )

        self._pending_confirmation = pending
        if prepare_only:
            return pending
        self.confirm_prepared_plan(result_inputs_enabled=result_inputs_enabled)
        return None

    def prepare_plan(
        self,
        *,
        groups: tuple[dict[str, object], ...] | None = None,
        hard_edges: tuple[tuple[str, str], ...] = (),
        max_attempts: int = 1,
        browser_state: bool = False,
    ) -> PendingResultInputSeed:
        prepared = self.confirm_plan(
            groups=groups,
            hard_edges=hard_edges,
            max_attempts=max_attempts,
            browser_state=browser_state,
            prepare_only=True,
        )
        assert prepared is not None
        return prepared

    def confirm_prepared_plan(self, *, result_inputs_enabled: bool = True) -> None:
        pending = self._pending_confirmation
        if pending is None:
            raise AssertionError("result-input proposal has not been prepared")

        result = CommandService(
            self.uow_factory,
            (
                PlanningCommandHandler(
                    local_actor_type="local",
                    local_actor_id="operator",
                    clock=self.clock,
                    result_inputs_enabled=result_inputs_enabled,
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
                target_id=pending.planning_request_id,
                payload={
                    "proposal_revision": pending.proposal_revision,
                    "proposal_content_hash": pending.proposal_content_hash,
                    "profile_id": str(pending.planning_profile_id),
                    "profile_version": pending.planning_profile_version,
                },
                expected_version=pending.expected_request_version,
                idempotency_key=(
                    f"result-input-confirm:{pending.planning_request_id}"
                ),
                correlation_id=uuid4(),
            )
        )
        self.ids["planning_request"] = pending.planning_request_id
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

    def load_pending_confirmation(self, pending: PendingResultInputSeed) -> None:
        """Hydrate the pre-confirmation seed after a process restart."""
        if pending.workspace_id != self.workspace_id:
            raise ValueError("pending result-input seed belongs to another workspace")
        self._pending_confirmation = pending

    def load_materialized_plan(self, planning_request_id: UUID) -> None:
        """Rebuild the scenario's durable IDs after a service restart."""
        with self.uow_factory() as uow:
            materialization = uow.proposal_materializations.get_by_request(
                planning_request_id, self.workspace_id
            )
            if materialization is None:
                raise AssertionError("materialized planning request was not found")
            self.ids["planning_request"] = planning_request_id
            self.ids["root_task"] = materialization.root_task_id
            self.ids["plan_snapshot"] = materialization.plan_snapshot_id
            self.worker_item_ids = {
                key: UUID(str(value))
                for key, value in materialization.worker_item_mapping.items()
            }
            if not self.worker_item_ids:
                raise AssertionError("materialization has no WorkerItems")
            rows = uow.session.scalars(
                select(ExecutionRequestModel).where(
                    ExecutionRequestModel.workspace_id == self.workspace_id,
                    ExecutionRequestModel.worker_item_key.in_(
                        tuple(self.worker_item_ids)
                    ),
                )
            ).all()
            self.request_ids = {
                row.worker_item_key: row.id
                for row in rows
                if row.worker_item_key in self.worker_item_ids
            }
            for key in self.worker_item_ids:
                if key not in self.request_ids:
                    raise AssertionError(f"execution request {key!r} was not found")
                execution_id = uow.session.scalar(
                    select(ExecutionModel.id)
                    .where(
                        ExecutionModel.workspace_id == self.workspace_id,
                        ExecutionModel.worker_item_id == self.worker_item_ids[key],
                    )
                    .order_by(
                        ExecutionModel.created_at.desc(),
                        ExecutionModel.attempt.desc(),
                        ExecutionModel.id.desc(),
                    )
                    .limit(1)
                )
                if execution_id is None:
                    continue
                artifact_id = uow.session.scalar(
                    select(ArtifactModel.id)
                    .where(
                        ArtifactModel.workspace_id == self.workspace_id,
                        ArtifactModel.execution_id == execution_id,
                    )
                    .order_by(ArtifactModel.created_at.asc(), ArtifactModel.id.asc())
                    .limit(1)
                )
                if artifact_id is not None:
                    self._source_artifact_ids[key] = artifact_id

    def run_to_started_summary(self) -> None:
        """Drive the existing result-input path to one started consumer."""
        self.run_to_released_summary()
        self.activate()
        self.assemble_context("summary")
        self.complete_source("a", body=b"report-a\n")
        self.start_worker("summary")

    def run_to_released_summary(self) -> None:
        """Create the released summary Selection without consuming its package."""
        self.confirm_plan(max_attempts=2)
        self.complete_source("b", body=b"report-b\n")
        self.complete_source("c", body=b"report-c\n")
        self.reconcile()

    def revise_group(self, *, minimum_count: int) -> object:
        """Submit and confirm a real v5 Revision against the current plan head."""
        if type(minimum_count) is not int or minimum_count < 1:
            raise ValueError("minimum_count must be a positive integer")

        with self.uow_factory() as uow:
            root_task = uow.tasks.get(
                self.ids["root_task"], self.workspace_id, for_update=True
            )
            head = uow.plans.get_head(
                self.ids["root_task"], self.workspace_id, for_update=True
            )
            base_snapshot = uow.plans.get(head.active_snapshot_id, self.workspace_id)
            original_request = uow.planning_requests.get(
                self.ids["planning_request"], self.workspace_id
            )
            original_proposal = uow.planning_records.get_current_proposal(
                self.ids["planning_request"], self.workspace_id
            )
            assert root_task is not None
            assert head is not None
            assert base_snapshot is not None
            assert original_request is not None
            assert original_proposal is not None
            expected_root_version = root_task.version
            expected_profile_id = original_request.profile_snapshot.profile_id
            expected_profile_version = original_request.profile_snapshot.profile_version
            raw = deepcopy(original_proposal.payload.to_mapping())

        raw["kind"] = PlanningRequestKind.REVISION.value
        raw["base_descriptor"] = {
            "schema_version": 1,
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(base_snapshot.root_task_id),
            "plan_snapshot_id": str(base_snapshot.id),
            "plan_generation": base_snapshot.generation,
            "plan_content_hash": plan_snapshot_content_hash(base_snapshot),
            "root_task_version": expected_root_version,
            "captured_at": self.now.isoformat(),
        }
        raw["subtask_identity_decisions"] = [
            {
                "target_subtask_key": item.key,
                "strategy": "reuse",
                "existing_subtask_task_id": str(item.task_id),
                "derived_from_subtask_task_ids": [],
                "reason": "reuse stable Task identity",
            }
            for item in base_snapshot.subtasks
        ]
        raw["worker_item_identity_decisions"] = [
            {
                "target_worker_item_key": item.key,
                "strategy": "reuse",
                "existing_worker_item_id": str(item.worker_item_id),
                "derived_from_worker_item_ids": [],
                "target_subtask_key": item.subtask_key,
                "context_disposition": (
                    "patch_required" if item.key == "summary" else "unchanged"
                ),
                "change_reasons": (
                    ["result input quorum changed"] if item.key == "summary" else []
                ),
            }
            for item in base_snapshot.worker_item_plans
        ]
        raw["retired_subtask_ids"] = []
        raw["retired_worker_item_ids"] = []
        impact_policy = raw.get("impact_policy")
        assert isinstance(impact_policy, dict)
        impact_policy["revision_operation"] = "revise"
        policy = raw.get("collaboration_policy_request")
        assert isinstance(policy, dict)
        groups = policy.get("input_groups")
        assert isinstance(groups, list)
        group = next(item for item in groups if item["group_key"] == "research")
        group["minimum_count"] = minimum_count
        payload = PlanningRevisionProposalPayloadV5.from_mapping(raw)

        submit = CommandService(
            self.uow_factory,
            (
                PlanningCommandHandler(
                    local_actor_type="local",
                    local_actor_id="operator",
                    clock=self.clock,
                    result_inputs_enabled=True,
                ),
            ),
        ).handle(
            CommandEnvelope(
                id=uuid4(),
                workspace_id=self.workspace_id,
                command_type="SubmitPlanRevision",
                target_type="task",
                actor_type="local",
                actor_id="operator",
                target_id=self.ids["root_task"],
                payload={
                    "root_task_id": str(self.ids["root_task"]),
                    "expected_base_snapshot_id": str(base_snapshot.id),
                    "expected_base_generation": base_snapshot.generation,
                    "expected_base_content_hash": plan_snapshot_content_hash(
                        base_snapshot
                    ),
                    "objective": "Revise the result quorum without changing worker identities",
                    "constraints": ["Keep the running execution input immutable"],
                    "acceptance_criteria": ["The next execution receives the new quorum"],
                    "planning_profile_id": str(expected_profile_id),
                    "planning_profile_version": expected_profile_version,
                },
                expected_version=expected_root_version,
                idempotency_key=(
                    f"result-input-revision-submit:{self.ids['root_task']}"
                ),
                correlation_id=self.ids["root_task"],
            )
        )
        revision_request_id = submit.aggregate_id

        with self.uow_factory() as uow:
            revision_request = uow.planning_requests.get(
                revision_request_id, self.workspace_id, for_update=True
            )
            assert revision_request is not None
            run = revision_request.runs[0]
            previous_request_version = revision_request.version
            previous_run_version = run.version
            lease_id = uuid4()
            revision_request.start_planning(
                run_id=run.id,
                lease_id=lease_id,
                fencing_token=1,
                now=self.now,
            )
            revision_request.succeed_run(
                run_id=run.id,
                lease_id=lease_id,
                fencing_token=1,
                provider_ref="fake:result-input-revision",
                structured_result_type="planning.proposal.v5",
                usage=run.usage,
                now=self.now,
            )
            resolved_policy = resolve_collaboration_policy_v2(
                payload.collaboration_policy_request,
                revision_request.profile_snapshot.collaboration_runtime_binding,
                subtask_keys=frozenset(item.key for item in payload.subtasks),
            )
            diff = RevisionDiffNormalizer().normalize(
                base_snapshot,
                payload,
                collaboration_policy=resolved_policy,
            )
            proposal = ProposalRevision.create(
                planning_request_id=revision_request.id,
                revision=1,
                brief_revision=1,
                planning_run_id=run.id,
                kind=revision_request.kind,
                payload=payload,
                profile_descriptor=PlanningProfileDescriptor.from_snapshot(
                    revision_request.profile_snapshot
                ),
                profile_snapshot=revision_request.profile_snapshot,
                created_at=self.now,
                derived_diff=diff,
            )
            revision_request.mark_proposal_ready(
                run_id=run.id,
                proposal_revision=proposal.revision,
                content_hash=proposal.content_hash,
                now=self.now,
            )
            uow.planning_records.add_proposal(proposal, self.workspace_id)
            uow.planning_runs.save(run, previous_run_version)
            uow.planning_requests.save(revision_request, previous_request_version)
            expected_request_version = revision_request.version

        confirmed = CommandService(
            self.uow_factory,
            (
                PlanningCommandHandler(
                    local_actor_type="local",
                    local_actor_id="operator",
                    clock=self.clock,
                    result_inputs_enabled=True,
                ),
            ),
        ).handle(
            CommandEnvelope(
                id=uuid4(),
                workspace_id=self.workspace_id,
                command_type="ConfirmPlanRevision",
                target_type="planning_request",
                actor_type="local",
                actor_id="operator",
                target_id=revision_request_id,
                payload={
                    "expected_proposal_revision": proposal.revision,
                    "expected_proposal_content_hash": proposal.content_hash,
                    "expected_diff_hash": diff.diff_hash,
                    "expected_base_snapshot_id": str(base_snapshot.id),
                    "expected_base_content_hash": plan_snapshot_content_hash(
                        base_snapshot
                    ),
                    "profile_id": str(expected_profile_id),
                    "profile_version": expected_profile_version,
                },
                expected_version=expected_request_version,
                idempotency_key=(
                    f"result-input-revision-confirm:{revision_request_id}"
                ),
                correlation_id=self.ids["root_task"],
            )
        )
        self.ids["revision_request"] = revision_request_id
        return confirmed

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
        artifact_id = report.artifacts[0].id
        self._source_artifact_ids[key] = artifact_id
        return artifact_id

    def remove_source_body(self, key: str) -> None:
        self.make_artifact_unavailable(self._source_artifact_ids[key])

    def make_artifact_unavailable(self, artifact_id: UUID) -> None:
        path = self._artifact_path(artifact_id)
        if artifact_id in self._unavailable_artifacts:
            return
        if not path.exists():
            raise AssertionError("artifact body is already unavailable")
        backup = path.with_name(f".{path.name}.unavailable-{artifact_id}")
        path.replace(backup)
        self._unavailable_artifacts[artifact_id] = backup

    def block_selected_body(
        self,
        key: str = "summary",
        *,
        source_key: str = "b",
        failure_mode: str = "unavailable",
    ) -> object:
        """Drive the real Gateway path after making one selected body fail delivery."""

        self.activate()
        self.assemble_context(key)
        self.complete_source("a", body=b"report-a\n")
        artifact_id = self.source_artifact_id(source_key)
        if failure_mode == "unavailable":
            self.make_artifact_unavailable(artifact_id)
        elif failure_mode == "integrity":
            self.corrupt_artifact(artifact_id, body=b"tampered-result\n")
        else:
            raise ValueError("failure_mode must be unavailable or integrity")
        return self.start_worker(key)

    def source_artifact_id(self, key: str) -> UUID:
        artifact_id = self._source_artifact_ids.get(key)
        if artifact_id is None:
            raise AssertionError(f"source artifact {key!r} is unavailable")
        return artifact_id

    def restore_artifact(self, artifact_id: UUID) -> None:
        path = self._artifact_path(artifact_id)
        backup = self._unavailable_artifacts.pop(artifact_id, None)
        if backup is not None:
            if path.exists():
                raise AssertionError("artifact body was recreated before restore")
            backup.replace(path)
        original = self._corrupted_artifact_bodies.pop(artifact_id, None)
        if original is not None:
            path.write_bytes(original)

    def corrupt_artifact(self, artifact_id: UUID, *, body: bytes) -> None:
        if artifact_id in self._unavailable_artifacts:
            raise AssertionError("restore the unavailable artifact before corrupting it")
        path = self._artifact_path(artifact_id)
        if artifact_id not in self._corrupted_artifact_bodies:
            self._corrupted_artifact_bodies[artifact_id] = path.read_bytes()
        path.write_bytes(body)

    def _artifact_path(self, artifact_id: UUID) -> Path:
        with self.database.session() as session:
            artifact = session.scalar(
                select(ArtifactModel).where(
                    ArtifactModel.workspace_id == self.workspace_id,
                    ArtifactModel.id == artifact_id,
                )
            )
        assert artifact is not None
        storage_ref = artifact.storage_ref
        prefix = f"artifact-local://{self.workspace_id}/"
        if not storage_ref.startswith(prefix):
            raise AssertionError("scenario artifact does not use the local store")
        digest = artifact.content_hash.lower()
        if storage_ref.rsplit("/", 1)[-1].lower() != digest:
            raise AssertionError("artifact storage reference hash is stale")
        return self._artifact_root / str(self.workspace_id) / digest[:2] / digest

    def reconcile(self) -> None:
        for _ in range(10):
            if not self._reconcile_once():
                return
        raise AssertionError("collaboration reconciliation did not converge")

    def run_notifications(self, *, limit: int = 100) -> tuple[object, ...]:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        results = []
        policies = (
            (1, NotificationPolicyV1()),
            (2, NotificationPolicyV2()),
        )
        for policy_version, policy in policies:
            reconciler = NotificationReconciler(
                self.uow_factory,
                policy=policy,
                clock=self.clock,
                owner=f"result-input-notification-v{policy_version}",
                batch_size=limit,
            )
            for _ in range(10):
                result = reconciler.reconcile_workspace(
                    self.workspace_id,
                    limit=limit,
                )
                assert result.acquired, result
                assert result.diagnostic_code is None, result
                results.append(result)
                if result.processed_changes < limit:
                    break
            else:
                raise AssertionError("notification backlog did not converge")
        return tuple(results)

    def notification_facts(self, kind: str) -> tuple[NotificationFactModel, ...]:
        with self.database.session() as session:
            return tuple(
                session.scalars(
                    select(NotificationFactModel)
                    .where(
                        NotificationFactModel.workspace_id == self.workspace_id,
                        NotificationFactModel.kind == kind,
                    )
                    .order_by(
                        NotificationFactModel.created_at.asc(),
                        NotificationFactModel.id.asc(),
                    )
                ).all()
            )

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
            result = self.run_context_worker(key)
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

    def context_package_for_execution(self, execution_id: UUID) -> object:
        with self.uow_factory() as uow:
            for schema_version in (2, 1):
                package = uow.context_packages.get_for_consumer(
                    ContextPurpose.EXECUTION,
                    execution_id,
                    self.workspace_id,
                    manifest_schema_version=schema_version,
                )
                if package is not None:
                    return package
        raise AssertionError("execution context package was not found")

    def run_context_worker(self, key: str):
        return ContextPackageWorker(
            uow_factory=self.uow_factory,
            catalog=_EmptyCatalog(),
            owner_id=f"result-input-context-{key}",
            lease_seconds=60,
            clock=self.clock,
        ).run_once(limit=20)

    def retry_context(self, key: str) -> object:
        execution_id = self.latest_execution_id(key)
        with self.uow_factory() as uow:
            assembly = uow.context_assemblies.get_for_consumer(
                ContextPurpose.EXECUTION,
                execution_id,
                self.workspace_id,
                manifest_schema_version=2,
            )
            assert assembly is not None
        return CommandService(
            self.uow_factory,
            (
                ContextPackageCommandHandler(
                    local_actor_type="operator",
                    local_actor_id="result-input-scenario",
                    clock=self.clock,
                ),
            ),
        ).handle(
            CommandEnvelope(
                id=uuid5(
                    NAMESPACE_URL,
                    f"result-input-context-retry:{assembly.request_id}:v{assembly.version}",
                ),
                workspace_id=self.workspace_id,
                command_type="RetryContextAssembly",
                target_type="context_assembly",
                actor_type="operator",
                actor_id="result-input-scenario",
                target_id=assembly.request_id,
                payload={"request_fingerprint": assembly.request_fingerprint},
                expected_version=assembly.version,
                idempotency_key=(
                    f"result-input-context-retry:{assembly.request_id}:v{assembly.version}"
                ),
                correlation_id=self.ids["root_task"],
            )
        )

    def start_worker(
        self,
        key: str,
        *,
        start_outcome_unknown: bool = False,
        crash_after_context_ack: bool = False,
    ) -> object:
        execution_id = self._execution_id(key)
        assert execution_id is not None
        with self.uow_factory() as uow:
            execution = uow.executions.get(execution_id, self.workspace_id)
            binding = uow.bindings.get_for_execution(execution_id, self.workspace_id)
            assert execution is not None

        if execution.status is ExecutionStatus.QUEUED:
            adapter = _CaptureWorkerAdapter(
                raise_after_start=start_outcome_unknown,
            )
            self._install_capture_runtime(execution_id, adapter)
            scheduled = Scheduler(
                self.uow_factory,
                LowestCostWorkerPolicy(),
                self.clock,
                context_enforcement=True,
            ).run_once(
                limit=1,
                workspace_id=self.workspace_id,
                target_execution_id=execution_id,
            )
            assert scheduled.assigned == 1
            self._capture_adapters_by_key.setdefault(key, []).append(adapter)
        elif execution.status is ExecutionStatus.LEASED:
            if start_outcome_unknown:
                raise AssertionError("unknown start injection requires a queued execution")
            assert binding is not None
            adapter = self._capture_adapters_by_instance.get(binding.adapter_module_id)
            if adapter is None:
                adapter = _CaptureWorkerAdapter()
                self._capture_adapters_by_instance[binding.adapter_module_id] = adapter
                self._capture_adapters_by_key.setdefault(key, []).append(adapter)
        else:
            raise AssertionError(
                f"cannot start result-input execution in {execution.status.value} state"
            )

        message = self._assignment_message(execution_id)
        self._capture_adapter = adapter
        gateway = self._capture_gateway()
        self._gateway = gateway
        if crash_after_context_ack:

            def crash_after_ack(_context: object, _spec: ExecutionSpec) -> object:
                raise RuntimeError("after context acknowledgement")

            gateway._ensure_workspace = crash_after_ack  # type: ignore[method-assign]
        try:
            return gateway.handle(message)
        except GatewayMessageError as exc:
            if exc.code in _RESULT_INPUT_GATEWAY_CODES:
                return exc
            raise

    def handoff_worker(
        self,
        key: str,
        *,
        target_context_versions: tuple[int, ...],
        workspace_mode: str,
    ) -> HandoffTransition:
        """Prepare, confirm, and drive one real WorkerItem handoff."""

        if not workspace_mode.strip():
            raise ValueError("workspace_mode is required")
        source_execution_id = self.latest_execution_id(key)
        source_workspace_instance = self._capture_workspace_instance
        if source_workspace_instance is None:
            raise AssertionError("source capture workspace runtime is not installed")
        source_adapters = self._capture_adapters_by_key.get(key, ())
        if not source_adapters:
            raise AssertionError("source capture adapter runtime is not installed")
        for handle in source_adapters[0].active_sessions():
            source_adapters[0].release_boundary(handle)

        target_adapter = _CaptureWorkerAdapter(
            capabilities=replace(
                _CAPTURE_ADAPTER_CAPABILITIES,
                context_package_versions=tuple(target_context_versions),
            )
        )
        target_worker_id, _adapter_instance_id, _workspace_instance_id = (
            self._install_capture_runtime(
                source_execution_id,
                target_adapter,
                workspace_mode=workspace_mode,
                reuse_workspace_instance=source_workspace_instance,
            )
        )
        self._capture_adapter = target_adapter
        self._capture_adapters_by_key.setdefault(key, []).append(target_adapter)

        runtime_resolver = WorkerRuntimeResolver()
        candidates = HandoffCandidateService(
            self.uow_factory,
            runtime_resolver=runtime_resolver,
        )
        handoff_commands = CommandService(
            self.uow_factory,
            (
                HandoffCommandHandler(
                    self.clock,
                    candidate_service=candidates,
                    runtime_resolver=runtime_resolver,
                ),
            ),
        )
        with self.uow_factory() as uow:
            worker_item = uow.worker_items.get(
                self.worker_item_ids[key], self.workspace_id
            )
            assert worker_item is not None
            worker_item_version = worker_item.version
        prepare = handoff_commands.handle(
            CommandEnvelope(
                id=uuid4(),
                workspace_id=self.workspace_id,
                command_type="PrepareWorkerHandoff",
                target_type="worker_item",
                actor_type="operator",
                actor_id="operator",
                target_id=self.worker_item_ids[key],
                expected_version=worker_item_version,
                payload={
                    "target_worker_id": str(target_worker_id),
                    "handoff_reason": "exercise result-input handoff reuse",
                    "handoff_instruction": "continue from the fixed result inputs",
                    "input_plan": {},
                    "proposal_ttl_seconds": 900,
                },
                idempotency_key=(
                    f"result-input-handoff-prepare:{self.worker_item_ids[key]}"
                ),
                correlation_id=self.ids["root_task"],
            )
        )
        proposal = prepare.data.get("proposal")
        assert isinstance(proposal, dict)
        proposal_id = UUID(str(proposal["id"]))
        proposal_version = int(proposal["version"])
        proposal_hash = str(proposal["proposal_hash"])
        confirm = handoff_commands.handle(
            CommandEnvelope(
                id=uuid4(),
                workspace_id=self.workspace_id,
                command_type="ConfirmWorkerHandoff",
                target_type="handoff_proposal",
                actor_type="operator",
                actor_id="operator",
                target_id=proposal_id,
                expected_version=proposal_version,
                payload={
                    "proposal_id": str(proposal_id),
                    "expected_proposal_version": proposal_version,
                    "expected_proposal_hash": proposal_hash,
                    "expected_worker_item_version": worker_item_version,
                },
                idempotency_key=(
                    f"result-input-handoff-confirm:{self.worker_item_ids[key]}"
                ),
                correlation_id=self.ids["root_task"],
            )
        )
        transition_id = confirm.aggregate_id
        workspace_handoff = WorkspaceHandoffService(
            self._capture_workspace_provider,
            uow_factory=self.uow_factory,
        )
        context_service = HandoffContextService(self.uow_factory)
        coordinator = HandoffCoordinator(
            uow_factory=self.uow_factory,
            gateway=self._capture_gateway(),
            workspace_handoff=workspace_handoff,
            target_service=HandoffTargetService(self.uow_factory),
            context_service=context_service,
            input_transfer=HandoffInputTransferService(self.uow_factory),
            scheduler=Scheduler(
                self.uow_factory,
                LowestCostWorkerPolicy(),
                self.clock,
                context_enforcement=True,
            ),
            clock=self.clock,
            owner_id="result-input-handoff-coordinator",
        )
        context_worker = ContextPackageWorker(
            uow_factory=self.uow_factory,
            catalog=_EmptyCatalog(),
            owner_id="result-input-handoff-context",
            lease_seconds=60,
            clock=self.clock,
        )
        for _ in range(40):
            coordinator.run_once(limit=1)
            context_worker.run_once(limit=20)
            with self.uow_factory() as uow:
                transition = uow.handoff_transitions.get(
                    transition_id, self.workspace_id
                )
                assert transition is not None
                if transition.status in {
                    HandoffTransitionStatus.CONVERGED,
                    HandoffTransitionStatus.BLOCKED,
                    HandoffTransitionStatus.BLOCKED_UNKNOWN,
                    HandoffTransitionStatus.FAILED,
                }:
                    return transition
        raise AssertionError("handoff did not reach a terminal state")

    def _capture_gateway(self) -> WorkerGateway:
        return WorkerGateway(
            self.uow_factory,
            adapter_factories={
                "capture": lambda instance: self._capture_adapters_by_instance[
                    instance.id
                ],
            },
            workspace_factories={
                "capture-workspace": lambda _instance: self._capture_workspace_provider
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

    def captured_context(self, key: str) -> ContextMaterialization:
        adapters = self._capture_adapters_by_key.get(key, ())
        for adapter in reversed(adapters):
            if adapter.accepted_contexts:
                return adapter.accepted_contexts[-1]
        if self._capture_adapter is None or not self._capture_adapter.accepted_contexts:
            raise AssertionError("worker context was not accepted")
        return self._capture_adapter.accepted_contexts[-1]

    def start_call_count(self, key: str) -> int:
        return sum(
            len(adapter.start_calls)
            for adapter in self._capture_adapters_by_key.get(key, ())
        )

    def send_worker_input(
        self,
        key: str,
        *,
        text: str,
        attachment_refs: tuple[str, ...] = (),
    ) -> object:
        execution_id = self.latest_execution_id(key)
        context = self.captured_context(key)
        with self.uow_factory() as uow:
            session = uow.sessions.get_active_for_execution(execution_id)
            snapshot = uow.plans.get_current_for_root(
                self.ids["root_task"], self.workspace_id
            )
            assert session is not None
            assert snapshot is not None
            session_version = session.version
            worker_id = session.worker_id
        command_id = uuid4()
        return CommandService(
            self.uow_factory,
            (ExecutionInputCommandHandler(self.clock),),
        ).handle(
            CommandEnvelope(
                id=command_id,
                workspace_id=self.workspace_id,
                command_type="SendExecutionInput",
                target_type="execution_session",
                actor_type="operator",
                actor_id="operator",
                target_id=session.id,
                payload={
                    "context": {
                        "worker_item_id": str(self.worker_item_ids[key]),
                        "plan_generation": snapshot.generation,
                        "context_version": context.metadata.context_version,
                        "execution_id": str(execution_id),
                        "session_id": str(session.id),
                        "session_version": session_version,
                        "worker_id": str(worker_id),
                    },
                    "client_idempotency_key": f"result-input-intervention:{key}",
                    "text": text,
                    "attachment_refs": list(attachment_refs),
                },
                expected_version=session_version,
                idempotency_key=f"result-input-intervention-command:{key}",
                correlation_id=self.ids["root_task"],
            )
        )

    def retry_envelope(
        self,
        key: str,
        *,
        idempotency_key: str,
        command_id: UUID | None = None,
    ) -> CommandEnvelope:
        execution_id = self.latest_execution_id(key)
        with self.uow_factory() as uow:
            execution = uow.executions.get(execution_id, self.workspace_id)
            assert execution is not None
            execution_version = execution.version
        return CommandEnvelope(
            id=command_id or uuid4(),
            workspace_id=self.workspace_id,
            command_type="RetryExecution",
            target_type="execution",
            actor_type="operator",
            actor_id="operator",
            target_id=execution_id,
            payload={"not_before": self.now.isoformat()},
            expected_version=execution_version,
            idempotency_key=idempotency_key,
            correlation_id=self.ids["root_task"],
        )

    def fail_worker(self, key: str) -> None:
        execution_id = self.latest_execution_id(key)
        with self.uow_factory() as uow:
            execution = uow.executions.get(execution_id, self.workspace_id)
            assert execution is not None
            assert execution.active_lease_id is not None
            lease = uow.leases.get(execution.active_lease_id, self.workspace_id)
            session = uow.sessions.get_active_for_execution(execution.id)
            assert lease is not None
            assert session is not None
            worker_id = execution.assigned_worker_id or lease.worker_id
            execution_version = execution.version
        CommandService(
            self.uow_factory,
            (ExecutionCommandHandler(self.clock),),
        ).handle(
            CommandEnvelope(
                id=uuid4(),
                workspace_id=self.workspace_id,
                command_type="ReportExecutionFailed",
                target_type="execution",
                actor_type="worker",
                actor_id=str(worker_id),
                target_id=execution.id,
                payload={
                    "worker_id": str(worker_id),
                    "lease_id": str(lease.id),
                    "fencing_token": lease.fencing_token,
                    "failure_code": f"scenario_failure:{key}",
                },
                expected_version=execution_version,
                idempotency_key=f"result-input-failed:{execution.id}",
                correlation_id=self.ids["root_task"],
            )
        )

    def retry_worker(self, key: str) -> UUID:
        result = CommandService(
            self.uow_factory,
            (ExecutionCommandHandler(self.clock),),
        ).handle(
            self.retry_envelope(
                key,
                idempotency_key=(
                    f"result-input-retry:{self.latest_execution_id(key)}"
                ),
            )
        )
        return result.aggregate_id

    def continue_worker(self, key: str) -> UUID:
        execution_id = self.latest_execution_id(key)
        with self.uow_factory() as uow:
            execution = uow.executions.get(execution_id, self.workspace_id)
            assert execution is not None
            session = uow.sessions.get_latest_for_execution(
                execution.id, self.workspace_id
            )
            assert session is not None
            session_version = session.version
        result = CommandService(
            self.uow_factory,
            (ExecutionCommandHandler(self.clock),),
        ).handle(
            CommandEnvelope(
                id=uuid4(),
                workspace_id=self.workspace_id,
                command_type="ContinueExecutionSession",
                target_type="execution_session",
                actor_type="operator",
                actor_id="operator",
                target_id=session.id,
                payload={"not_before": self.now.isoformat()},
                expected_version=session_version,
                idempotency_key=f"result-input-continue:{session.id}",
                correlation_id=self.ids["root_task"],
            )
        )
        return result.aggregate_id

    def mutate_context_assembly_budget(
        self,
        key: str,
        mutate: Callable[[dict[str, object]], None],
    ) -> None:
        execution_id = self.latest_execution_id(key)
        with self.database.session() as session:
            row = session.scalar(
                select(ContextAssemblyRequestModel).where(
                    ContextAssemblyRequestModel.workspace_id == self.workspace_id,
                    ContextAssemblyRequestModel.consumer_id == execution_id,
                    ContextAssemblyRequestModel.purpose == ContextPurpose.EXECUTION.value,
                    ContextAssemblyRequestModel.manifest_schema_version == 2,
                )
            )
            assert row is not None
            budget = deepcopy(dict(row.budget))
            mutate(budget)
            session.execute(
                update(ContextAssemblyRequestModel)
                .where(
                    ContextAssemblyRequestModel.id == row.id,
                    ContextAssemblyRequestModel.workspace_id == self.workspace_id,
                )
                .values(budget=budget)
            )
            session.commit()

    def remove_execution_source_link(self, key: str) -> None:
        execution_id = self.latest_execution_id(key)
        with self.database.session() as session:
            session.execute(
                update(ExecutionModel)
                .where(
                    ExecutionModel.id == execution_id,
                    ExecutionModel.workspace_id == self.workspace_id,
                )
                .values(source_execution_id=None, source_session_id=None)
            )
            session.commit()

    def revoke_package(self, package_id: UUID, *, scope: str = "all_materialization") -> None:
        with self.uow_factory() as uow:
            package = uow.context_packages.get(package_id, self.workspace_id)
            assert package is not None
            causation_id = uow.session.scalar(
                select(CommandModel.id)
                .where(CommandModel.workspace_id == self.workspace_id)
                .order_by(CommandModel.created_at.desc(), CommandModel.id.desc())
                .limit(1)
            )
            assert causation_id is not None
            uow.context_packages.revoke(
                package_id,
                self.workspace_id,
                expected_package_hash=package.package_hash,
                reason="result-input-inheritance-test",
                scope=scope,
                now=self.now,
                actor_type="operator",
                actor_id="operator",
                causation_id=causation_id,
                correlation_id=self.ids["root_task"],
            )

    def _install_capture_runtime(
        self,
        execution_id: UUID,
        adapter: _CaptureWorkerAdapter,
        *,
        workspace_mode: str = "managed",
        reuse_workspace_instance: ModuleInstance | None = None,
    ) -> tuple[UUID, UUID, UUID]:
        workspace_capabilities = WorkspaceProviderCapabilityProfile(
            isolation=SandboxCapability.FILESYSTEM,
            writable=True,
            git_worktree=True,
            stability=StabilityLevel.STABLE,
            workspace_handoff_versions=(1,),
            workspace_transfer_modes=(HandoffTransferMode.REUSE_EXACT,),
            handoff_inspection=True,
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
            adapter_instance = ModuleInstance(
                id=uuid4(),
                workspace_id=self.workspace_id,
                definition_id=adapter_definition.id,
                instance_name=f"result-input-capture-adapter-{worker.id}",
                config_version=1,
                config_namespace="adapter",
                config={"adapter.mode": workspace_mode},
                secret_refs=(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
            if reuse_workspace_instance is None:
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
                workspace_instance = ModuleInstance(
                    id=uuid4(),
                    workspace_id=self.workspace_id,
                    definition_id=workspace_definition.id,
                    instance_name=f"result-input-capture-workspace-{worker.id}",
                    config_version=1,
                    config_namespace="workspace",
                    config={
                        "workspace.repository_ref": "repo://sidera",
                        "workspace.base_revision": "a" * 40,
                    },
                    secret_refs=(),
                    lifecycle=ModuleLifecycle.ACTIVE,
                )
            else:
                workspace_definition = None
                workspace_instance = reuse_workspace_instance
            uow.workers.add(worker)
            uow.modules.add_definition(adapter_definition)
            if workspace_definition is not None:
                uow.modules.add_definition(workspace_definition)
            uow.modules.add_instance(adapter_instance)
            if reuse_workspace_instance is None:
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
        self._capture_adapters_by_instance[adapter_instance.id] = adapter
        self._capture_workers_by_adapter_instance[adapter_instance.id] = worker.id
        if self._capture_workspace_instance is None:
            self._capture_workspace_instance = workspace_instance
        return worker.id, adapter_instance.id, workspace_instance.id

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
        values: list[UUID] = []
        with self.uow_factory() as uow:
            executions = uow.executions.list_for_worker_item(
                self.worker_item_ids[key], self.workspace_id
            )
            for execution in executions:
                for version in (1, 2):
                    package = uow.context_packages.get_for_consumer(
                        ContextPurpose.EXECUTION,
                        execution.id,
                        self.workspace_id,
                        manifest_schema_version=version,
                    )
                    if package is not None:
                        values.append(package.package_id)
        return tuple(values)

    def latest_execution_id(self, key: str) -> UUID:
        execution_id = self._execution_id(key)
        assert execution_id is not None
        return execution_id

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

    def get_result_inputs(
        self,
        key: str,
        *,
        execution_request_id: UUID | None = None,
        workspace_id: UUID | None = None,
        result_inputs_enabled: bool = True,
    ):
        route_workspace_id = workspace_id or self.workspace_id
        params = (
            {"execution_request_id": str(execution_request_id)}
            if execution_request_id is not None
            else {}
        )
        settings = Settings(
            database_url=str(self.database.engine.url),
            collaboration_governance_enabled=True,
            result_inputs_enabled=result_inputs_enabled,
            local_actor_id="operator",
        )
        with TestClient(create_app(settings)) as client:
            return client.get(
                f"/api/v1/workspaces/{route_workspace_id}/worker-items/"
                f"{self.worker_item_ids[key]}/result-inputs",
                params=params,
            )

    def get_input_selection(
        self,
        selection_id: UUID,
        *,
        workspace_id: UUID | None = None,
        result_inputs_enabled: bool = True,
    ):
        route_workspace_id = workspace_id or self.workspace_id
        settings = Settings(
            database_url=str(self.database.engine.url),
            collaboration_governance_enabled=True,
            result_inputs_enabled=result_inputs_enabled,
            local_actor_id="operator",
        )
        with TestClient(create_app(settings)) as client:
            return client.get(
                f"/api/v1/workspaces/{route_workspace_id}/collaboration/"
                f"input-selections/{selection_id}"
            )

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
                select(ExecutionModel.id)
                .where(
                    ExecutionModel.workspace_id == self.workspace_id,
                    ExecutionModel.worker_item_id == worker_item_id,
                )
                .order_by(
                    ExecutionModel.created_at.desc(),
                    ExecutionModel.attempt.desc(),
                    ExecutionModel.id.desc(),
                )
                .limit(1)
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
    max_attempts: int = 1,
    browser_state: bool = False,
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

    if browser_state:
        subtasks = raw["subtasks"]
        milestones = raw["milestones"]
        assert isinstance(subtasks, list)
        assert isinstance(milestones, list)
        subtasks.append(
            {
                "key": "integration-gate",
                "kind": "child",
                "title": "Integration gate",
                "goal_summary": "Wait for the independent integration result.",
                "acceptance_refs": ["acceptance:integration"],
                "expected_outputs": ["artifact_type:build"],
                "milestone_key": "m3",
                "source_refs": ["brief:1"],
            }
        )
        milestones.append(
            {
                "key": "m3",
                "title": "Integration gate",
                "subtask_keys": ["integration-gate"],
                "acceptance_refs": ["acceptance:integration"],
                "order_hint": 3,
            }
        )

        source_d = deepcopy(build_worker)
        source_d["key"] = "d"
        source_d["title"] = "Worker D"
        source_d["responsibility"] = "Produce the independent integration result."
        worker_items.append(source_d)
        source_d_intent = deepcopy(build_intent)
        source_d_intent["worker_item_key"] = "d"
        source_d_intent["purpose"] = "Produce the independent integration result."
        intents.append(source_d_intent)

        approval_worker = deepcopy(summary_worker)
        approval_worker["key"] = "approval-worker"
        approval_worker["title"] = "Approval worker"
        approval_worker["role"] = "approver"
        approval_worker["responsibility"] = "Review the fixed result selection."
        worker_items.append(approval_worker)
        approval_intent = deepcopy(summary_intent)
        approval_intent["worker_item_key"] = "approval-worker"
        approval_intent["purpose"] = "Review the fixed result selection."
        intents.append(approval_intent)

        integration_worker = deepcopy(summary_worker)
        integration_worker["key"] = "integration-gate-worker"
        integration_worker["subtask_key"] = "integration-gate"
        integration_worker["title"] = "Integration gate worker"
        integration_worker["role"] = "integrator"
        integration_worker["responsibility"] = "Wait for the independent integration result."
        worker_items.append(integration_worker)
        integration_intent = deepcopy(summary_intent)
        integration_intent["worker_item_key"] = "integration-gate-worker"
        integration_intent["purpose"] = "Wait for the independent integration result."
        integration_intent["activation_dependencies"] = [
            {
                "upstream_worker_item_key": "d",
                "downstream_worker_item_key": "integration-gate-worker",
                "kind": "hard",
                "required_artifact_refs": ["artifact_type:build"],
            }
        ]
        intents.append(integration_intent)
    for intent in intents:
        intent["max_attempts"] = max_attempts
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
    browser_groups = (
        *default_groups,
        {
            "group_key": "approval-review",
            "title": "Approval review",
            "consumer_worker_item_key": "approval-worker",
            "candidates": [
                {"worker_item_key": "b", "priority": 1},
                {"worker_item_key": "c", "priority": 2},
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
    if browser_state and groups is None:
        selected_groups = browser_groups
    elif groups is None:
        selected_groups = default_groups
    else:
        selected_groups = groups
    policy["input_groups"] = [deepcopy(item) for item in selected_groups]
    return raw


@pytest.fixture
def scenario(
    database: Database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)
