from collections.abc import Collection, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

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
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_item import WorkerItem
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

    def list_for_root(
        self, root_task_id: UUID, workspace_id: UUID
    ) -> tuple[TaskPlanSnapshot, ...]:
        raise NotImplementedError

    def list_roots_with_snapshots(self, workspace_id: UUID) -> tuple[UUID, ...]:
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

    def add_definition(self, definition: ModuleDefinition) -> None:
        raise NotImplementedError

    def add_instance(self, instance: ModuleInstance) -> None:
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

    def stop_session(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> DeliveryResult:
        raise NotImplementedError

    def collect_artifacts(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> tuple[ArtifactCandidate, ...]:
        raise NotImplementedError


class PlannerAdapter(Protocol):
    def probe(self, config: Mapping[str, object]) -> PlannerAdapterCapabilityProfile:
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
    bindings: ExecutionBindingRepositoryPort
    gateway_operations: GatewayOperationRepositoryPort
    workspaces: WorkspaceRepositoryPort
    execution_events: ExecutionEventRepositoryPort
    artifacts: ArtifactRepositoryPort
    evidence_claims: EvidenceClaimRepositoryPort
    execution_inputs: ExecutionInputRepositoryPort
    commands: CommandRepositoryPort
    events: EventStorePort

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
