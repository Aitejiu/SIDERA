from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
