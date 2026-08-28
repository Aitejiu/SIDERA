from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import TracebackType
from typing import Literal, Protocol, Self
from uuid import UUID

from sidera_control_plane.application.knowledge_candidate_submission import (
    KnowledgeCandidateProposalV1,
    KnowledgeCandidateSubmission,
)
from sidera_control_plane.application.planner_runtime import (
    PlannerInput,
    PlannerResult,
    PlannerRunControl,
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
from sidera_control_plane.domain.commands import (
    CommandEnvelope,
    CommandResult,
    StoredCommand,
)
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextAssemblyRequest,
    ContextMaterializationAudit,
    ContextMaterializationV1,
    ContextPackageManifestV1,
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
from sidera_control_plane.domain.inputs import ExecutionInput
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    ExecutionBinding,
    ModuleDefinition,
    ModuleInstance,
    PlannerAdapterCapabilityProfile,
    WorkerAdapterCapabilityProfile,
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
        manifest: ContextPackageManifestV1,
        *,
        owner: str,
        expected_version: int,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> ContextPackageManifestV1:
        raise NotImplementedError


class ContextPackageRepositoryPort(Protocol):
    def get(
        self, package_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ContextPackageManifestV1 | None:
        raise NotImplementedError

    def get_for_consumer(
        self,
        purpose: ContextPurpose,
        consumer_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ContextPackageManifestV1 | None:
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
    ) -> ContextPackageManifestV1:
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
    ) -> ContextPackageManifestV1:
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

    def save(self, submission: KnowledgeCandidateSubmission, previous_version: int) -> None:
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

    def save(self, request: ExecutionRequest, previous_version: int) -> None:
        raise NotImplementedError

    def claim_ready(
        self, *, now: datetime, limit: int
    ) -> tuple[ExecutionRequest, ...]:
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


class WorkerRepositoryPort(Protocol):
    def get(
        self, worker_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> Worker | None:
        raise NotImplementedError

    def add(self, worker: Worker) -> None:
        raise NotImplementedError

    def save(self, worker: Worker, previous_version: int) -> None:
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

    def attempt_exists(self, lineage_id: UUID, attempt: int) -> bool:
        raise NotImplementedError

    def list_for_lineage(
        self, lineage_id: UUID, workspace_id: UUID
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

    def add(self, lease: AssignmentLease) -> None:
        raise NotImplementedError

    def save(self, lease: AssignmentLease, previous_version: int) -> None:
        raise NotImplementedError

    def claim_expired(
        self, *, now: datetime, limit: int
    ) -> tuple[AssignmentLease, ...]:
        raise NotImplementedError


class SessionRepositoryPort(Protocol):
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


class WorkspaceRepositoryPort(Protocol):
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
        materialization: ContextMaterializationV1,
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
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> tuple[KnowledgeCandidateProposalV1, ...]:
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
    def prepare(
        self, spec: ExecutionSpec, *, operation_id: UUID
    ) -> ExecutionWorkspaceManifest:
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


class CommandRepositoryPort(Protocol):
    def find_by_id(
        self, workspace_id: UUID, command_id: UUID
    ) -> StoredCommand | None:
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


class ControlPlaneUnitOfWork(Protocol):
    tasks: TaskRepositoryPort
    plans: PlanRepositoryPort
    plan_transitions: PlanTransitionRepositoryPort
    worker_transitions: WorkerTransitionRepositoryPort
    plan_entity_lineages: PlanEntityLineageRepositoryPort
    context_patches: ContextPatchRepositoryPort
    attention: AttentionAcknowledgementRepositoryPort
    projection_changes: ProjectionChangePublisherPort
    workers: WorkerRepositoryPort
    worker_items: WorkerItemRepositoryPort
    executions: ExecutionRepositoryPort
    execution_requests: ExecutionRequestRepositoryPort
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
