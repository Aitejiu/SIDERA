from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


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


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    workspace_id: UUID
    command_type: NonEmptyString
    actor: ActorRequest
    target_id: UUID | None
    expected_version: int | None
    idempotency_key: NonEmptyString
    correlation_id: UUID
    payload: CreateTaskPayload

    def to_envelope(self) -> CommandEnvelope:
        return CommandEnvelope(
            id=self.command_id,
            workspace_id=self.workspace_id,
            command_type=self.command_type,
            actor_type=self.actor.type,
            actor_id=self.actor.id,
            target_id=self.target_id,
            payload=dict(self.payload.model_dump(mode="json")),
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
