from collections.abc import Collection, Iterable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from sidera_control_plane.domain.artifacts import (
    Artifact,
    ArtifactCandidate,
    EvidenceClaim,
)
from sidera_control_plane.domain.commands import (
    CommandEnvelope,
    CommandResult,
    StoredCommand,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution, ExecutionSession
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
    CapabilityProfile,
    ExecutionBinding,
    ModuleDefinition,
    ModuleInstance,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.projections import ProjectionChange
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
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

    def list_for_root(
        self, root_task_id: UUID, workspace_id: UUID
    ) -> tuple[TaskPlanSnapshot, ...]:
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
    def probe(self, config: Mapping[str, object]) -> CapabilityProfile:
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
    projection_changes: ProjectionChangePublisherPort
    workers: WorkerRepositoryPort
    executions: ExecutionRepositoryPort
    leases: LeaseStorePort
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
