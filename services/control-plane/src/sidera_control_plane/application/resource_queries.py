from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeVar
from uuid import UUID

from sidera_control_plane.application.errors import NotFound


@dataclass(frozen=True, slots=True)
class WorkerView:
    id: UUID
    workspace_id: UUID
    adapter_type: str
    display_name: str
    status: str
    desired_state: str
    capabilities: tuple[str, ...]
    capacity: int
    current_load: int
    cost_per_hour_microunits: int
    startup_latency_ms: int
    last_heartbeat_at: datetime
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionView:
    id: UUID
    workspace_id: UUID
    task_id: UUID
    lineage_id: UUID
    attempt: int
    max_attempts: int
    purpose: str
    status: str
    capability_requirements: tuple[str, ...]
    pinned_worker_id: UUID | None
    assigned_worker_id: UUID | None
    active_lease_id: UUID | None
    last_fencing_token: int
    not_before: datetime
    failure_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    version: int
    stop_requested: bool
    source_execution_id: UUID | None
    source_session_id: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class LeaseView:
    id: UUID
    execution_id: UUID
    worker_id: UUID
    status: str
    issued_at: datetime
    expires_at: datetime
    renewed_at: datetime | None
    ended_at: datetime | None
    fencing_token: int
    assignment_reason: dict[str, object]
    version: int


@dataclass(frozen=True, slots=True)
class ExecutionBindingView:
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    worker_id: UUID
    adapter_module_id: UUID
    workspace_module_id: UUID
    adapter_config_version: int
    workspace_config_version: int
    capability_snapshot: dict[str, object]
    lease_id: UUID
    fencing_token: int
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionSessionView:
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    worker_id: UUID
    adapter_session_id: str
    status: str
    last_sequence: int
    last_activity_at: datetime
    input_mode: str
    adapter_module_id: UUID | None
    adapter_config_version: int | None
    workspace_module_id: UUID | None
    workspace_config_version: int | None
    capability_snapshot: dict[str, object] | None
    workspace_ref: str | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionWorkspaceView:
    id: UUID
    workspace_id: UUID
    task_id: UUID
    execution_id: UUID
    checkouts: tuple[dict[str, object], ...]
    location_ref: str
    lifecycle: str
    retention_seconds: int
    retain_until: datetime | None
    fencing_token: int
    frozen_at: datetime | None
    released_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionEventView:
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    session_id: UUID
    sequence: int
    event_type: str
    payload: dict[str, object]
    occurred_at: datetime
    raw_ref: str | None


@dataclass(frozen=True, slots=True)
class ArtifactView:
    id: UUID
    workspace_id: UUID
    task_id: UUID
    execution_id: UUID
    session_id: UUID | None
    artifact_type: str
    storage_ref: str
    content_hash: str
    size_bytes: int
    media_type: str
    provenance: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceClaimView:
    id: UUID
    workspace_id: UUID
    task_id: UUID
    execution_id: UUID
    acceptance_criterion_ref: str
    conclusion: str
    environment_ref: str
    artifact_ids: tuple[UUID, ...]
    producer: str
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionInputView:
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    session_id: UUID
    client_idempotency_key: str
    status: str
    operation_id: UUID | None
    failure_code: str | None
    version: int
    text_length: int
    attachment_count: int
    created_at: datetime
    updated_at: datetime


ViewT = TypeVar("ViewT")


@dataclass(frozen=True, slots=True)
class QueryPage[ViewT]:
    items: tuple[ViewT, ...]
    next_cursor: str | None


class ResourceQueryStore(Protocol):
    def list_workers(self, workspace_id: UUID) -> tuple[WorkerView, ...]:
        raise NotImplementedError

    def get_worker(self, workspace_id: UUID, worker_id: UUID) -> WorkerView | None:
        raise NotImplementedError

    def list_executions(
        self, workspace_id: UUID, task_id: UUID | None, status: str | None = None
    ) -> tuple[ExecutionView, ...]:
        raise NotImplementedError

    def get_execution(
        self, workspace_id: UUID, execution_id: UUID
    ) -> ExecutionView | None:
        raise NotImplementedError

    def list_leases(
        self, workspace_id: UUID, execution_id: UUID
    ) -> tuple[LeaseView, ...]:
        raise NotImplementedError

    def get_execution_binding(
        self, workspace_id: UUID, execution_id: UUID
    ) -> ExecutionBindingView | None:
        raise NotImplementedError

    def get_execution_workspace(
        self, workspace_id: UUID, execution_id: UUID
    ) -> ExecutionWorkspaceView | None:
        raise NotImplementedError

    def list_execution_sessions(
        self,
        workspace_id: UUID,
        *,
        execution_id: UUID | None,
        session_id: UUID | None,
        status: str | None,
        cursor: int,
        limit: int,
    ) -> QueryPage[ExecutionSessionView]:
        raise NotImplementedError

    def list_execution_events(
        self,
        workspace_id: UUID,
        *,
        execution_id: UUID | None,
        session_id: UUID | None,
        after_sequence: int,
        cursor: int,
        limit: int,
    ) -> QueryPage[ExecutionEventView]:
        raise NotImplementedError

    def list_artifacts(
        self,
        workspace_id: UUID,
        *,
        task_id: UUID | None,
        execution_id: UUID | None,
        session_id: UUID | None,
        cursor: int,
        limit: int,
    ) -> QueryPage[ArtifactView]:
        raise NotImplementedError

    def list_evidence_claims(
        self,
        workspace_id: UUID,
        *,
        task_id: UUID | None,
        execution_id: UUID | None,
        cursor: int,
        limit: int,
    ) -> QueryPage[EvidenceClaimView]:
        raise NotImplementedError

    def list_execution_inputs(
        self,
        workspace_id: UUID,
        *,
        execution_id: UUID | None,
        session_id: UUID | None,
        status: str | None,
        cursor: int,
        limit: int,
    ) -> QueryPage[ExecutionInputView]:
        raise NotImplementedError


class ResourceQueryService:
    def __init__(self, store: ResourceQueryStore) -> None:
        self._store = store

    def list_workers(self, workspace_id: UUID) -> tuple[WorkerView, ...]:
        return self._store.list_workers(workspace_id)

    def get_worker(self, workspace_id: UUID, worker_id: UUID) -> WorkerView:
        worker = self._store.get_worker(workspace_id, worker_id)
        if worker is None:
            raise NotFound("worker not found")
        return worker

    def list_executions(
        self,
        workspace_id: UUID,
        task_id: UUID | None = None,
        status: str | None = None,
    ) -> tuple[ExecutionView, ...]:
        return self._store.list_executions(workspace_id, task_id, status)

    def get_execution(self, workspace_id: UUID, execution_id: UUID) -> ExecutionView:
        execution = self._store.get_execution(workspace_id, execution_id)
        if execution is None:
            raise NotFound("execution not found")
        return execution

    def list_leases(
        self, workspace_id: UUID, execution_id: UUID
    ) -> tuple[LeaseView, ...]:
        self.get_execution(workspace_id, execution_id)
        return self._store.list_leases(workspace_id, execution_id)

    def get_execution_binding(
        self, workspace_id: UUID, execution_id: UUID
    ) -> ExecutionBindingView:
        binding = self._store.get_execution_binding(workspace_id, execution_id)
        if binding is None:
            raise NotFound("execution binding not found")
        return binding

    def get_execution_workspace(
        self, workspace_id: UUID, execution_id: UUID
    ) -> ExecutionWorkspaceView:
        workspace = self._store.get_execution_workspace(workspace_id, execution_id)
        if workspace is None:
            raise NotFound("execution workspace not found")
        return workspace

    def list_execution_sessions(
        self,
        workspace_id: UUID,
        *,
        execution_id: UUID | None = None,
        session_id: UUID | None = None,
        status: str | None = None,
        cursor: int = 0,
        limit: int = 50,
    ) -> QueryPage[ExecutionSessionView]:
        _validate_paging(cursor, limit)
        return self._store.list_execution_sessions(
            workspace_id,
            execution_id=execution_id,
            session_id=session_id,
            status=status,
            cursor=cursor,
            limit=limit,
        )

    def list_execution_events(
        self,
        workspace_id: UUID,
        *,
        execution_id: UUID | None = None,
        session_id: UUID | None = None,
        after_sequence: int = 0,
        cursor: int = 0,
        limit: int = 100,
    ) -> QueryPage[ExecutionEventView]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        _validate_paging(cursor, limit)
        return self._store.list_execution_events(
            workspace_id,
            execution_id=execution_id,
            session_id=session_id,
            after_sequence=after_sequence,
            cursor=cursor,
            limit=limit,
        )

    def list_artifacts(
        self,
        workspace_id: UUID,
        *,
        task_id: UUID | None = None,
        execution_id: UUID | None = None,
        session_id: UUID | None = None,
        cursor: int = 0,
        limit: int = 50,
    ) -> QueryPage[ArtifactView]:
        _validate_paging(cursor, limit)
        return self._store.list_artifacts(
            workspace_id,
            task_id=task_id,
            execution_id=execution_id,
            session_id=session_id,
            cursor=cursor,
            limit=limit,
        )

    def list_evidence_claims(
        self,
        workspace_id: UUID,
        *,
        task_id: UUID | None = None,
        execution_id: UUID | None = None,
        cursor: int = 0,
        limit: int = 50,
    ) -> QueryPage[EvidenceClaimView]:
        _validate_paging(cursor, limit)
        return self._store.list_evidence_claims(
            workspace_id,
            task_id=task_id,
            execution_id=execution_id,
            cursor=cursor,
            limit=limit,
        )

    def list_execution_inputs(
        self,
        workspace_id: UUID,
        *,
        execution_id: UUID | None = None,
        session_id: UUID | None = None,
        status: str | None = None,
        cursor: int = 0,
        limit: int = 50,
    ) -> QueryPage[ExecutionInputView]:
        _validate_paging(cursor, limit)
        return self._store.list_execution_inputs(
            workspace_id,
            execution_id=execution_id,
            session_id=session_id,
            status=status,
            cursor=cursor,
            limit=limit,
        )


def _validate_paging(cursor: int, limit: int) -> None:
    if cursor < 0:
        raise ValueError("cursor must be non-negative")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
