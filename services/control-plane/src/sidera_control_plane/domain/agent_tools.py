"""Immutable business references for native task-tool authority."""

import json
from dataclasses import dataclass
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictInt,
    StringConstraints,
    model_validator,
)
from sidera_agents.contracts import ProfileRef


def _positive(values: tuple[object, ...]) -> None:
    if any(type(value) is not int or value < 1 for value in values):
        raise ValueError("Tool scope requires exact positive versions and fences")


def _identifiers(values: tuple[object, ...]) -> None:
    if any(type(value) is not UUID for value in values):
        raise ValueError("Tool scope requires exact UUID identities")


@dataclass(frozen=True, slots=True)
class ControlPlaneToolScope:
    workspace_id: UUID
    principal_id: str
    role: Literal["worker", "planner"]
    native_grant_id: UUID
    connection_id: UUID
    profile_ref: ProfileRef
    start_operation_id: UUID
    run_id: UUID | None
    root_task_id: UUID | None
    execution_id: UUID | None
    worker_item_id: UUID | None
    execution_version: int | None
    binding_version: int | None
    planning_request_id: UUID | None
    planning_run_id: UUID | None
    lease_id: UUID
    fencing_token: int

    def __post_init__(self) -> None:
        _identifiers(
            (
                self.workspace_id,
                self.native_grant_id,
                self.connection_id,
                self.start_operation_id,
                self.lease_id,
            )
        )
        _positive((self.fencing_token,))
        if (
            not isinstance(self.principal_id, str)
            or not self.principal_id.strip()
            or not isinstance(self.profile_ref, ProfileRef)
            or self.profile_ref.workspace_id != self.workspace_id
        ):
            raise ValueError(
                "Tool scope requires a workspace-scoped profile and principal"
            )
        _identifiers(
            tuple(
                value for value in (self.run_id, self.root_task_id) if value is not None
            )
        )
        if self.role == "worker":
            _identifiers((self.execution_id, self.worker_item_id))
            _positive((self.execution_version, self.binding_version))
            if self.planning_request_id is not None or self.planning_run_id is not None:
                raise ValueError("Worker tool scope cannot contain planning identities")
        elif self.role == "planner":
            _identifiers((self.planning_request_id, self.planning_run_id))
            if any(
                value is not None
                for value in (
                    self.execution_id,
                    self.worker_item_id,
                    self.execution_version,
                    self.binding_version,
                )
            ):
                raise ValueError(
                    "Planner tool scope cannot contain execution identities"
                )
        else:
            raise ValueError("Unknown task-tool role")


@dataclass(frozen=True, slots=True)
class WorkerProgressTarget:
    workspace_id: UUID
    principal_id: str
    execution_id: UUID
    worker_item_id: UUID
    native_grant_id: UUID
    connection_id: UUID
    execution_version: int
    binding_version: int
    lease_id: UUID
    fencing_token: int
    run_id: UUID
    response_id: UUID

    def __post_init__(self) -> None:
        _identifiers(
            (
                self.workspace_id,
                self.execution_id,
                self.worker_item_id,
                self.native_grant_id,
                self.connection_id,
                self.lease_id,
                self.run_id,
                self.response_id,
            )
        )
        _positive((self.execution_version, self.binding_version, self.fencing_token))
        if not isinstance(self.principal_id, str) or not self.principal_id.strip():
            raise ValueError("Progress requires a trusted principal")


class WorkerProgressPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    worker_item_id: UUID
    authority_id: UUID
    connection_id: UUID
    binding_version: StrictInt = Field(ge=1)
    lease_id: UUID
    fencing_token: StrictInt = Field(ge=1)
    run_id: UUID
    response_id: UUID
    summary: Annotated[
        str,
        StringConstraints(
            strict=True, strip_whitespace=True, min_length=1, max_length=4000
        ),
    ]
    steps: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def bounded_finite_data(self) -> Self:
        try:
            encoded = json.dumps(
                {"summary": self.summary, "steps": self.steps},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("Progress data must be finite JSON") from None
        if len(encoded) > 32768:
            raise ValueError("Progress data exceeds its declared budget")
        return self
