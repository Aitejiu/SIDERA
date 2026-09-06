from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
ToolOutcome = Literal[
    "ok",
    "needs_user",
    "conflict",
    "unavailable",
    "result_unknown",
    "failed",
]

GovernanceAction = Literal["pause", "approve", "reject", "resume_automatic"]
GovernanceMode = Literal["automatic", "paused"]
GovernanceProposalStatus = Literal["pending", "approved", "rejected", "stale"]
NotificationCategory = Literal["action", "progress", "archived"]
NotificationBusinessStatus = Literal["open", "resolved"]
NotificationOrganizationStatus = Literal["active", "archived"]
NotificationCommandName = Literal["mark_read", "archive"]


def _validate_opaque_cursor(value: str | None) -> str | None:
    if value is None:
        return None
    if not value or len(value) > 512:
        raise ValueError("cursor must be between 1 and 512 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("cursor contains unsupported control characters")
    return value


class PlanRevisionBrief(BaseModel):
    """The bounded, provider-neutral brief accepted by a plan revision."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=4_000)
    constraints: tuple[str, ...] = Field(default_factory=tuple)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)


class PlanRevisionConfirmation(BaseModel):
    """The exact optimistic-concurrency fence for revision confirmation."""

    model_config = ConfigDict(extra="forbid")

    proposal_revision: int = Field(ge=1)
    proposal_content_hash: str = Field(min_length=1)
    diff_hash: str = Field(min_length=1)
    base_snapshot_id: UUID
    base_content_hash: str = Field(min_length=1)


class ProjectionChange(BaseModel):
    """Provider-neutral invalidation metadata from the Control Plane."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    cursor: int = Field(ge=1)
    schema_version: Literal[1]
    projection_type: str = Field(min_length=1)
    resource_id: UUID | None = None
    change_type: str = Field(min_length=1)
    resource_version: int | None = Field(default=None, ge=0)
    occurred_at: datetime
    correlation_id: UUID


class ToolEnvelope(BaseModel):
    """Stable, bounded result envelope returned by every bridge tool."""

    model_config = ConfigDict(extra="forbid")

    outcome: ToolOutcome
    summary: str
    resource_ref: str | None = None
    web_url: str | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)
    error_code: str | None = None

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("summary must not be empty")
        return value


class GovernanceWorkerItemView(BaseModel):
    """Strict outer contract for the bounded Control Plane governance view."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    root_task_id: UUID
    subtask_id: UUID | None
    subtask_key: str
    subtask_title: str
    worker_item_id: UUID
    worker_item_key: str
    worker_item_title: str
    worker_item_role: str
    worker_item_responsibility: str
    governance: dict[str, JsonValue]


class GovernanceProposalView(BaseModel):
    """Strict outer contract for one current release Proposal."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    root_task_id: UUID
    subtask_id: UUID | None
    subtask_key: str
    subtask_title: str
    worker_item_id: UUID
    worker_item_key: str
    worker_item_title: str
    proposal_id: UUID
    status: GovernanceProposalStatus
    resource_version: int = Field(ge=1)
    target_request_version: int = Field(ge=1)
    reason_code: str
    summary: str
    hold_id: UUID
    hold_resource_version: int = Field(ge=1)
    governance_mode: GovernanceMode
    available_actions: tuple[GovernanceAction, ...]
    deep_link: str
    # Older Control Plane fixtures omitted these optional audit timestamps.
    created_at: datetime | None = None
    resolved_at: datetime | None = None
    input_selection_id: UUID | None = None
    input_selection_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def validate_input_selection_identity(self) -> GovernanceProposalView:
        if (self.input_selection_id is None) != (
            self.input_selection_hash is None
        ):
            raise ValueError(
                "input selection id and hash must be provided together"
            )
        return self


class NotificationSummary(BaseModel):
    """Strict workspace/operator notification counters."""

    model_config = ConfigDict(extra="forbid")

    recipient_type: str = Field(min_length=1, max_length=64)
    recipient_id: str = Field(min_length=1, max_length=200)
    open_count: int = Field(ge=0)
    unread_count: int = Field(ge=0)
    progress_count: int = Field(ge=0)
    channel_issue_count: int = Field(ge=0)
    last_change_cursor: int = Field(ge=0)


class NotificationFact(BaseModel):
    """One immutable notification fact suitable for an agent handoff."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    thread_id: UUID
    policy_version: int = Field(ge=1)
    kind: str = Field(min_length=1, max_length=128)
    priority: str = Field(min_length=1, max_length=64)
    transition: str = Field(min_length=1, max_length=64)
    topic: str = Field(min_length=1, max_length=128)
    root_task_id: UUID
    subtask_id: UUID | None
    worker_item_id: UUID | None
    source_projection_cursor: int = Field(ge=0)
    source_projection_type: str = Field(min_length=1, max_length=128)
    source_resource_id: UUID
    source_resource_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4_000)
    action: str = Field(min_length=1, max_length=500)
    deep_link: str = Field(min_length=1, max_length=1_000)
    correlation_id: str = Field(min_length=1, max_length=200)
    baseline_import: bool
    occurred_at: datetime
    created_at: datetime


class NotificationDelivery(BaseModel):
    """Delivery state exposed for bounded channel troubleshooting."""

    model_config = ConfigDict(extra="forbid")

    delivery_id: UUID
    route_id: UUID
    channel_type: str = Field(min_length=1, max_length=64)
    contract_version: int = Field(ge=1)
    status: str = Field(min_length=1, max_length=64)
    attempts: int = Field(ge=0)
    next_attempt_at: datetime
    last_error_code: str | None = Field(default=None, max_length=128)
    delivered_at: datetime | None
    exhausted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class NotificationThread(BaseModel):
    """Current notification thread projection without provider internals."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    workspace_id: UUID
    recipient_type: str = Field(min_length=1, max_length=64)
    recipient_id: str = Field(min_length=1, max_length=200)
    category: NotificationCategory
    priority: str = Field(min_length=1, max_length=64)
    topic: str = Field(min_length=1, max_length=128)
    root_task_id: UUID
    root_task_title: str = Field(min_length=1, max_length=300)
    subtask_id: UUID | None
    subtask_title: str | None = Field(default=None, max_length=300)
    worker_item_id: UUID | None
    worker_item_title: str | None = Field(default=None, max_length=300)
    latest_fact_id: UUID
    latest_fact_occurred_at: datetime
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4_000)
    action: str = Field(min_length=1, max_length=500)
    deep_link: str = Field(min_length=1, max_length=1_000)
    notification_ref: str = Field(min_length=1, max_length=128)
    business_status: NotificationBusinessStatus
    organization_status: NotificationOrganizationStatus
    last_read_fact_id: UUID | None
    unread_count: int = Field(ge=0)
    version: int = Field(ge=1)
    opened_at: datetime
    resolved_at: datetime | None
    archived_at: datetime | None
    updated_at: datetime


class NotificationThreadPage(BaseModel):
    """Bounded page of notification threads."""

    model_config = ConfigDict(extra="forbid")

    items: tuple[NotificationThread, ...]
    next_cursor: str | None
    last_change_cursor: int = Field(ge=0)

    @field_validator("next_cursor")
    @classmethod
    def validate_next_cursor(cls, value: str | None) -> str | None:
        return _validate_opaque_cursor(value)


class NotificationFactPage(BaseModel):
    """Bounded page of immutable notification facts."""

    model_config = ConfigDict(extra="forbid")

    items: tuple[NotificationFact, ...]
    next_cursor: str | None

    @field_validator("next_cursor")
    @classmethod
    def validate_next_cursor(cls, value: str | None) -> str | None:
        return _validate_opaque_cursor(value)


class NotificationThreadDetail(BaseModel):
    """Thread detail with facts and the only notification commands allowed here."""

    model_config = ConfigDict(extra="forbid")

    thread: NotificationThread
    facts: NotificationFactPage
    deliveries: tuple[NotificationDelivery, ...]
    notification_ref: str = Field(min_length=1, max_length=128)
    available_commands: tuple[NotificationCommandName, ...]
    deep_link: str = Field(min_length=1, max_length=1_000)
    last_change_cursor: int = Field(ge=0)
