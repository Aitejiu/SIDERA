from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sidera_control_plane.application.resource_queries import (
    ExecutionView,
    LeaseView,
    WorkerView,
)
from sidera_control_plane.application.task_queries import (
    TaskPage,
    TaskView,
    TimelineItem,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
AggregateType = Literal["task", "worker", "execution", "lease"]


class ActorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: NonEmptyString
    id: NonEmptyString


class CreateTaskPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: NonEmptyString
    description: str
    acceptance_criteria: tuple[NonEmptyString, ...] = Field(min_length=1)
    priority: int = Field(ge=0, le=100)


class SetTaskPriorityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority: int = Field(ge=0, le=100)


class EmptyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterWorkerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_type: NonEmptyString
    display_name: NonEmptyString
    capabilities: tuple[NonEmptyString, ...] = Field(min_length=1)
    capacity: int = Field(ge=1, le=1_000)
    cost_per_hour_microunits: int = Field(ge=0)
    startup_latency_ms: int = Field(ge=0)


class HeartbeatWorkerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    healthy: bool


class CreateExecutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    purpose: NonEmptyString
    capability_requirements: tuple[NonEmptyString, ...] = Field(min_length=1)
    pinned_worker_id: UUID | None = None
    max_attempts: int = Field(ge=1, le=10)
    not_before: datetime


class RetryExecutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    not_before: datetime


class LeaseOwnerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: UUID
    fencing_token: int = Field(ge=1)


class ReportExecutionStartedPayload(LeaseOwnerPayload):
    lease_id: UUID
    adapter_session_id: NonEmptyString


class ReportExecutionFinishedPayload(LeaseOwnerPayload):
    lease_id: UUID


class ReportExecutionFailedPayload(ReportExecutionFinishedPayload):
    failure_code: NonEmptyString = Field(max_length=100)


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    workspace_id: UUID
    command_type: NonEmptyString
    target_type: AggregateType
    actor: ActorRequest
    target_id: UUID | None
    expected_version: int | None
    idempotency_key: NonEmptyString
    correlation_id: UUID
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_command_shape(self) -> "CommandRequest":
        task_commands = {
            "CreateTask",
            "SetTaskPriority",
            "PauseTask",
            "ResumeTask",
            "CancelTask",
        }
        if self.command_type in task_commands and self.target_type != "task":
            raise ValueError("Task commands require target_type=task")
        return self._validate_payload()

    def _validate_payload(self) -> "CommandRequest":
        if self.command_type == "CreateTask":
            self._require_create("task")
            parsed: BaseModel = CreateTaskPayload.model_validate(self.payload)
        elif self.command_type == "SetTaskPriority":
            self._require_mutation_target()
            parsed = SetTaskPriorityPayload.model_validate(self.payload)
        elif self.command_type in {"PauseTask", "ResumeTask", "CancelTask"}:
            self._require_mutation_target()
            parsed = EmptyPayload.model_validate(self.payload)
        elif self.command_type == "RegisterWorker":
            self._require_create("worker")
            parsed = RegisterWorkerPayload.model_validate(self.payload)
        elif self.command_type == "HeartbeatWorker":
            self._require_mutation_target("worker")
            parsed = HeartbeatWorkerPayload.model_validate(self.payload)
        elif self.command_type == "DrainWorker":
            self._require_mutation_target("worker")
            parsed = EmptyPayload.model_validate(self.payload)
        elif self.command_type == "CreateExecution":
            self._require_create("execution")
            parsed = CreateExecutionPayload.model_validate(self.payload)
        elif self.command_type == "RetryExecution":
            self._require_mutation_target("execution")
            parsed = RetryExecutionPayload.model_validate(self.payload)
        elif self.command_type == "CancelExecution":
            self._require_mutation_target("execution")
            parsed = EmptyPayload.model_validate(self.payload)
        elif self.command_type == "RenewLease":
            self._require_mutation_target("lease")
            parsed = LeaseOwnerPayload.model_validate(self.payload)
        elif self.command_type == "ReportExecutionStarted":
            self._require_mutation_target("execution")
            parsed = ReportExecutionStartedPayload.model_validate(self.payload)
        elif self.command_type == "ReportExecutionSucceeded":
            self._require_mutation_target("execution")
            parsed = ReportExecutionFinishedPayload.model_validate(self.payload)
        elif self.command_type == "ReportExecutionFailed":
            self._require_mutation_target("execution")
            parsed = ReportExecutionFailedPayload.model_validate(self.payload)
        else:
            return self
        self.payload = dict(parsed.model_dump(mode="json"))
        return self

    def _require_create(self, target_type: AggregateType) -> None:
        if self.target_type != target_type:
            raise ValueError(f"create command requires target_type={target_type}")
        if self.target_id is not None or self.expected_version is not None:
            raise ValueError("create commands cannot target an existing aggregate")

    def _require_mutation_target(self, target_type: AggregateType | None = None) -> None:
        if target_type is not None and self.target_type != target_type:
            raise ValueError(f"mutation command requires target_type={target_type}")
        if self.target_id is None or self.expected_version is None:
            raise ValueError("mutation commands require target_id and expected_version")
        if self.expected_version < 1:
            raise ValueError("expected_version must be positive")

    def to_envelope(self) -> CommandEnvelope:
        return CommandEnvelope(
            id=self.command_id,
            workspace_id=self.workspace_id,
            command_type=self.command_type,
            target_type=self.target_type,
            actor_type=self.actor.type,
            actor_id=self.actor.id,
            target_id=self.target_id,
            payload=self.payload,
            expected_version=self.expected_version,
            idempotency_key=self.idempotency_key,
            correlation_id=self.correlation_id,
        )


class CommandResponse(BaseModel):
    command_id: UUID
    aggregate_id: UUID
    aggregate_version: int
    event_ids: tuple[UUID, ...]

    @classmethod
    def from_result(cls, result: CommandResult) -> "CommandResponse":
        return cls(
            command_id=result.command_id,
            aggregate_id=result.aggregate_id,
            aggregate_version=result.aggregate_version,
            event_ids=result.event_ids,
        )


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any]


class ErrorResponse(BaseModel):
    error: ErrorBody


class TaskResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    status: str
    priority: int
    requested_by: str
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, task: TaskView) -> "TaskResponse":
        return cls(
            id=task.id,
            workspace_id=task.workspace_id,
            title=task.title,
            description=task.description,
            acceptance_criteria=task.acceptance_criteria,
            status=task.status,
            priority=task.priority,
            requested_by=task.requested_by,
            version=task.version,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )


class WorkerResponse(BaseModel):
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

    @classmethod
    def from_view(cls, worker: WorkerView) -> "WorkerResponse":
        return cls(
            id=worker.id,
            workspace_id=worker.workspace_id,
            adapter_type=worker.adapter_type,
            display_name=worker.display_name,
            status=worker.status,
            desired_state=worker.desired_state,
            capabilities=tuple(sorted(worker.capabilities)),
            capacity=worker.capacity,
            current_load=worker.current_load,
            cost_per_hour_microunits=worker.cost_per_hour_microunits,
            startup_latency_ms=worker.startup_latency_ms,
            last_heartbeat_at=worker.last_heartbeat_at,
            version=worker.version,
            created_at=worker.created_at,
            updated_at=worker.updated_at,
        )


class ExecutionResponse(BaseModel):
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
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, execution: ExecutionView) -> "ExecutionResponse":
        return cls(
            id=execution.id,
            workspace_id=execution.workspace_id,
            task_id=execution.task_id,
            lineage_id=execution.lineage_id,
            attempt=execution.attempt,
            max_attempts=execution.max_attempts,
            purpose=execution.purpose,
            status=execution.status,
            capability_requirements=tuple(sorted(execution.capability_requirements)),
            pinned_worker_id=execution.pinned_worker_id,
            assigned_worker_id=execution.assigned_worker_id,
            active_lease_id=execution.active_lease_id,
            last_fencing_token=execution.last_fencing_token,
            not_before=execution.not_before,
            failure_code=execution.failure_code,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            version=execution.version,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
        )


class LeaseResponse(BaseModel):
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

    @classmethod
    def from_view(cls, lease: LeaseView) -> "LeaseResponse":
        return cls(
            id=lease.id,
            execution_id=lease.execution_id,
            worker_id=lease.worker_id,
            status=lease.status,
            issued_at=lease.issued_at,
            expires_at=lease.expires_at,
            renewed_at=lease.renewed_at,
            ended_at=lease.ended_at,
            fencing_token=lease.fencing_token,
            assignment_reason=lease.assignment_reason,
            version=lease.version,
        )


class TaskPageResponse(BaseModel):
    items: tuple[TaskResponse, ...]
    total: int
    limit: int
    offset: int

    @classmethod
    def from_page(cls, page: TaskPage) -> "TaskPageResponse":
        return cls(
            items=tuple(TaskResponse.from_view(item) for item in page.items),
            total=page.total,
            limit=page.limit,
            offset=page.offset,
        )


class ActorResponse(BaseModel):
    type: str
    id: str


class TimelineItemResponse(BaseModel):
    id: UUID
    event_type: str
    aggregate_version: int
    actor: ActorResponse
    payload: dict[str, Any]
    causation_id: UUID
    correlation_id: UUID
    created_at: datetime

    @classmethod
    def from_item(cls, item: TimelineItem) -> "TimelineItemResponse":
        return cls(
            id=item.id,
            event_type=item.event_type,
            aggregate_version=item.aggregate_version,
            actor=ActorResponse(type=item.actor_type, id=item.actor_id),
            payload=item.payload,
            causation_id=item.causation_id,
            correlation_id=item.correlation_id,
            created_at=item.created_at,
        )
