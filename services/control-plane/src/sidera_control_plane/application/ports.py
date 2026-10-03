from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Literal, Protocol, Self
from uuid import UUID

from sqlalchemy.orm import Session

from sidera_control_plane.application.collaboration_projection import CollaborationFacts
from sidera_control_plane.application.knowledge_candidate_submission import (
    KnowledgeCandidateProposalV1,
    KnowledgeCandidateSubmission,
)
from sidera_control_plane.application.notification_observations import (
    NotificationBaselineCandidate,
    NotificationObservation,
    NotificationRouteBackfillCandidate,
)
from sidera_control_plane.application.planner_runtime import (
    PlannerInput,
    PlannerResult,
    PlannerRunControl,
)
from sidera_control_plane.application.result_ports import (
    CodeInputReceipt,
    ResultRepositoryPort,
)
from sidera_control_plane.domain.agent_bindings import (
    AgentBinding,
    AgentExecutionLink,
    ExecutionAuthority,
)
from sidera_control_plane.domain.artifacts import (
    Artifact,
    ArtifactCandidate,
    EvidenceClaim,
)
from sidera_control_plane.domain.attention import (
    AttentionAcknowledgement,
    AttentionSource,
)
from sidera_control_plane.domain.code_results import CodeInputManifestV1
from sidera_control_plane.domain.collaboration import (
    CollaborationEvaluation,
    CollaborationRun,
    RuntimeDecision,
    RuntimeIntent,
    RuntimeIntentApplication,
    RuntimeProjectionSnapshot,
    RuntimeRelease,
)
from sidera_control_plane.domain.collaboration_governance import (
    CollaborationGovernanceHold,
    CollaborationGovernanceResolution,
    CollaborationReleaseProposal,
)
from sidera_control_plane.domain.commands import (
    CommandEnvelope,
    CommandResult,
    StoredCommand,
)
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextAssemblyRequest,
    ContextConsumerType,
    ContextMaterialization,
    ContextMaterializationAudit,
    ContextMaterializationV1,
    ContextPackageManifest,
    ContextPurpose,
)
from sidera_control_plane.domain.context_patches import (
    ContextPatchAcknowledgement,
    ContextPatchDeliveryResult,
    ContextPatchMaterializationV1,
    ContextPatchV1,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestDependency,
)
from sidera_control_plane.domain.gateway import (
    AdapterEvent,
    AdapterSessionHandle,
    DeliveryResult,
    ExecutionEvent,
    GatewayOperation,
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffCheckpointEvidence,
    HandoffContextBundle,
    HandoffOperationInspection,
    HandoffWorkspaceInspection,
    HandoffWorkspaceTargetSpec,
    HandoffWorkspaceTransfer,
)
from sidera_control_plane.domain.handoff_recovery import HandoffRecoveryDecision
from sidera_control_plane.domain.handoffs import (
    HandoffProposal,
    HandoffTransition,
    HandoffTransitionStatus,
)
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
)
from sidera_control_plane.domain.inputs import ExecutionInput
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    ExecutionBinding,
    ModuleDefinition,
    ModuleInstance,
    PlannerAdapterCapabilityProfile,
    WorkerAdapterCapabilityProfile,
)
from sidera_control_plane.domain.notifications import (
    InboxThread,
    NotificationDeliveryAttempt,
    NotificationDeliveryIntent,
    NotificationDeliveryStatus,
    NotificationFact,
    NotificationPolicyState,
    NotificationReconciliationCursor,
    NotificationRoute,
    NotificationSourceBinding,
)
from sidera_control_plane.domain.plan_revisions import RootTaskPlanHead
from sidera_control_plane.domain.plan_transitions import (
    PlanEntityLineage,
    PlanTransition,
    WorkerTransition,
)
from sidera_control_plane.domain.planning import (
    PlanningBriefRevision,
    PlanningClarification,
    PlanningRequest,
    PlanningRun,
    PlanningRunLease,
    ProposalMaterialization,
)
from sidera_control_plane.domain.planning_profiles import PlanningProfile
from sidera_control_plane.domain.planning_proposals import ProposalRevision
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.projections import ProjectionChange
from sidera_control_plane.domain.scheduling import SchedulingBlock
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.work_context import WorkContext, WorkContextRevision
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration
from sidera_control_plane.domain.workspace import (
    ExecutionSpec,
    ExecutionWorkspaceManifest,
)


class TaskRepositoryPort(Protocol):
    def get(
        self, task_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> Task | None:
        raise NotImplementedError

    def get_many(
        self, task_ids: Collection[UUID], workspace_id: UUID
    ) -> tuple[Task, ...]:
        raise NotImplementedError

    def workspace_exists(self, workspace_id: UUID) -> bool:
        raise NotImplementedError

    def add(self, task: Task) -> None:
        raise NotImplementedError

    def save(self, task: Task, previous_version: int) -> None:
        raise NotImplementedError


class PlanRepositoryPort(Protocol):
    def add(self, snapshot: TaskPlanSnapshot) -> None:
        raise NotImplementedError

    def add_snapshot_and_advance_head(
        self,
        snapshot: TaskPlanSnapshot,
        *,
        expected_head: RootTaskPlanHead | None,
    ) -> RootTaskPlanHead:
        raise NotImplementedError

    def get_head(
        self,
        root_task_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> RootTaskPlanHead | None:
        raise NotImplementedError

    def advance_head(
        self,
        head: RootTaskPlanHead,
        *,
        expected_snapshot_id: UUID,
        target: TaskPlanSnapshot,
    ) -> RootTaskPlanHead:
        raise NotImplementedError

    def get(self, snapshot_id: UUID, workspace_id: UUID) -> TaskPlanSnapshot | None:
        raise NotImplementedError

    def get_current_for_root(
        self, root_task_id: UUID, workspace_id: UUID
    ) -> TaskPlanSnapshot | None:
        raise NotImplementedError

    def get_current_for_task(
        self, task_id: UUID, workspace_id: UUID
    ) -> TaskPlanSnapshot | None:
        raise NotImplementedError

    def get_for_generation(
        self, root_task_id: UUID, workspace_id: UUID, generation: int
    ) -> TaskPlanSnapshot | None:
        raise NotImplementedError

    def list_for_root(
        self, root_task_id: UUID, workspace_id: UUID
    ) -> tuple[TaskPlanSnapshot, ...]:
        raise NotImplementedError

    def list_roots_with_snapshots(self, workspace_id: UUID) -> tuple[UUID, ...]:
        raise NotImplementedError


class PlanTransitionRepositoryPort(Protocol):
    def add(self, transition: PlanTransition) -> None:
        raise NotImplementedError

    def get(
        self, transition_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> PlanTransition | None:
        raise NotImplementedError

    def get_for_target(
        self,
        *,
        workspace_id: UUID,
        root_task_id: UUID,
        plan_snapshot_id: UUID,
        plan_generation: int,
    ) -> PlanTransition | None:
        raise NotImplementedError

    def save(self, transition: PlanTransition, previous_version: int) -> None:
        raise NotImplementedError

    def claim_converging(
        self, *, now: datetime, limit: int
    ) -> tuple[PlanTransition, ...]:
        raise NotImplementedError

    def list_for_root(
        self, root_task_id: UUID, workspace_id: UUID
    ) -> tuple[PlanTransition, ...]:
        raise NotImplementedError


class WorkerTransitionRepositoryPort(Protocol):
    def add(self, transition: WorkerTransition) -> None:
        raise NotImplementedError

    def get(
        self, transition_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> WorkerTransition | None:
        raise NotImplementedError

    def save(self, transition: WorkerTransition, previous_version: int) -> None:
        raise NotImplementedError

    def get_active_for_worker_item(
        self,
        worker_item_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkerTransition | None:
        raise NotImplementedError

    def claim_pending(
        self, *, now: datetime, limit: int
    ) -> tuple[WorkerTransition, ...]:
        raise NotImplementedError

    def list_for_plan_transition(
        self, plan_transition_id: UUID, workspace_id: UUID
    ) -> tuple[WorkerTransition, ...]:
        raise NotImplementedError


class PlanEntityLineageRepositoryPort(Protocol):
    def add(self, lineage: PlanEntityLineage) -> None:
        raise NotImplementedError

    def get(self, lineage_id: UUID, workspace_id: UUID) -> PlanEntityLineage | None:
        raise NotImplementedError

    def list_for_transition(
        self, plan_transition_id: UUID, workspace_id: UUID
    ) -> tuple[PlanEntityLineage, ...]:
        raise NotImplementedError


class ContextPatchRepositoryPort(Protocol):
    def add(self, patch: ContextPatchV1) -> None:
        raise NotImplementedError

    def get(
        self, patch_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ContextPatchV1 | None:
        raise NotImplementedError

    def get_by_hash(self, patch_hash: str, workspace_id: UUID) -> ContextPatchV1 | None:
        raise NotImplementedError

    def list_for_worker_item(
        self, worker_item_id: UUID, workspace_id: UUID
    ) -> tuple[ContextPatchV1, ...]:
        raise NotImplementedError

    def add_acknowledgement(
        self, acknowledgement: ContextPatchAcknowledgement, workspace_id: UUID
    ) -> None:
        raise NotImplementedError

    def get_acknowledgement(
        self, patch_id: UUID, workspace_id: UUID
    ) -> ContextPatchAcknowledgement | None:
        raise NotImplementedError


class PlanningProfileRepositoryPort(Protocol):
    def add(self, profile: PlanningProfile) -> None:
        raise NotImplementedError

    def get(
        self,
        profile_id: UUID,
        workspace_id: UUID,
        *,
        version: int | None = None,
    ) -> PlanningProfile | None:
        raise NotImplementedError

    def list_selectable(self, workspace_id: UUID) -> tuple[PlanningProfile, ...]:
        raise NotImplementedError


class PlanningRequestRepositoryPort(Protocol):
    def add(self, request: PlanningRequest) -> None:
        raise NotImplementedError

    def get(
        self,
        request_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> PlanningRequest | None:
        raise NotImplementedError

    def get_active_revision_for_root(
        self,
        root_task_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> PlanningRequest | None:
        raise NotImplementedError

    def list_by_source_ref(
        self, workspace_id: UUID, source_type: str, source_ref: str
    ) -> tuple[PlanningRequest, ...]:
        raise NotImplementedError

    def save(self, request: PlanningRequest, previous_version: int) -> None:
        raise NotImplementedError


class PlanningRunRepositoryPort(Protocol):
    def add(self, run: PlanningRun) -> None:
        raise NotImplementedError

    def get(
        self,
        run_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> PlanningRun | None:
        raise NotImplementedError

    def save(self, run: PlanningRun, previous_version: int) -> None:
        raise NotImplementedError

    def claim_queued(self, *, now: datetime, limit: int) -> tuple[PlanningRun, ...]:
        raise NotImplementedError

    def claim_failed(self, *, now: datetime, limit: int) -> tuple[PlanningRun, ...]:
        raise NotImplementedError


class PlanningLeaseRepositoryPort(Protocol):
    def add(self, lease: PlanningRunLease) -> None:
        raise NotImplementedError

    def get(
        self,
        lease_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> PlanningRunLease | None:
        raise NotImplementedError

    def get_active_for_run(
        self,
        run_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> PlanningRunLease | None:
        raise NotImplementedError

    def save(self, lease: PlanningRunLease, previous_version: int) -> None:
        raise NotImplementedError

    def claim_expired(
        self, *, now: datetime, limit: int
    ) -> tuple[PlanningRunLease, ...]:
        raise NotImplementedError


class PlanningRecordRepositoryPort(Protocol):
    def add_brief(self, brief: PlanningBriefRevision) -> None:
        raise NotImplementedError

    def get_brief(
        self, request_id: UUID, workspace_id: UUID, revision: int
    ) -> PlanningBriefRevision | None:
        raise NotImplementedError

    def list_briefs(
        self, request_id: UUID, workspace_id: UUID
    ) -> tuple[PlanningBriefRevision, ...]:
        raise NotImplementedError

    def add_proposal(self, proposal: ProposalRevision, workspace_id: UUID) -> None:
        raise NotImplementedError

    def get_proposal(
        self, proposal_id: UUID, workspace_id: UUID
    ) -> ProposalRevision | None:
        raise NotImplementedError

    def list_proposals(
        self, request_id: UUID, workspace_id: UUID
    ) -> tuple[ProposalRevision, ...]:
        raise NotImplementedError

    def get_current_proposal(
        self, request_id: UUID, workspace_id: UUID
    ) -> ProposalRevision | None:
        raise NotImplementedError

    def add_clarification(self, clarification: PlanningClarification) -> None:
        raise NotImplementedError

    def list_pending_clarifications(
        self, request_id: UUID, workspace_id: UUID
    ) -> tuple[PlanningClarification, ...]:
        raise NotImplementedError

    def save_clarification(
        self, clarification: PlanningClarification, previous_version: int
    ) -> None:
        raise NotImplementedError


class WorkContextRepositoryPort(Protocol):
    def get(
        self,
        work_context_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkContext | None:
        raise NotImplementedError

    def get_by_planning_request(
        self,
        planning_request_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkContext | None:
        raise NotImplementedError

    def get_by_root_task(
        self,
        root_task_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkContext | None:
        raise NotImplementedError

    def get_revision(
        self,
        revision_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkContextRevision | None:
        raise NotImplementedError

    def add(self, context: WorkContext, first_revision: WorkContextRevision) -> None:
        raise NotImplementedError

    def append_revision(
        self,
        context: WorkContext,
        revision: WorkContextRevision,
        *,
        expected_version: int,
    ) -> None:
        raise NotImplementedError

    def bind_root_task(
        self,
        context: WorkContext,
        root_task_id: UUID,
        revision: WorkContextRevision,
        *,
        expected_version: int,
    ) -> None:
        raise NotImplementedError

    def archive(self, context: WorkContext, *, expected_version: int) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class SearchHit:
    page_id: UUID
    revision_id: UUID
    section_key: str
    revision_number: int
    revision_hash: str
    section_hash: str
    title: str
    summary: str
    tags: tuple[str, ...]
    source_ids: tuple[UUID, ...]
    source_types: tuple[str, ...]
    source_availability: tuple[str, ...]
    trust: str
    section_byte_count: int
    rank: float
    rank_explanation: str


@dataclass(frozen=True, slots=True)
class ExactSectionRef:
    ordinal: int
    page_id: UUID
    revision_id: UUID
    revision_hash: str
    section_key: str
    section_hash: str
    source_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeSourceMetadata:
    source_id: UUID
    workspace_id: UUID
    source_type: str
    label: str
    uri: str | None
    workspace_relative_path: str | None
    git_revision: str | None
    digest: str | None
    metadata: Mapping[str, object]
    availability: str
    safe_reason_code: str | None
    last_checked_at: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class ExactRevisionSection:
    section_key: str
    heading: str
    summary: str
    ordinal: int
    section_hash: str
    content_byte_count: int


@dataclass(frozen=True, slots=True)
class ExactRevisionManifest:
    workspace_id: UUID
    page_id: UUID
    revision_id: UUID
    revision_number: int
    revision_hash: str
    title: str
    summary: str
    tags: tuple[str, ...]
    source_ids: tuple[UUID, ...]
    sections: tuple[ExactRevisionSection, ...]


@dataclass(frozen=True, slots=True)
class ExactSectionBody:
    ordinal: int
    page_id: UUID
    revision_id: UUID
    revision_hash: str
    section_key: str
    section_hash: str
    body_markdown: str
    sources: tuple[KnowledgeSourceMetadata, ...]
    trust: str
    section_byte_count: int


@dataclass(frozen=True, slots=True)
class KnowledgeCommandStatus:
    command_id: UUID
    workspace_id: UUID
    status: Literal["pending", "applied", "rejected"]
    result: Mapping[str, object] | None
    error_code: str | None


class KnowledgeCatalogPort(Protocol):
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
        raise NotImplementedError

    def get_revision_manifest(
        self,
        workspace_id: UUID,
        page_id: UUID,
        revision_id: UUID,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> ExactRevisionManifest:
        raise NotImplementedError

    def materialize(
        self,
        workspace_id: UUID,
        sections: Sequence[ExactSectionRef],
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[ExactSectionBody, ...]:
        raise NotImplementedError


class KnowledgeCandidateCatalogPort(Protocol):
    def submit_candidate(
        self,
        workspace_id: UUID,
        command_id: UUID,
        idempotency_key: str,
        candidate_payload: Mapping[str, object],
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> KnowledgeCommandStatus:
        raise NotImplementedError

    def get_command_status(
        self,
        workspace_id: UUID,
        command_id: UUID,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> KnowledgeCommandStatus:
        raise NotImplementedError


class ContextAssemblyRepositoryPort(Protocol):
    def add(self, request: ContextAssemblyRequest) -> ContextAssemblyRequest:
        raise NotImplementedError

    def get_for_consumer(
        self,
        purpose: ContextPurpose,
        consumer_id: UUID,
        workspace_id: UUID,
        *,
        manifest_schema_version: int = 1,
        for_update: bool = False,
    ) -> ContextAssemblyRequest | None:
        raise NotImplementedError

    def get_for_update(
        self, request_id: UUID, workspace_id: UUID
    ) -> ContextAssemblyRequest | None:
        raise NotImplementedError

    def rebind_pending(
        self,
        request: ContextAssemblyRequest,
        *,
        expected_version: int,
        now: datetime,
    ) -> ContextAssemblyRequest:
        raise NotImplementedError

    def reset_for_retry(
        self,
        request: ContextAssemblyRequest,
        *,
        expected_version: int,
        now: datetime,
    ) -> ContextAssemblyRequest:
        raise NotImplementedError

    def claim_next(
        self,
        *,
        workspace_id: UUID | None = None,
        owner: str,
        now: datetime,
        lease_ttl: timedelta,
        limit: int,
    ) -> tuple[ContextAssemblyRequest, ...]:
        raise NotImplementedError

    def save_retry(
        self,
        request: ContextAssemblyRequest,
        *,
        owner: str,
        expected_version: int,
        next_attempt_at: datetime,
        error_code: str,
        now: datetime | None = None,
    ) -> ContextAssemblyRequest:
        raise NotImplementedError

    def save_blocked(
        self,
        request: ContextAssemblyRequest,
        *,
        owner: str,
        expected_version: int,
        error_code: str,
        now: datetime | None = None,
    ) -> ContextAssemblyRequest:
        raise NotImplementedError

    def complete_with_package(
        self,
        request: ContextAssemblyRequest,
        manifest: ContextPackageManifest,
        *,
        owner: str,
        expected_version: int,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> ContextPackageManifest:
        raise NotImplementedError


class ContextPackageRepositoryPort(Protocol):
    def get(
        self, package_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ContextPackageManifest | None:
        raise NotImplementedError

    def get_ack(
        self,
        package_id: UUID,
        consumer_type: ContextConsumerType,
        consumer_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ContextAcknowledgement | None:
        raise NotImplementedError

    def get_for_consumer(
        self,
        purpose: ContextPurpose,
        consumer_id: UUID,
        workspace_id: UUID,
        *,
        manifest_schema_version: int = 1,
        for_update: bool = False,
    ) -> ContextPackageManifest | None:
        raise NotImplementedError

    def next_materialization_attempt(self, package_id: UUID, workspace_id: UUID) -> int:
        raise NotImplementedError

    def add_ack(
        self,
        acknowledgement: ContextAcknowledgement,
        *,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> ContextAcknowledgement:
        raise NotImplementedError

    def append_materialization_audit(
        self,
        audit: ContextMaterializationAudit,
        *,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> None:
        raise NotImplementedError

    def revoke(
        self,
        package_id: UUID,
        workspace_id: UUID,
        *,
        expected_package_hash: str,
        reason: str,
        scope: str,
        now: datetime,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> ContextPackageManifest:
        raise NotImplementedError

    def mark_corrupt(
        self,
        package_id: UUID,
        workspace_id: UUID,
        *,
        reason: str,
        now: datetime,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> ContextPackageManifest:
        raise NotImplementedError


class KnowledgeCandidateSubmissionRepositoryPort(Protocol):
    def add(self, submission: KnowledgeCandidateSubmission) -> None:
        raise NotImplementedError

    def get(
        self,
        submission_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> KnowledgeCandidateSubmission | None:
        raise NotImplementedError

    def find_by_producer_hash(
        self,
        workspace_id: UUID,
        producer_type: str,
        producer_id: UUID,
        proposal_hash: str,
        *,
        for_update: bool = False,
    ) -> KnowledgeCandidateSubmission | None:
        raise NotImplementedError

    def claim_next(
        self,
        *,
        owner: str,
        now: datetime,
        lease_ttl: timedelta,
        limit: int,
        workspace_id: UUID | None = None,
    ) -> tuple[KnowledgeCandidateSubmission, ...]:
        raise NotImplementedError

    def save(
        self, submission: KnowledgeCandidateSubmission, previous_version: int
    ) -> None:
        raise NotImplementedError

    def list_for_producer(
        self, workspace_id: UUID, producer_type: str, producer_id: UUID
    ) -> tuple[KnowledgeCandidateSubmission, ...]:
        raise NotImplementedError

    def list_for_workspace(
        self,
        workspace_id: UUID,
        *,
        producer_types: Collection[str] = (),
    ) -> tuple[KnowledgeCandidateSubmission, ...]:
        raise NotImplementedError


class ProposalMaterializationRepositoryPort(Protocol):
    def add(self, materialization: ProposalMaterialization) -> None:
        raise NotImplementedError

    def get_by_request(
        self, request_id: UUID, workspace_id: UUID
    ) -> ProposalMaterialization | None:
        raise NotImplementedError

    def get_by_proposal(
        self, proposal_id: UUID, workspace_id: UUID
    ) -> ProposalMaterialization | None:
        raise NotImplementedError


class ExecutionRequestRepositoryPort(Protocol):
    def add(self, request: ExecutionRequest) -> None:
        raise NotImplementedError

    def get(
        self,
        request_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionRequest | None:
        raise NotImplementedError

    def get_by_proposal_worker_item(
        self,
        proposal_revision_id: UUID,
        worker_item_key: str,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionRequest | None:
        raise NotImplementedError

    def get_by_handoff_transition(
        self,
        transition_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionRequest | None:
        raise NotImplementedError

    def get_by_execution_lineage(
        self,
        execution_lineage_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionRequest | None:
        """Load the normal request that owns a source execution lineage."""
        raise NotImplementedError

    def get_effective_by_execution_lineage(
        self,
        execution_lineage_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionRequest | None:
        """Load any current request, including handoff targets, for a lineage."""
        raise NotImplementedError

    def get_many(
        self,
        request_ids: Collection[UUID],
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[ExecutionRequest, ...]:
        """Load requests in stable identity order for deterministic locking."""
        raise NotImplementedError

    def list_for_root(
        self,
        root_task_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[ExecutionRequest, ...]:
        """List every execution request for a root in stable identity order."""
        raise NotImplementedError

    def save(self, request: ExecutionRequest, previous_version: int) -> None:
        raise NotImplementedError

    def claim_ready(self, *, now: datetime, limit: int) -> tuple[ExecutionRequest, ...]:
        raise NotImplementedError

    def claim_blocked(
        self, *, now: datetime, limit: int
    ) -> tuple[ExecutionRequest, ...]:
        raise NotImplementedError

    def add_dependency(self, dependency: ExecutionRequestDependency) -> None:
        raise NotImplementedError

    def list_dependencies(
        self, downstream_request_id: UUID, workspace_id: UUID
    ) -> tuple[ExecutionRequestDependency, ...]:
        raise NotImplementedError

    def list_downstream_for_upstream(
        self, upstream_request_id: UUID, workspace_id: UUID
    ) -> tuple[ExecutionRequest, ...]:
        raise NotImplementedError


class CollaborationRunRepositoryPort(Protocol):
    def get_current(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        for_update: bool = False,
    ) -> CollaborationRun | None:
        raise NotImplementedError

    def get(
        self,
        workspace_id: UUID,
        run_id: UUID,
        *,
        for_update: bool = False,
    ) -> CollaborationRun | None:
        raise NotImplementedError

    def get_for_plan(
        self,
        workspace_id: UUID,
        plan_snapshot_id: UUID,
        plan_generation: int,
        *,
        for_update: bool = False,
    ) -> CollaborationRun | None:
        raise NotImplementedError

    def add(self, run: CollaborationRun) -> None:
        raise NotImplementedError

    def save(self, run: CollaborationRun, previous_version: int) -> None:
        raise NotImplementedError

    def list_recovery_candidates(
        self, *, now: datetime, limit: int
    ) -> tuple[CollaborationRun, ...]:
        raise NotImplementedError


class CollaborationFactStorePort(Protocol):
    def load(
        self,
        workspace_id: UUID,
        run: CollaborationRun,
        *,
        fact_cursor: int,
    ) -> CollaborationFacts:
        raise NotImplementedError


class CollaborationEvaluationRepositoryPort(Protocol):
    def get(
        self,
        workspace_id: UUID,
        evaluation_id: UUID,
        *,
        for_update: bool = False,
    ) -> CollaborationEvaluation | None:
        raise NotImplementedError

    def get_for_projection(
        self, workspace_id: UUID, projection_snapshot_id: UUID
    ) -> CollaborationEvaluation | None:
        raise NotImplementedError

    def get_live_for_run(
        self, workspace_id: UUID, run_id: UUID, *, now: datetime
    ) -> CollaborationEvaluation | None:
        raise NotImplementedError

    def get_or_add(
        self, evaluation: CollaborationEvaluation
    ) -> CollaborationEvaluation:
        raise NotImplementedError

    def claim_next(
        self,
        *,
        workspace_id: UUID | None,
        run_id: UUID | None = None,
        owner_id: str,
        now: datetime,
        lease_ttl: timedelta,
        limit: int,
    ) -> tuple[CollaborationEvaluation, ...]:
        raise NotImplementedError

    def save(self, evaluation: CollaborationEvaluation, previous_version: int) -> None:
        raise NotImplementedError

    def recover_expired(
        self,
        *,
        now: datetime,
        limit: int,
        workspace_id: UUID | None = None,
    ) -> tuple[CollaborationEvaluation, ...]:
        raise NotImplementedError


class CollaborationLedgerRepositoryPort(Protocol):
    def get_projection(
        self, workspace_id: UUID, projection_snapshot_id: UUID
    ) -> RuntimeProjectionSnapshot | None:
        raise NotImplementedError

    def get_or_add_projection(
        self, snapshot: RuntimeProjectionSnapshot
    ) -> RuntimeProjectionSnapshot:
        raise NotImplementedError

    def next_decision_sequence(self, run_id: UUID, workspace_id: UUID) -> int:
        raise NotImplementedError

    def add_decision_bundle(
        self, decision: RuntimeDecision, intents: tuple[RuntimeIntent, ...]
    ) -> None:
        raise NotImplementedError

    def add_applications(
        self, applications: tuple[RuntimeIntentApplication, ...]
    ) -> None:
        raise NotImplementedError

    def add_releases(self, releases: tuple[RuntimeRelease, ...]) -> None:
        raise NotImplementedError

    def get_release_for_request(
        self,
        workspace_id: UUID,
        request_id: UUID,
        *,
        for_update: bool = False,
    ) -> RuntimeRelease | None:
        raise NotImplementedError

    def get_decision(
        self, workspace_id: UUID, decision_id: UUID, *, for_update: bool = False
    ) -> RuntimeDecision | None:
        raise NotImplementedError

    def get_intent(
        self, workspace_id: UUID, intent_id: UUID, *, for_update: bool = False
    ) -> RuntimeIntent | None:
        raise NotImplementedError

    def get_application(
        self, workspace_id: UUID, application_id: UUID, *, for_update: bool = False
    ) -> RuntimeIntentApplication | None:
        raise NotImplementedError

    def save_application(
        self, application: RuntimeIntentApplication, previous_version: int
    ) -> None:
        raise NotImplementedError


class InputSelectionRepositoryPort(Protocol):
    def add(
        self, selection: CollaborationInputSelectionV1
    ) -> CollaborationInputSelectionV1:
        raise NotImplementedError

    def get(
        self, workspace_id: UUID, selection_id: UUID
    ) -> CollaborationInputSelectionV1 | None:
        raise NotImplementedError

    def get_by_intent(
        self, workspace_id: UUID, intent_id: UUID
    ) -> CollaborationInputSelectionV1 | None:
        raise NotImplementedError


class CollaborationGovernanceRepositoryPort(Protocol):
    def get_hold(
        self, workspace_id: UUID, hold_id: UUID, *, for_update: bool = False
    ) -> CollaborationGovernanceHold | None:
        raise NotImplementedError

    def get_active_hold(
        self,
        workspace_id: UUID,
        run_id: UUID,
        worker_item_id: UUID,
        *,
        for_update: bool = False,
    ) -> CollaborationGovernanceHold | None:
        raise NotImplementedError

    def get_active_holds(
        self,
        workspace_id: UUID,
        run_id: UUID,
        worker_item_ids: tuple[UUID, ...],
        *,
        for_update: bool = False,
    ) -> tuple[CollaborationGovernanceHold, ...]:
        raise NotImplementedError

    def list_active_holds_for_run(
        self, workspace_id: UUID, run_id: UUID, *, for_update: bool = False
    ) -> tuple[CollaborationGovernanceHold, ...]:
        raise NotImplementedError

    def add_hold(self, hold: CollaborationGovernanceHold) -> None:
        raise NotImplementedError

    def save_hold(
        self, hold: CollaborationGovernanceHold, previous_version: int
    ) -> None:
        raise NotImplementedError

    def get_proposal(
        self, workspace_id: UUID, proposal_id: UUID, *, for_update: bool = False
    ) -> CollaborationReleaseProposal | None:
        raise NotImplementedError

    def get_proposal_for_intent(
        self, workspace_id: UUID, intent_id: UUID
    ) -> CollaborationReleaseProposal | None:
        raise NotImplementedError

    def find_matching_proposal(
        self,
        workspace_id: UUID,
        hold_id: UUID,
        execution_request_id: UUID,
        target_request_version: int,
        fingerprint_version: int,
        fingerprint: str,
        *,
        for_update: bool = False,
    ) -> CollaborationReleaseProposal | None:
        raise NotImplementedError

    def list_pending_proposals_for_hold(
        self, workspace_id: UUID, hold_id: UUID, *, for_update: bool = False
    ) -> tuple[CollaborationReleaseProposal, ...]:
        raise NotImplementedError

    def list_pending_proposals_for_run(
        self, workspace_id: UUID, run_id: UUID, *, for_update: bool = False
    ) -> tuple[CollaborationReleaseProposal, ...]:
        raise NotImplementedError

    def list_supersede_candidates(
        self,
        *,
        now: datetime,
        limit: int,
        workspace_id: UUID | None = None,
    ) -> tuple[CollaborationGovernanceHold, ...]:
        raise NotImplementedError

    def add_proposal(self, proposal: CollaborationReleaseProposal) -> None:
        raise NotImplementedError

    def save_proposal(
        self, proposal: CollaborationReleaseProposal, previous_version: int
    ) -> None:
        raise NotImplementedError

    def get_resolution_for_proposal(
        self, workspace_id: UUID, proposal_id: UUID
    ) -> CollaborationGovernanceResolution | None:
        raise NotImplementedError

    def add_resolution(self, resolution: CollaborationGovernanceResolution) -> None:
        raise NotImplementedError


class HandoffProposalRepositoryPort(Protocol):
    def get(
        self, proposal_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> HandoffProposal | None:
        """Load one proposal in one workspace."""
        raise NotImplementedError

    def find_prepared_for_worker_item(
        self,
        worker_item_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffProposal | None:
        """Load the sole prepared proposal."""
        raise NotImplementedError

    def add(self, proposal: HandoffProposal) -> None:
        """Insert one proposal."""
        raise NotImplementedError

    def save_status(self, proposal: HandoffProposal, previous_version: int) -> None:
        """Persist lifecycle fields only using CAS."""
        raise NotImplementedError


class HandoffTransitionRepositoryPort(Protocol):
    def get(
        self, transition_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> HandoffTransition | None:
        """Load one transition."""
        raise NotImplementedError

    def get_active_for_worker_item(
        self,
        worker_item_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffTransition | None:
        """Load the active owner for one WorkerItem."""
        raise NotImplementedError

    def get_active_for_worker_items(
        self,
        worker_item_ids: tuple[UUID, ...],
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[HandoffTransition, ...]:
        """Load active owners for sorted WorkerItem IDs."""
        raise NotImplementedError

    def claim_next(
        self,
        *,
        owner_id: str,
        now: datetime,
        limit: int,
        statuses: tuple[HandoffTransitionStatus, ...] | None = None,
        lease_ttl_seconds: int | None = None,
    ) -> tuple[HandoffTransition, ...]:
        """Claim due transitions with SKIP LOCKED."""
        raise NotImplementedError

    def add(self, transition: HandoffTransition) -> None:
        """Insert one transition."""
        raise NotImplementedError

    def save(self, transition: HandoffTransition, previous_version: int) -> None:
        """Persist mutable coordinator fields using CAS."""
        raise NotImplementedError

    def save_recovery(
        self, transition: HandoffTransition, previous_version: int
    ) -> None:
        """Persist an evidence-backed recovery that may replace the target facts."""
        raise NotImplementedError


class HandoffContextBundleRepositoryPort(Protocol):
    def get(
        self, bundle_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> HandoffContextBundle | None:
        raise NotImplementedError

    def get_by_attempt(
        self,
        transition_id: UUID,
        logical_attempt: int,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffContextBundle | None:
        raise NotImplementedError

    def add_append_only(self, bundle: HandoffContextBundle) -> None:
        raise NotImplementedError

    def list_for_transition(
        self, transition_id: UUID, workspace_id: UUID
    ) -> tuple[HandoffContextBundle, ...]:
        raise NotImplementedError


class HandoffRecoveryDecisionRepositoryPort(Protocol):
    def get(
        self, decision_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> HandoffRecoveryDecision | None:
        raise NotImplementedError

    def add_append_only(
        self, decision: HandoffRecoveryDecision, workspace_id: UUID
    ) -> None:
        raise NotImplementedError

    def list_for_transition(
        self, transition_id: UUID, workspace_id: UUID
    ) -> tuple[HandoffRecoveryDecision, ...]:
        raise NotImplementedError


class HandoffWorkspaceTransferRepositoryPort(Protocol):
    def get(
        self, transfer_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> HandoffWorkspaceTransfer | None:
        raise NotImplementedError

    def get_by_attempt(
        self,
        transition_id: UUID,
        logical_attempt: int,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffWorkspaceTransfer | None:
        raise NotImplementedError

    def get_by_operation(
        self, operation_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> HandoffWorkspaceTransfer | None:
        raise NotImplementedError

    def add_append_only(
        self, transfer: HandoffWorkspaceTransfer, workspace_id: UUID
    ) -> None:
        raise NotImplementedError

    def save(self, transfer: HandoffWorkspaceTransfer, previous_version: int) -> None:
        """Persist mutable outcome fields while retaining immutable provenance."""
        raise NotImplementedError

    def list_for_transition(
        self, transition_id: UUID, workspace_id: UUID
    ) -> tuple[HandoffWorkspaceTransfer, ...]:
        raise NotImplementedError


# Keep the shorter name available to application code that treats the table as
# a workspace-transfer aggregate rather than an adapter handoff detail.
WorkspaceTransferRepositoryPort = HandoffWorkspaceTransferRepositoryPort


class AttentionAcknowledgementRepositoryPort(Protocol):
    def get_current_source(
        self, root_task_id: UUID, workspace_id: UUID, attention_key: str
    ) -> AttentionSource | None:
        raise NotImplementedError

    def get(
        self,
        workspace_id: UUID,
        actor_type: str,
        actor_id: str,
        attention_key: str,
    ) -> AttentionAcknowledgement | None:
        raise NotImplementedError

    def list_for_root(
        self,
        root_task_id: UUID,
        workspace_id: UUID,
        actor_type: str,
        actor_id: str,
    ) -> tuple[AttentionAcknowledgement, ...]:
        raise NotImplementedError

    def upsert(self, acknowledgement: AttentionAcknowledgement) -> None:
        raise NotImplementedError


class ProjectionChangePublisherPort(Protocol):
    def changed(
        self,
        *,
        workspace_id: UUID,
        projection_type: str,
        resource_id: UUID | None,
        change_type: str,
        resource_version: int | None,
        correlation_id: UUID,
    ) -> ProjectionChange:
        raise NotImplementedError

    def latest_cursor(self, workspace_id: UUID) -> int:
        raise NotImplementedError

    def latest_target_cursor(
        self, workspace_id: UUID, projection_type: str, resource_id: UUID
    ) -> int:
        raise NotImplementedError

    def oldest_cursor(self, workspace_id: UUID) -> int | None:
        raise NotImplementedError


class NotificationObservationStore(Protocol):
    def list_after(
        self, workspace_id: UUID, *, cursor: int, limit: int
    ) -> tuple[ProjectionChange, ...]:
        raise NotImplementedError

    def observe(self, change: ProjectionChange) -> NotificationObservation | None:
        raise NotImplementedError

    def list_route_backfill_candidates(
        self, workspace_id: UUID, *, now: datetime, window: timedelta
    ) -> tuple[NotificationRouteBackfillCandidate, ...]:
        raise NotImplementedError

    def list_baseline_candidates(
        self,
        workspace_id: UUID,
        *,
        source_cursor: int,
        now: datetime,
    ) -> tuple[NotificationBaselineCandidate, ...]:
        raise NotImplementedError


class WorkerRepositoryPort(Protocol):
    def get(
        self, worker_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> Worker | None:
        raise NotImplementedError

    def add(self, worker: Worker) -> None:
        raise NotImplementedError

    def save(self, worker: Worker, previous_version: int) -> None:
        raise NotImplementedError

    def list_for_workspace(self, workspace_id: UUID) -> tuple[Worker, ...]:
        raise NotImplementedError

    def lock_candidates(
        self,
        *,
        workspace_id: UUID,
        required_capabilities: frozenset[str],
        pinned_worker_id: UUID | None,
        heartbeat_cutoff: datetime,
    ) -> tuple[Worker, ...]:
        raise NotImplementedError

    def claim_stale(
        self, *, heartbeat_cutoff: datetime, limit: int
    ) -> tuple[Worker, ...]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class WorkerItemPlacement:
    plan_snapshot_id: UUID
    plan_generation: int
    workspace_id: UUID
    root_task_id: UUID
    worker_item_id: UUID
    worker_item_key: str
    subtask_key: str
    subtask_task_id: UUID | None
    role: str
    capability_requirements: tuple[str, ...]
    worker_policy_ref: str | None
    configuration_ref: str | None
    created_at: datetime


class WorkerItemRepositoryPort(Protocol):
    def get(
        self, item_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> WorkerItem | None:
        raise NotImplementedError

    def add(self, item: WorkerItem) -> None:
        raise NotImplementedError

    def save(self, item: WorkerItem, previous_version: int) -> None:
        raise NotImplementedError

    def get_by_stable_key(
        self, root_task_id: UUID, stable_key: str
    ) -> WorkerItem | None:
        raise NotImplementedError

    def add_placement(self, placement: WorkerItemPlacement) -> None:
        raise NotImplementedError

    def get_placement(
        self, plan_snapshot_id: UUID, worker_item_id: UUID
    ) -> WorkerItemPlacement | None:
        raise NotImplementedError

    def get_current_placement(
        self, worker_item_id: UUID, workspace_id: UUID
    ) -> WorkerItemPlacement | None:
        raise NotImplementedError

    def list_placements(
        self, worker_item_id: UUID, workspace_id: UUID
    ) -> tuple[WorkerItemPlacement, ...]:
        raise NotImplementedError

    def list_for_subtask(
        self, plan_snapshot_id: UUID, subtask_key: str
    ) -> tuple[WorkerItemPlacement, ...]:
        raise NotImplementedError


class ExecutionRepositoryPort(Protocol):
    def get(
        self,
        execution_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> Execution | None:
        raise NotImplementedError

    def add(self, execution: Execution) -> None:
        raise NotImplementedError

    def save(self, execution: Execution, previous_version: int) -> None:
        raise NotImplementedError

    def attempt_exists(
        self, lineage_id: UUID, attempt: int, workspace_id: UUID | None = None
    ) -> bool:
        raise NotImplementedError

    def list_for_lineage(
        self, lineage_id: UUID, workspace_id: UUID
    ) -> tuple[Execution, ...]:
        raise NotImplementedError

    def get_by_handoff_transition(
        self,
        transition_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> Execution | None:
        raise NotImplementedError

    def list_for_worker_item(
        self, worker_item_id: UUID, workspace_id: UUID
    ) -> tuple[Execution, ...]:
        raise NotImplementedError

    def list_for_tasks(
        self, task_ids: Collection[UUID], workspace_id: UUID
    ) -> tuple[Execution, ...]:
        raise NotImplementedError

    def claim_schedulable(
        self,
        *,
        now: datetime,
        limit: int,
        excluded_ids: Collection[UUID] = (),
        workspace_id: UUID | None = None,
        target_execution_id: UUID | None = None,
    ) -> tuple[Execution, ...]:
        raise NotImplementedError

    def has_live_for_task(
        self, task_id: UUID, *, excluding: UUID | None = None
    ) -> bool:
        raise NotImplementedError

    def list_ids_for_task_window(
        self,
        task_id: UUID,
        workspace_id: UUID,
        start: datetime,
        end: datetime | None,
    ) -> tuple[UUID, ...]:
        raise NotImplementedError

    def assign_worker_item_if_unset(
        self, execution_id: UUID, workspace_id: UUID, worker_item_id: UUID
    ) -> bool:
        raise NotImplementedError


class LeaseStorePort(Protocol):
    def get(
        self, lease_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> AssignmentLease | None:
        raise NotImplementedError

    def get_active_for_execution(
        self,
        execution_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> AssignmentLease | None:
        raise NotImplementedError

    def add(self, lease: AssignmentLease) -> None:
        raise NotImplementedError

    def save(self, lease: AssignmentLease, previous_version: int) -> None:
        raise NotImplementedError

    def claim_expired(
        self, *, now: datetime, limit: int
    ) -> tuple[AssignmentLease, ...]:
        raise NotImplementedError


class SessionRepositoryPort(Protocol):
    def list_active(self, workspace_id: UUID) -> tuple[ExecutionSession, ...]:
        raise NotImplementedError

    def get(
        self, session_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ExecutionSession | None:
        raise NotImplementedError

    def get_any(
        self, session_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ExecutionSession | None:
        raise NotImplementedError

    def get_active_for_execution(
        self, execution_id: UUID, *, for_update: bool = False
    ) -> ExecutionSession | None:
        raise NotImplementedError

    def list_active_for_execution(
        self, execution_id: UUID, workspace_id: UUID
    ) -> tuple[ExecutionSession, ...]:
        raise NotImplementedError

    def get_latest_for_execution(
        self,
        execution_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ExecutionSession | None:
        raise NotImplementedError

    def add(self, session: ExecutionSession) -> None:
        raise NotImplementedError

    def save(self, session: ExecutionSession, previous_version: int) -> None:
        raise NotImplementedError


class ModuleRepositoryPort(Protocol):
    def get_definition(
        self, module_id: UUID, workspace_id: UUID
    ) -> ModuleDefinition | None:
        raise NotImplementedError

    def get_instance(
        self, module_id: UUID, workspace_id: UUID
    ) -> ModuleInstance | None:
        raise NotImplementedError

    def get_instances(
        self,
        module_ids: Collection[UUID],
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[ModuleInstance, ...]:
        raise NotImplementedError

    def get_definitions(
        self,
        module_ids: Collection[UUID],
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[ModuleDefinition, ...]:
        raise NotImplementedError

    def add_definition(self, definition: ModuleDefinition) -> None:
        raise NotImplementedError

    def add_instance(self, instance: ModuleInstance) -> None:
        raise NotImplementedError

    def save_definition(
        self, definition: ModuleDefinition, previous_version: int
    ) -> None:
        raise NotImplementedError

    def save_instance(self, instance: ModuleInstance, previous_version: int) -> None:
        raise NotImplementedError


class WorkerRuntimeConfigurationRepositoryPort(Protocol):
    def get_for_worker(
        self,
        worker_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkerRuntimeConfiguration | None:
        raise NotImplementedError

    def get_for_workers(
        self,
        worker_ids: Collection[UUID],
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[WorkerRuntimeConfiguration, ...]:
        raise NotImplementedError

    def add(self, configuration: WorkerRuntimeConfiguration) -> None:
        raise NotImplementedError

    def save(
        self,
        configuration: WorkerRuntimeConfiguration,
        previous_version: int,
    ) -> None:
        raise NotImplementedError


class SchedulingBlockRepositoryPort(Protocol):
    def get_active_for_execution(
        self,
        execution_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> SchedulingBlock | None:
        raise NotImplementedError

    def add(self, block: SchedulingBlock) -> None:
        raise NotImplementedError

    def save(self, block: SchedulingBlock, previous_version: int) -> None:
        raise NotImplementedError


class ExecutionBindingRepositoryPort(Protocol):
    def retire_not_admitted(self, binding: ExecutionBinding, pin) -> None:
        raise NotImplementedError

    def get_for_execution(
        self, execution_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ExecutionBinding | None:
        raise NotImplementedError

    def add(self, binding: ExecutionBinding) -> None:
        raise NotImplementedError


class GatewayOperationRepositoryPort(Protocol):
    def get_by_operation_id(
        self, operation_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> GatewayOperation | None:
        raise NotImplementedError

    def add(self, operation: GatewayOperation) -> None:
        raise NotImplementedError

    def save(self, operation: GatewayOperation, previous_version: int) -> None:
        raise NotImplementedError

    def list_for_execution(
        self, execution_id: UUID, workspace_id: UUID
    ) -> tuple[GatewayOperation, ...]:
        raise NotImplementedError


class WorkspaceRepositoryPort(Protocol):
    def get(
        self, manifest_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ExecutionWorkspaceManifest | None:
        raise NotImplementedError

    def get_for_execution(
        self, execution_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ExecutionWorkspaceManifest | None:
        raise NotImplementedError

    def add(self, manifest: ExecutionWorkspaceManifest) -> None:
        raise NotImplementedError

    def save(self, manifest: ExecutionWorkspaceManifest, previous_version: int) -> None:
        raise NotImplementedError


class ExecutionEventRepositoryPort(Protocol):
    def add(self, event: ExecutionEvent) -> None:
        raise NotImplementedError

    def get(
        self, session_id: UUID, sequence: int, workspace_id: UUID
    ) -> ExecutionEvent | None:
        raise NotImplementedError

    def list_for_session(
        self,
        session_id: UUID,
        workspace_id: UUID,
    ) -> tuple[ExecutionEvent, ...]:
        raise NotImplementedError


class ArtifactRepositoryPort(Protocol):
    def add(self, artifact: Artifact) -> None:
        raise NotImplementedError

    def list_for_execution(
        self, execution_id: UUID, workspace_id: UUID
    ) -> tuple[Artifact, ...]:
        raise NotImplementedError

    def list_for_executions(
        self, execution_ids: Collection[UUID], workspace_id: UUID
    ) -> tuple[Artifact, ...]:
        raise NotImplementedError


class ArtifactContentWriter(Protocol):
    def put(
        self,
        workspace_id: UUID,
        execution_id: UUID,
        expected_hash: str,
        content: bytes,
    ) -> str:
        raise NotImplementedError


class ResultInputContentPort(Protocol):
    def open_exact(
        self,
        *,
        workspace_id: UUID,
        artifact_id: UUID,
        expected_hash: str,
        expected_size_bytes: int,
    ) -> BinaryIO:
        """Open one authorized artifact without accepting a storage reference."""
        raise NotImplementedError


class EvidenceClaimRepositoryPort(Protocol):
    def add(self, claim: EvidenceClaim) -> None:
        raise NotImplementedError

    def list_for_task(
        self, task_id: UUID, workspace_id: UUID
    ) -> tuple[EvidenceClaim, ...]:
        raise NotImplementedError

    def list_for_executions(
        self, execution_ids: Collection[UUID], workspace_id: UUID
    ) -> tuple[EvidenceClaim, ...]:
        raise NotImplementedError


class ExecutionInputRepositoryPort(Protocol):
    def get(
        self, input_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ExecutionInput | None:
        raise NotImplementedError

    def get_pending_for_session(
        self, session_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ExecutionInput | None:
        raise NotImplementedError

    def list_for_session(
        self,
        session_id: UUID,
        workspace_id: UUID,
        *,
        before: datetime | None = None,
        for_update: bool = False,
    ) -> tuple[ExecutionInput, ...]:
        """List inputs in a Session through a bounded observation time."""
        raise NotImplementedError

    def list_for_handoff_transition(
        self,
        transition_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[ExecutionInput, ...]:
        """List derived inputs for one handoff transition."""
        raise NotImplementedError

    def find_by_client_idempotency_key(
        self,
        session_id: UUID,
        workspace_id: UUID,
        client_idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> ExecutionInput | None:
        raise NotImplementedError

    def claim_queued(self, limit: int) -> tuple[ExecutionInput, ...]:
        raise NotImplementedError

    def add(self, item: ExecutionInput) -> None:
        raise NotImplementedError

    def save(self, item: ExecutionInput, previous_version: int) -> None:
        raise NotImplementedError


class WorkerAdapter(Protocol):
    def probe(self, config: Mapping[str, object]) -> WorkerAdapterCapabilityProfile:
        raise NotImplementedError

    def accept_context(
        self,
        materialization: ContextMaterialization,
        *,
        operation_id: UUID,
    ) -> ContextAcknowledgement:
        raise NotImplementedError

    def start_session(
        self,
        spec: ExecutionSpec,
        workspace: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
    ) -> AdapterSessionHandle:
        raise NotImplementedError

    def read_events(self, handle: AdapterSessionHandle) -> Iterable[AdapterEvent]:
        raise NotImplementedError

    def send_input(
        self,
        handle: AdapterSessionHandle,
        text: str,
        attachment_refs: tuple[str, ...],
        *,
        operation_id: UUID,
    ) -> DeliveryResult:
        raise NotImplementedError

    def apply_context_patch(
        self,
        handle: AdapterSessionHandle,
        materialization: ContextPatchMaterializationV1,
        *,
        operation_id: UUID,
    ) -> ContextPatchDeliveryResult:
        raise NotImplementedError

    def stop_session(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> DeliveryResult:
        raise NotImplementedError

    def collect_artifacts(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> tuple[ArtifactCandidate, ...]:
        raise NotImplementedError

    def collect_knowledge_candidates(
        self,
        handle: AdapterSessionHandle,
        *,
        operation_id: UUID,
        worker_item_id: UUID | None = None,
        source_artifact_ids: tuple[UUID, ...] = (),
    ) -> tuple[KnowledgeCandidateProposalV1, ...]:
        raise NotImplementedError

    def capture_handoff_checkpoint(
        self,
        handle: AdapterSessionHandle,
        *,
        transition_id: UUID,
        source_input_cutoff_sequence: int,
        operation_id: UUID,
    ) -> HandoffCheckpointEvidence:
        raise NotImplementedError

    def inspect_handoff_operation(
        self,
        handle: AdapterSessionHandle,
        *,
        operation_id: UUID,
    ) -> HandoffOperationInspection:
        raise NotImplementedError

    def enable_handoff_writer(
        self,
        handle: AdapterSessionHandle,
        *,
        fencing_token: int,
        operation_id: UUID,
    ) -> DeliveryResult:
        raise NotImplementedError


class PlannerAdapter(Protocol):
    def probe(self, config: Mapping[str, object]) -> PlannerAdapterCapabilityProfile:
        raise NotImplementedError

    def accept_context(
        self,
        materialization: ContextMaterializationV1,
        *,
        operation_id: UUID,
    ) -> ContextAcknowledgement:
        raise NotImplementedError

    def plan(
        self,
        planner_input: PlannerInput,
        control: PlannerRunControl,
    ) -> PlannerResult:
        raise NotImplementedError

    def cancel(self, operation_id: UUID) -> None:
        raise NotImplementedError


class WorkspaceProvider(Protocol):
    def refence_not_admitted(
        self,
        manifest: ExecutionWorkspaceManifest,
        *,
        pin,
        binding: ExecutionBinding,
        physical_input: CodeInputReceipt | None,
        operation_id: UUID,
    ) -> ExecutionWorkspaceManifest:
        raise NotImplementedError

    def confirm_not_admitted_refence(
        self, persisted: ExecutionWorkspaceManifest, *, operation_id: UUID
    ) -> None:
        raise NotImplementedError

    def physical_directory(self, manifest: ExecutionWorkspaceManifest) -> Path:
        """Resolve an exact prepared manifest through provider-owned validation."""
        raise NotImplementedError

    def prepare(
        self, spec: ExecutionSpec, *, operation_id: UUID
    ) -> ExecutionWorkspaceManifest:
        raise NotImplementedError

    def materialize(
        self,
        *,
        workspace_manifest: ExecutionWorkspaceManifest,
        code_inputs: CodeInputManifestV1,
        operation_id: UUID,
    ) -> CodeInputReceipt:
        """Install one exact code-input manifest and return its typed receipt."""
        raise NotImplementedError

    def freeze(
        self, manifest: ExecutionWorkspaceManifest, *, operation_id: UUID
    ) -> ExecutionWorkspaceManifest:
        raise NotImplementedError

    def retain(
        self,
        manifest: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
    ) -> ExecutionWorkspaceManifest:
        raise NotImplementedError

    def release(
        self, manifest: ExecutionWorkspaceManifest, *, operation_id: UUID
    ) -> ExecutionWorkspaceManifest:
        raise NotImplementedError

    def inspect(self, location_ref: str) -> ExecutionWorkspaceManifest:
        raise NotImplementedError

    def transfer_handoff(
        self,
        source_manifest: ExecutionWorkspaceManifest,
        target: HandoffWorkspaceTargetSpec,
        *,
        transition_id: UUID,
        logical_attempt: int,
        operation_id: UUID,
    ) -> ExecutionWorkspaceManifest:
        raise NotImplementedError

    def inspect_handoff_transfer(
        self,
        *,
        transition_id: UUID,
        logical_attempt: int,
        operation_id: UUID,
    ) -> HandoffWorkspaceInspection:
        raise NotImplementedError


class CommandRepositoryPort(Protocol):
    def find_by_id(self, workspace_id: UUID, command_id: UUID) -> StoredCommand | None:
        raise NotImplementedError

    def find_duplicate(
        self,
        workspace_id: UUID,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> StoredCommand | None:
        raise NotImplementedError

    def record_applied(
        self,
        envelope: CommandEnvelope,
        request_hash: str,
        result: CommandResult,
    ) -> None:
        raise NotImplementedError

    def record_rejected(
        self,
        envelope: CommandEnvelope,
        request_hash: str,
        error_code: str,
        error_result: dict[str, object],
    ) -> None:
        raise NotImplementedError


class EventStorePort(Protocol):
    def append_with_outbox(self, event: EventData) -> None:
        raise NotImplementedError


class NotificationLedger(Protocol):
    def get_policy_state(
        self,
        workspace_id: UUID,
        recipient_type: str,
        recipient_id: str,
        subject_type: str,
        subject_id: str,
        topic: str,
        *,
        for_update: bool = False,
        policy_version: int | None = None,
    ) -> NotificationPolicyState | None:
        raise NotImplementedError

    def list_due_policy_states(
        self,
        workspace_id: UUID,
        now: datetime,
        limit: int,
        *,
        for_update_skip_locked: bool = False,
        policy_version: int | None = None,
    ) -> tuple[NotificationPolicyState, ...]:
        raise NotImplementedError

    def add_policy_state(self, state: NotificationPolicyState) -> None:
        raise NotImplementedError

    def save_policy_state(
        self, state: NotificationPolicyState, previous_version: int
    ) -> None:
        raise NotImplementedError

    def add_fact_or_get_existing(self, fact: NotificationFact) -> NotificationFact:
        raise NotImplementedError

    def get_fact(self, workspace_id: UUID, fact_id: UUID) -> NotificationFact | None:
        raise NotImplementedError

    def list_thread_facts(
        self,
        workspace_id: UUID,
        thread_id: UUID,
        *,
        cursor: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> tuple[NotificationFact, ...]:
        raise NotImplementedError

    def get_thread(
        self, workspace_id: UUID, thread_id: UUID, *, for_update: bool = False
    ) -> InboxThread | None:
        raise NotImplementedError

    def get_thread_by_key(
        self,
        workspace_id: UUID,
        recipient_type: str,
        recipient_id: str,
        thread_key: str,
        *,
        for_update: bool = False,
    ) -> InboxThread | None:
        raise NotImplementedError

    def add_thread(self, thread: InboxThread) -> None:
        raise NotImplementedError

    def save_thread(self, thread: InboxThread, previous_version: int) -> None:
        raise NotImplementedError

    def get_route(
        self, workspace_id: UUID, route_id: UUID, *, for_update: bool = False
    ) -> NotificationRoute | None:
        raise NotImplementedError

    def get_route_by_source_ref(
        self,
        workspace_id: UUID,
        channel_module_instance_id: UUID,
        source_ref: str,
        *,
        for_update: bool = False,
    ) -> NotificationRoute | None:
        raise NotImplementedError

    def list_routes_for_source_ref(
        self, workspace_id: UUID, source_ref: str, *, for_update: bool = False
    ) -> tuple[NotificationRoute, ...]:
        raise NotImplementedError

    def add_route(self, route: NotificationRoute) -> None:
        raise NotImplementedError

    def save_route(self, route: NotificationRoute, previous_version: int) -> None:
        raise NotImplementedError

    def list_routes_for_recipient(
        self, workspace_id: UUID, recipient_type: str, recipient_id: str
    ) -> tuple[NotificationRoute, ...]:
        raise NotImplementedError

    def list_routes_for_resource(
        self, workspace_id: UUID, resource_type: str, resource_id: UUID
    ) -> tuple[NotificationRoute, ...]:
        raise NotImplementedError

    def add_binding_or_get_existing(
        self, binding: NotificationSourceBinding
    ) -> NotificationSourceBinding:
        raise NotImplementedError

    def get_binding(
        self,
        workspace_id: UUID,
        resource_type: str,
        resource_id: UUID,
        binding_kind: str,
    ) -> NotificationSourceBinding | None:
        raise NotImplementedError

    def copy_origin_binding(
        self,
        workspace_id: UUID,
        source_resource_type: str,
        source_resource_id: UUID,
        target_resource_type: str,
        target_resource_id: UUID,
    ) -> NotificationSourceBinding | None:
        raise NotImplementedError

    def add_intent_or_get_existing(
        self, intent: NotificationDeliveryIntent
    ) -> NotificationDeliveryIntent:
        raise NotImplementedError

    def get_intent(
        self, workspace_id: UUID, intent_id: UUID, *, for_update: bool = False
    ) -> NotificationDeliveryIntent | None:
        raise NotImplementedError

    def save_intent(
        self,
        intent: NotificationDeliveryIntent,
        previous_fencing_token: int,
        expected_status: NotificationDeliveryStatus | str,
    ) -> None:
        raise NotImplementedError

    def list_due_intents(
        self,
        workspace_id: UUID,
        channel_module_instance_id: UUID,
        now: datetime,
        limit: int,
        *,
        for_update_skip_locked: bool = False,
    ) -> tuple[NotificationDeliveryIntent, ...]:
        raise NotImplementedError

    def list_expired_claims(
        self,
        workspace_id: UUID,
        now: datetime,
        limit: int,
        *,
        for_update_skip_locked: bool = False,
    ) -> tuple[NotificationDeliveryIntent, ...]:
        raise NotImplementedError

    def add_attempt(
        self, workspace_id: UUID, attempt: NotificationDeliveryAttempt
    ) -> None:
        raise NotImplementedError

    def finish_attempt(
        self, workspace_id: UUID, attempt: NotificationDeliveryAttempt
    ) -> None:
        raise NotImplementedError

    def list_attempts(
        self, workspace_id: UUID, intent_id: UUID
    ) -> tuple[NotificationDeliveryAttempt, ...]:
        raise NotImplementedError

    def next_delivery_round(
        self, intent: NotificationDeliveryIntent, *, now: datetime
    ) -> NotificationDeliveryIntent:
        raise NotImplementedError

    def get_or_create_cursor(
        self, workspace_id: UUID, policy_version: int, *, now: datetime
    ) -> NotificationReconciliationCursor:
        raise NotImplementedError

    def claim_cursor(
        self,
        workspace_id: UUID,
        policy_version: int,
        owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> NotificationReconciliationCursor | None:
        raise NotImplementedError

    def save_cursor(
        self,
        cursor: NotificationReconciliationCursor,
        previous_version: int,
        expected_owner: str,
    ) -> None:
        raise NotImplementedError


NotificationLedgerPort = NotificationLedger


class AgentBindingRepositoryPort(Protocol):
    def get(
        self,
        workspace_id: UUID,
        target_type: str,
        target_id: UUID,
        *,
        for_update: bool = False,
    ) -> AgentBinding | None:
        raise NotImplementedError

    def append(self, binding: AgentBinding) -> None:
        raise NotImplementedError

    def history(
        self, workspace_id: UUID, target_type: str, target_id: UUID
    ) -> tuple[AgentBinding, ...]:
        raise NotImplementedError

    def execution_facts(
        self, workspace_id: UUID, target_type: str, target_id: UUID
    ) -> tuple[bool, frozenset[str]]:
        raise NotImplementedError

    def retirable_intentions(self, workspace_id, target_type, target_id):
        raise NotImplementedError


class ExecutionAuthorityRepositoryPort(Protocol):
    def save_preparation(
        self, expected, preparation: Mapping[str, object], *, now: datetime
    ):
        raise NotImplementedError

    def has_session(self, workspace_id: UUID, execution_id: UUID) -> bool:
        raise NotImplementedError

    def retirement_proof(self, workspace_id: UUID, execution_id: UUID):
        raise NotImplementedError

    def begin_start(self, expected, *, request_sha256: str, now: datetime):
        raise NotImplementedError

    def record_start(self, expected, receipt, *, now: datetime):
        raise NotImplementedError

    def lock_intention(self, expected, *, now: datetime | None = None):
        raise NotImplementedError

    def append_admission_history(self, pin):
        raise NotImplementedError

    def remove_archived_link(self, pin):
        raise NotImplementedError

    def admission_history(self, workspace_id: UUID, execution_id: UUID):
        raise NotImplementedError

    def list_intentions(self, workspace_id: UUID, *, limit: int = 100):
        raise NotImplementedError

    def get(
        self, authority_id: UUID, *, workspace_id: UUID | None = None
    ) -> ExecutionAuthority | None:
        raise NotImplementedError

    def add(self, authority: ExecutionAuthority) -> None:
        raise NotImplementedError

    def revoke(self, authority_id: UUID, workspace_id: UUID) -> None:
        raise NotImplementedError

    def prepared_location_ref(
        self, authority_id: UUID, workspace_id: UUID
    ) -> str | None:
        raise NotImplementedError

    def add_link(self, link: AgentExecutionLink) -> None:
        raise NotImplementedError

    def get_link(
        self,
        workspace_id: UUID,
        execution_id: UUID,
        *,
        for_update: bool = False,
    ) -> AgentExecutionLink | None:
        raise NotImplementedError

    def advance_link(
        self,
        *,
        workspace_id: UUID,
        execution_id: UUID,
        authority_id: UUID,
        run_id: UUID | None,
        event_cursor: int,
        status: str,
    ) -> AgentExecutionLink:
        raise NotImplementedError


class ControlPlaneUnitOfWork(Protocol):
    session: Session
    agent_bindings: AgentBindingRepositoryPort
    execution_authorities: ExecutionAuthorityRepositoryPort
    tasks: TaskRepositoryPort
    plans: PlanRepositoryPort
    plan_transitions: PlanTransitionRepositoryPort
    worker_transitions: WorkerTransitionRepositoryPort
    plan_entity_lineages: PlanEntityLineageRepositoryPort
    context_patches: ContextPatchRepositoryPort
    attention: AttentionAcknowledgementRepositoryPort
    projection_changes: ProjectionChangePublisherPort
    notification_observations: NotificationObservationStore
    workers: WorkerRepositoryPort
    worker_items: WorkerItemRepositoryPort
    executions: ExecutionRepositoryPort
    execution_requests: ExecutionRequestRepositoryPort
    collaboration_runs: CollaborationRunRepositoryPort
    collaboration_evaluations: CollaborationEvaluationRepositoryPort
    collaboration_ledger: CollaborationLedgerRepositoryPort
    input_selections: InputSelectionRepositoryPort
    collaboration_governance: CollaborationGovernanceRepositoryPort
    handoff_proposals: HandoffProposalRepositoryPort
    handoff_transitions: HandoffTransitionRepositoryPort
    handoff_context_bundles: HandoffContextBundleRepositoryPort
    handoff_recovery_decisions: HandoffRecoveryDecisionRepositoryPort
    handoff_workspace_transfers: HandoffWorkspaceTransferRepositoryPort
    workspace_transfers: HandoffWorkspaceTransferRepositoryPort
    leases: LeaseStorePort
    planning_profiles: PlanningProfileRepositoryPort
    planning_requests: PlanningRequestRepositoryPort
    planning_runs: PlanningRunRepositoryPort
    planning_leases: PlanningLeaseRepositoryPort
    planning_records: PlanningRecordRepositoryPort
    proposal_materializations: ProposalMaterializationRepositoryPort
    sessions: SessionRepositoryPort
    modules: ModuleRepositoryPort
    worker_runtime_configurations: WorkerRuntimeConfigurationRepositoryPort
    scheduling_blocks: SchedulingBlockRepositoryPort
    bindings: ExecutionBindingRepositoryPort
    gateway_operations: GatewayOperationRepositoryPort
    workspaces: WorkspaceRepositoryPort
    execution_events: ExecutionEventRepositoryPort
    artifacts: ArtifactRepositoryPort
    evidence_claims: EvidenceClaimRepositoryPort
    execution_inputs: ExecutionInputRepositoryPort
    commands: CommandRepositoryPort
    events: EventStorePort
    work_contexts: WorkContextRepositoryPort
    context_assemblies: ContextAssemblyRepositoryPort
    context_packages: ContextPackageRepositoryPort
    knowledge_candidate_submissions: KnowledgeCandidateSubmissionRepositoryPort
    notifications: NotificationLedger
    results: ResultRepositoryPort

    def flush(self) -> None:
        raise NotImplementedError

    def savepoint(self) -> AbstractContextManager[None]:
        raise NotImplementedError

    def __enter__(self) -> Self:
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError
