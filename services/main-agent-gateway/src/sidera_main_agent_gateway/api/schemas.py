from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )


class CreateThreadRequest(ApiModel):
    title: str | None = Field(default=None, max_length=120)
    context_refs: tuple[str, ...] = Field(
        default_factory=tuple,
        alias="contextRefs",
        max_length=20,
    )

    @field_validator("context_refs")
    @classmethod
    def validate_context_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for context_ref in value:
            if not context_ref.strip() or len(context_ref) > 200:
                raise ValueError("context_refs entries must be 1-200 characters")
        return value


class StartTurnRequest(ApiModel):
    input: str = Field(min_length=1, max_length=100_000)
    operation_id: str | None = Field(default=None, alias="operationId", max_length=128)


class TurnItemResponse(ApiModel):
    id: str
    type: Literal["userMessage", "assistantMessage", "toolCall", "error"]
    data: dict[str, Any]


class TurnResponse(ApiModel):
    id: str
    thread_id: str = Field(alias="threadId")
    status: Literal[
        "queued",
        "in_progress",
        "completed",
        "interrupted",
        "failed",
        "cancelled",
    ]
    created_at: datetime = Field(alias="createdAt")
    started_at: datetime | None = Field(alias="startedAt")
    completed_at: datetime | None = Field(alias="completedAt")
    final_response: str | None = Field(alias="finalResponse")
    items: tuple[TurnItemResponse, ...]
    usage: dict[str, Any] | None
    error: dict[str, Any] | None
    stream_available: bool = Field(alias="streamAvailable")
    recovery_mode: Literal["poll"] | None = Field(alias="recoveryMode")


class ThreadResponse(ApiModel):
    id: str
    title: str
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    context_refs: tuple[str, ...] = Field(alias="contextRefs")
    turns: tuple[TurnResponse, ...] = ()


class ThreadListResponse(ApiModel):
    data: tuple[ThreadResponse, ...]
    next_cursor: str | None = Field(alias="nextCursor")


class CapabilitiesResponse(ApiModel):
    thread_list: Literal[True] = True
    thread_create: Literal[True] = True
    thread_read: Literal[True] = True
    thread_rename: Literal[False] = False
    turn_stream: Literal[True] = True
    turn_interrupt: Literal[True] = True
    stream_resume_after_gateway_restart: Literal[False] = False


class HealthResponse(ApiModel):
    status: Literal["ok"] = "ok"
    service: Literal["sidera-main-agent-gateway"] = "sidera-main-agent-gateway"
    provider: Literal["available", "unavailable"]


class ErrorDetail(ApiModel):
    code: str
    message: str
    retryable: bool


class ErrorResponse(ApiModel):
    error: ErrorDetail
