from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


class CommandEnvelope(BaseModel):
    """Command fields supplied by a Public API caller."""

    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    command_type: str
    target_type: str
    target_id: UUID | None
    expected_version: int | None
    idempotency_key: str
    correlation_id: UUID
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class CommandIntent(BaseModel):
    """Business fields used to create a new command envelope."""

    model_config = ConfigDict(extra="forbid")

    command_type: str
    target_type: str
    target_id: UUID | None
    expected_version: int | None
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class CommandResponse(BaseModel):
    """Safe applied command result returned by the control plane."""

    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    aggregate_id: UUID
    aggregate_version: int
    event_ids: tuple[UUID, ...]
    data: dict[str, JsonValue] = Field(default_factory=dict)


class CommandExecution(BaseModel):
    """Normalized result of a command POST."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["applied", "rejected"]
    command_id: UUID
    result: CommandResponse | None = None
    error_code: str | None = None


class CommandStatus(BaseModel):
    """Terminal or pending command status from the public query API."""

    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    status: Literal["pending", "applied", "rejected"]
    result: CommandResponse | None = None
    error_code: str | None = None
    command_type: str | None = None
    target_type: str | None = None
    target_id: UUID | None = None
    expected_version: int | None = None
    payload_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, JsonValue] | None = None


class NotificationDeliveryEnvelope(BaseModel):
    """Strict provider-neutral payload claimed by a channel Adapter."""

    model_config = ConfigDict(extra="forbid")

    delivery_id: UUID
    notification_id: UUID
    thread_id: UUID
    contract_version: int = Field(ge=1)
    claim_token: UUID
    fencing_token: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=512)
    channel_type: str = Field(min_length=1, max_length=64)
    source_ref: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=128)
    priority: str = Field(min_length=1, max_length=64)
    root_task_title: str = Field(min_length=1, max_length=300)
    subtask_path: tuple[str, ...] = Field(max_length=32)
    worker_item_title: str | None = Field(default=None, max_length=300)
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4_000)
    next_action: str = Field(min_length=1, max_length=500)
    deep_link: str = Field(min_length=1, max_length=1_000)
    correlation_id: str = Field(min_length=1, max_length=200)
    notification_ref: str = Field(min_length=1, max_length=128)
    created_at: datetime

    @field_validator(
        "idempotency_key",
        "channel_type",
        "source_ref",
        "kind",
        "priority",
        "root_task_title",
        "title",
        "summary",
        "next_action",
        "deep_link",
        "correlation_id",
        "notification_ref",
        "worker_item_title",
    )
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("notification delivery text must not be empty")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("notification delivery text contains control characters")
        return value.strip()

    @field_validator("subtask_path")
    @classmethod
    def validate_subtask_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not item.strip() or len(item) > 300:
                raise ValueError("subtask path item is outside supported bounds")
            if any(ord(character) < 32 or ord(character) == 127 for character in item):
                raise ValueError("subtask path contains control characters")
        return tuple(item.strip() for item in value)


NotificationDeliveryErrorCode = Literal[
    "transport_unavailable",
    "timeout",
    "rate_limited",
    "provider_unavailable",
    "unknown_result",
    "route_not_found",
    "route_revoked",
    "permission_denied",
    "contract_unsupported",
    "payload_rejected",
]


@dataclass(frozen=True, slots=True)
class MalformedNotificationDelivery:
    """Safe identity extracted from one invalid claim item for terminal nack."""

    delivery_id: UUID | None
    claim_token: UUID | None
    fencing_token: int | None
    error_code: NotificationDeliveryErrorCode = "payload_rejected"


class NotificationDeliverySettlement(BaseModel):
    """Strict response returned after one claim is settled."""

    model_config = ConfigDict(extra="forbid")

    delivery_id: UUID
    status: Literal[
        "pending",
        "claimed",
        "retry_wait",
        "delivered",
        "exhausted",
        "suppressed",
    ]
    attempt_number: int = Field(ge=1)
    outcome: Literal[
        "delivered", "retryable_failure", "terminal_failure", "unknown_result"
    ]
    provider_message_ref: str | None = Field(default=None, max_length=512)
    next_attempt_at: datetime | None
    fencing_token: int = Field(ge=1)
    error_code: str | None = Field(default=None, max_length=128)


class NotificationReadinessCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    ready: bool
    code: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=512)


class NotificationReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: str = Field(min_length=1, max_length=128)
    as_of: datetime
    status: Literal["ready", "not_ready"]
    checks: tuple[NotificationReadinessCheck, ...]


class NotificationModuleSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    instance_name: str = Field(min_length=1, max_length=300)
    provider: str = Field(min_length=1, max_length=128)
    module_type: str = Field(min_length=1, max_length=128)
    config_version: int = Field(ge=1)
    definition_lifecycle: str = Field(min_length=1, max_length=64)
    instance_lifecycle: str = Field(min_length=1, max_length=64)


def _validate_opaque_cursor(value: str | None) -> str | None:
    if value is None:
        return None
    if not value or len(value) > 512:
        raise ValueError("cursor must be between 1 and 512 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("cursor contains unsupported control characters")
    return value


NotificationCategory = Literal["action", "progress", "archived"]
NotificationBusinessStatus = Literal["open", "resolved"]
NotificationOrganizationStatus = Literal["active", "archived"]
NotificationCommandName = Literal["mark_read", "archive"]


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


class TaskResultOverviewView(BaseModel):
    """Strict outer contract for the bounded Task First result overview."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    root_task_id: UUID
    task_title: str = Field(min_length=1, max_length=300)
    task_status: str = Field(min_length=1, max_length=64)
    task_version: int = Field(ge=1)
    result_version: int | None = Field(default=None, ge=1)
    result_stage: str = Field(min_length=1, max_length=64)
    current_candidate: dict[str, JsonValue] | None
    acceptance: dict[str, JsonValue] | None
    active_operation: dict[str, JsonValue] | None
    criteria: tuple[dict[str, JsonValue], ...]
    blocking_reasons: tuple[str, ...]
    available_actions: tuple[dict[str, JsonValue], ...]
    candidate_history: dict[str, JsonValue]
    last_change_cursor: int = Field(ge=0)
    as_of: datetime


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
