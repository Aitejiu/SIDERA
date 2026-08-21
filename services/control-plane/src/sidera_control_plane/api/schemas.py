from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    model_validator,
)

from sidera_control_plane.application.resource_queries import (
    ArtifactView,
    EvidenceClaimView,
    ExecutionBindingView,
    ExecutionEventView,
    ExecutionInputView,
    ExecutionSessionView,
    ExecutionView,
    ExecutionWorkspaceView,
    LeaseView,
    WorkerView,
)
from sidera_control_plane.application.task_queries import (
    TaskPage,
    TaskView,
    TimelineItem,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.plans import TaskPlanSnapshot

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
InputText = Annotated[str, StringConstraints(min_length=1)]
AggregateType = Literal[
    "task", "worker", "execution", "execution_session", "lease"
]


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


class ArtifactContentDescriptorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    workspace_id: UUID
    media_type: str
    byte_size: int = Field(ge=0)
    content_hash: str
    download_name: NonEmptyString
    content_available: bool
    unavailable_reason: str | None


class DownloadTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: NonEmptyString


class PublishPlanSnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_current_generation: int = Field(ge=0)
    snapshot: dict[str, Any]


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


class ExecutionInputTargetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: UUID
    client_idempotency_key: NonEmptyString


class SendExecutionInputPayload(ExecutionInputTargetPayload):
    text: InputText
    attachment_refs: tuple[NonEmptyString, ...] = Field(default_factory=tuple)


class UpdatePendingExecutionInputPayload(ExecutionInputTargetPayload):
    text: InputText


class CancelExecutionInputPayload(ExecutionInputTargetPayload):
    pass


class RetryBlockedExecutionInputPayload(ExecutionInputTargetPayload):
    pass


class ContinueExecutionSessionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    not_before: datetime


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
            "PublishPlanSnapshot",
        }
        if self.command_type in task_commands and self.target_type != "task":
            raise ValueError("Task commands require target_type=task")
        return self._validate_payload()

    def _validate_payload(self) -> "CommandRequest":
        if self.command_type == "CreateTask":
            self._require_create("task")
            parsed: BaseModel = CreateTaskPayload.model_validate(self.payload)
        elif self.command_type == "PublishPlanSnapshot":
            self._require_mutation_target("task")
            parsed = PublishPlanSnapshotPayload.model_validate(self.payload)
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
        elif self.command_type in {"CancelExecution", "StopExecution"}:
            self._require_mutation_target("execution")
            parsed = EmptyPayload.model_validate(self.payload)
        elif self.command_type == "ContinueExecutionSession":
            self._require_mutation_target("execution_session")
            parsed = ContinueExecutionSessionPayload.model_validate(self.payload)
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
        elif self.command_type == "SendExecutionInput":
            self._require_mutation_target("execution_session")
            parsed = SendExecutionInputPayload.model_validate(self.payload)
        elif self.command_type == "UpdatePendingExecutionInput":
            self._require_mutation_target("execution_session")
            parsed = UpdatePendingExecutionInputPayload.model_validate(self.payload)
        elif self.command_type == "CancelExecutionInput":
            self._require_mutation_target("execution_session")
            parsed = CancelExecutionInputPayload.model_validate(self.payload)
        elif self.command_type == "RetryBlockedExecutionInput":
            self._require_mutation_target("execution_session")
            parsed = RetryBlockedExecutionInputPayload.model_validate(self.payload)
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


class WorkItemResponse(BaseModel):
    key: str
    task_id: UUID | None
    kind: str
    title: str
    goal_summary: str
    acceptance_refs: tuple[str, ...]
    milestone_key: str | None
    worker_slot_key: str | None
    expected_outputs: tuple[str, ...]
    source_refs: tuple[str, ...]


class WorkerSlotResponse(BaseModel):
    key: str
    role: str
    capability_requirements: tuple[str, ...]
    worker_policy_ref: str | None
    configuration_ref: str | None
    concurrency_limit: int | None


class MilestoneResponse(BaseModel):
    key: str
    title: str
    work_item_keys: tuple[str, ...]
    acceptance_refs: tuple[str, ...]
    order_hint: int


class DependencyResponse(BaseModel):
    upstream_work_item_key: str
    downstream_work_item_key: str
    kind: str
    required_evidence_refs: tuple[str, ...]
    enforcement: str


class TaskPlanSnapshotResponse(BaseModel):
    id: UUID
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    generation: int
    parent_snapshot_id: UUID | None
    source_type: str
    source_ref: str
    title: str
    goal_summary: str
    acceptance_refs: tuple[str, ...]
    work_items: tuple[WorkItemResponse, ...]
    worker_slots: tuple[WorkerSlotResponse, ...]
    milestones: tuple[MilestoneResponse, ...]
    dependencies: tuple[DependencyResponse, ...]
    published_by: str
    published_at: datetime

    @field_serializer("published_at")
    def serialize_published_at(self, value: datetime) -> str:
        return value.isoformat()

    @classmethod
    def from_snapshot(cls, snapshot: TaskPlanSnapshot) -> "TaskPlanSnapshotResponse":
        return cls(
            id=snapshot.id,
            schema_version=snapshot.schema_version,
            workspace_id=snapshot.workspace_id,
            root_task_id=snapshot.root_task_id,
            generation=snapshot.generation,
            parent_snapshot_id=snapshot.parent_snapshot_id,
            source_type=snapshot.source_type,
            source_ref=snapshot.source_ref,
            title=snapshot.title,
            goal_summary=snapshot.goal_summary,
            acceptance_refs=snapshot.acceptance_refs,
            work_items=tuple(
                WorkItemResponse(
                    key=item.key,
                    task_id=item.task_id,
                    kind=item.kind.value,
                    title=item.title,
                    goal_summary=item.goal_summary,
                    acceptance_refs=item.acceptance_refs,
                    milestone_key=item.milestone_key,
                    worker_slot_key=item.worker_slot_key,
                    expected_outputs=item.expected_outputs,
                    source_refs=item.source_refs,
                )
                for item in snapshot.work_items
            ),
            worker_slots=tuple(
                WorkerSlotResponse(
                    key=slot.key,
                    role=slot.role,
                    capability_requirements=slot.capability_requirements,
                    worker_policy_ref=slot.worker_policy_ref,
                    configuration_ref=slot.configuration_ref,
                    concurrency_limit=slot.concurrency_limit,
                )
                for slot in snapshot.worker_slots
            ),
            milestones=tuple(
                MilestoneResponse(
                    key=milestone.key,
                    title=milestone.title,
                    work_item_keys=milestone.work_item_keys,
                    acceptance_refs=milestone.acceptance_refs,
                    order_hint=milestone.order_hint,
                )
                for milestone in snapshot.milestones
            ),
            dependencies=tuple(
                DependencyResponse(
                    upstream_work_item_key=dependency.upstream_work_item_key,
                    downstream_work_item_key=dependency.downstream_work_item_key,
                    kind=dependency.kind.value,
                    required_evidence_refs=dependency.required_evidence_refs,
                    enforcement=dependency.enforcement.value,
                )
                for dependency in snapshot.dependencies
            ),
            published_by=snapshot.published_by,
            published_at=snapshot.published_at,
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
    stop_requested: bool
    source_execution_id: UUID | None
    source_session_id: UUID | None
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
            stop_requested=execution.stop_requested,
            source_execution_id=execution.source_execution_id,
            source_session_id=execution.source_session_id,
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


class ExecutionBindingResponse(BaseModel):
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

    @classmethod
    def from_view(cls, binding: ExecutionBindingView) -> "ExecutionBindingResponse":
        return cls(
            id=binding.id,
            workspace_id=binding.workspace_id,
            execution_id=binding.execution_id,
            worker_id=binding.worker_id,
            adapter_module_id=binding.adapter_module_id,
            workspace_module_id=binding.workspace_module_id,
            adapter_config_version=binding.adapter_config_version,
            workspace_config_version=binding.workspace_config_version,
            capability_snapshot=binding.capability_snapshot,
            lease_id=binding.lease_id,
            fencing_token=binding.fencing_token,
            version=binding.version,
            created_at=binding.created_at,
            updated_at=binding.updated_at,
        )


class ExecutionSessionResponse(BaseModel):
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

    @classmethod
    def from_view(cls, session: ExecutionSessionView) -> "ExecutionSessionResponse":
        return cls(
            id=session.id,
            workspace_id=session.workspace_id,
            execution_id=session.execution_id,
            worker_id=session.worker_id,
            adapter_session_id=session.adapter_session_id,
            status=session.status,
            last_sequence=session.last_sequence,
            last_activity_at=session.last_activity_at,
            input_mode=session.input_mode,
            adapter_module_id=session.adapter_module_id,
            adapter_config_version=session.adapter_config_version,
            workspace_module_id=session.workspace_module_id,
            workspace_config_version=session.workspace_config_version,
            capability_snapshot=session.capability_snapshot,
            workspace_ref=session.workspace_ref,
            version=session.version,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )


class ExecutionWorkspaceResponse(BaseModel):
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

    @classmethod
    def from_view(
        cls, workspace: ExecutionWorkspaceView
    ) -> "ExecutionWorkspaceResponse":
        return cls(
            id=workspace.id,
            workspace_id=workspace.workspace_id,
            task_id=workspace.task_id,
            execution_id=workspace.execution_id,
            checkouts=workspace.checkouts,
            location_ref=workspace.location_ref,
            lifecycle=workspace.lifecycle,
            retention_seconds=workspace.retention_seconds,
            retain_until=workspace.retain_until,
            fencing_token=workspace.fencing_token,
            frozen_at=workspace.frozen_at,
            released_at=workspace.released_at,
            version=workspace.version,
            created_at=workspace.created_at,
            updated_at=workspace.updated_at,
        )


class ExecutionEventResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    session_id: UUID
    sequence: int
    event_type: str
    payload: dict[str, object]
    occurred_at: datetime
    raw_ref: str | None

    @classmethod
    def from_view(cls, event: ExecutionEventView) -> "ExecutionEventResponse":
        return cls(
            id=event.id,
            workspace_id=event.workspace_id,
            execution_id=event.execution_id,
            session_id=event.session_id,
            sequence=event.sequence,
            event_type=event.event_type,
            payload=event.payload,
            occurred_at=event.occurred_at,
            raw_ref=event.raw_ref,
        )


class ArtifactMetadataResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    task_id: UUID
    execution_id: UUID
    session_id: UUID | None
    artifact_type: str
    content_hash: str
    size_bytes: int
    media_type: str
    provenance: dict[str, object]
    created_at: datetime

    @classmethod
    def from_view(cls, artifact: ArtifactView) -> "ArtifactMetadataResponse":
        return cls(
            id=artifact.id,
            workspace_id=artifact.workspace_id,
            task_id=artifact.task_id,
            execution_id=artifact.execution_id,
            session_id=artifact.session_id,
            artifact_type=artifact.artifact_type,
            content_hash=artifact.content_hash,
            size_bytes=artifact.size_bytes,
            media_type=artifact.media_type,
            provenance=artifact.provenance,
            created_at=artifact.created_at,
        )


class EvidenceClaimResponse(BaseModel):
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

    @classmethod
    def from_view(cls, claim: EvidenceClaimView) -> "EvidenceClaimResponse":
        return cls(
            id=claim.id,
            workspace_id=claim.workspace_id,
            task_id=claim.task_id,
            execution_id=claim.execution_id,
            acceptance_criterion_ref=claim.acceptance_criterion_ref,
            conclusion=claim.conclusion,
            environment_ref=claim.environment_ref,
            artifact_ids=claim.artifact_ids,
            producer=claim.producer,
            status=claim.status,
            created_at=claim.created_at,
        )


class ExecutionInputStatusResponse(BaseModel):
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

    @classmethod
    def from_view(
        cls, item: ExecutionInputView
    ) -> "ExecutionInputStatusResponse":
        return cls(
            id=item.id,
            workspace_id=item.workspace_id,
            execution_id=item.execution_id,
            session_id=item.session_id,
            client_idempotency_key=item.client_idempotency_key,
            status=item.status,
            operation_id=item.operation_id,
            failure_code=item.failure_code,
            version=item.version,
            text_length=item.text_length,
            attachment_count=item.attachment_count,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


class CursorPageResponse(BaseModel):
    next_cursor: str | None


class ExecutionSessionPageResponse(CursorPageResponse):
    items: tuple[ExecutionSessionResponse, ...]


class ExecutionEventPageResponse(CursorPageResponse):
    items: tuple[ExecutionEventResponse, ...]


class ArtifactPageResponse(CursorPageResponse):
    items: tuple[ArtifactMetadataResponse, ...]


class EvidenceClaimPageResponse(CursorPageResponse):
    items: tuple[EvidenceClaimResponse, ...]


class ExecutionInputPageResponse(CursorPageResponse):
    items: tuple[ExecutionInputStatusResponse, ...]


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
