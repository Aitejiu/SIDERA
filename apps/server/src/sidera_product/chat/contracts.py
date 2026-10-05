from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator
from sidera_agents.contracts import ExecutionWorkspace, ProfileRef

type RequestState = Literal[
    "claimed",
    "prepared",
    "dispatching",
    "accepted",
    "delivered",
    "busy",
    "rejected",
    "unknown",
]
type StopState = Literal[
    "prepared", "dispatching", "accepted", "unknown", "stopped", "failed"
]


class ChatModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, hide_input_in_errors=True
    )


class CreateThreadBody(ChatModel):
    title: str | None = Field(default=None, max_length=120)
    context_refs: tuple[str, ...] = Field(
        default=(), alias="contextRefs", max_length=20
    )
    profile_ref: ProfileRef | None = Field(default=None, alias="profileRef")

    @field_validator("context_refs")
    @classmethod
    def validate_context_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not ref.strip() or len(ref) > 200 for ref in value):
            raise ValueError("Context references must contain 1-200 characters")
        return value


class StartTurnBody(ChatModel):
    input: str = Field(min_length=1, max_length=100_000)
    operation_id: str | None = Field(
        default=None, alias="operationId", min_length=1, max_length=128
    )


@dataclass(frozen=True, slots=True)
class Conversation:
    id: UUID
    workspace_id: UUID
    principal_id: str
    title: str
    context_refs: tuple[str, ...]
    profile_ref: ProfileRef
    created_at: datetime
    updated_at: datetime
    version: int
    active_turn_id: UUID | None = None
    grant_id: UUID | None = None
    start_operation_id: UUID | None = None
    run_id: UUID | None = None
    execution_workspace: ExecutionWorkspace | None = None
    stop_operation_id: UUID | None = None
    stop_state: StopState | None = None


@dataclass(frozen=True, slots=True)
class ChatTurn:
    id: UUID
    conversation_id: UUID
    workspace_id: UUID
    principal_id: str
    operation_id: str
    native_operation_id: UUID
    request_fingerprint: str
    input_text: str
    kind: Literal["start", "input"]
    request_state: RequestState
    created_at: datetime
    updated_at: datetime
    version: int = 1
    dispatch_attempt: int = 0
    native_request: dict[str, JsonValue] | None = None
    response_id: UUID | None = None
    reason_code: str | None = None
    cached_view: dict[str, JsonValue] | None = None
    interrupt_operation_id: UUID | None = None
    interrupt_state: str | None = None


type TurnStatus = Literal[
    "queued", "in_progress", "completed", "interrupted", "failed", "cancelled"
]


class TurnItem(ChatModel):
    id: str
    type: Literal["userMessage", "assistantMessage", "toolCall", "error"]
    data: dict[str, JsonValue]


class TurnView(ChatModel):
    id: str
    thread_id: str = Field(alias="threadId")
    status: TurnStatus
    created_at: datetime = Field(alias="createdAt")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")
    final_response: str | None = Field(default=None, alias="finalResponse")
    items: tuple[TurnItem, ...]
    usage: dict[str, JsonValue] | None = None
    error: dict[str, JsonValue] | None = None
    stream_available: bool = Field(default=False, alias="streamAvailable")
    recovery_mode: Literal["poll"] | None = Field(default=None, alias="recoveryMode")
    profile_ref: ProfileRef | None = Field(default=None, alias="profileRef")
    run_id: UUID | None = Field(default=None, alias="runId")
    response_id: UUID | None = Field(default=None, alias="responseId")
    operation_id: str = Field(alias="operationId")
    operation_state: RequestState = Field(alias="operationState")
    interrupt_available: bool = Field(default=False, alias="interruptAvailable")
    interrupt_reason_code: str | None = Field(default=None, alias="interruptReasonCode")


class ThreadView(ChatModel):
    id: str
    title: str
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    context_refs: tuple[str, ...] = Field(alias="contextRefs")
    profile_ref: ProfileRef = Field(alias="profileRef")
    run_id: UUID | None = Field(default=None, alias="runId")
    turns: tuple[TurnView, ...] = ()


class ThreadListView(ChatModel):
    data: tuple[ThreadView, ...]
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class ChatCapabilities(ChatModel):
    thread_list: bool = True
    thread_create: bool = True
    thread_read: bool = True
    thread_rename: bool = False
    turn_stream: bool = True
    turn_interrupt: bool = False
    stream_resume_after_gateway_restart: bool = True


class ChatHealth(ChatModel):
    status: Literal["ok"] = "ok"
    service: Literal["sidera-product"] = "sidera-product"
    provider: Literal["available", "unavailable"]


def error_envelope(code: str, *, retryable: bool = False) -> dict[str, JsonValue]:
    return {"error": {"code": code, "message": code, "retryable": retryable}}


@dataclass(frozen=True, slots=True)
class ChatEvent:
    sequence: int
    type: str
    data: dict[str, JsonValue]
