from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StrictBool, model_validator
from sidera_agents.contracts import ProfileRef, RunEvent

from sidera_control_plane.domain.assignment_views import (
    AgentBindingView,
    AgentExecutionView,
    NativeRunView,
    ReadView,
)


class NativeWorkerContextAddress(ReadView):
    worker_item_id: UUID
    plan_generation: int = Field(ge=1, strict=True)
    context_version: int = Field(ge=1, strict=True)
    execution_id: UUID
    session_id: UUID
    session_version: int = Field(ge=1, strict=True)
    worker_id: UUID
    execution_mode: Literal["native_profile"]
    binding_version: int = Field(ge=1, strict=True)
    run_id: UUID
    session_last_sequence: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def _validate_session_observation(self) -> "NativeWorkerContextAddress":
        if (
            self.session_last_sequence is not None
            and self.session_last_sequence >= self.session_version
        ):
            raise ValueError("Session sequence must be less than its version")
        return self


class NativeWorkerContextView(ReadView):
    workspace_id: UUID
    worker_item_id: UUID
    execution_mode: Literal["native_profile", "legacy", "unassigned"]
    worker_item_version: int = Field(ge=1, strict=True)
    binding: AgentBindingView | None
    execution: AgentExecutionView | None
    execution_version: int | None = Field(default=None, ge=1, strict=True)
    context: NativeWorkerContextAddress | None
    input_enabled: StrictBool
    read_only_reason: str | None
    run: NativeRunView | None
    as_of: AwareDatetime

    @model_validator(mode="after")
    def _validate_scope(self) -> "NativeWorkerContextView":
        if (self.execution is None) != (self.execution_version is None):
            raise ValueError("execution version must match execution presence")
        if self.binding is not None and (
            self.binding.workspace_id != self.workspace_id
            or self.binding.target_type != "worker_item"
            or self.binding.target_id != self.worker_item_id
        ):
            raise ValueError("binding is outside the WorkerItem scope")
        if self.execution is not None and (
            self.execution.workspace_id != self.workspace_id
            or self.execution.worker_item_id != self.worker_item_id
        ):
            raise ValueError("execution is outside the WorkerItem scope")
        if self.context is not None and (
            self.execution_mode != "native_profile"
            or self.execution is None
            or self.context.worker_item_id != self.worker_item_id
            or self.context.execution_id != self.execution.execution_id
        ):
            raise ValueError("native context is outside the WorkerItem scope")
        if (
            self.run is not None
            and self.run.profile_ref.workspace_id != self.workspace_id
        ):
            raise ValueError("Run profile is outside the workspace scope")
        if self.input_enabled and (
            self.execution_mode != "native_profile"
            or self.context is None
            or self.run is None
        ):
            raise ValueError("native input requires an observed native Run")
        return self


class NativeWorkerTraceView(ReadView):
    workspace_id: UUID
    worker_item_id: UUID
    execution_id: UUID
    run_id: UUID
    profile_ref: ProfileRef
    events: tuple[RunEvent, ...]
    next_sequence: int = Field(ge=0, strict=True)
    has_more: StrictBool

    @model_validator(mode="after")
    def _validate_scope(self) -> "NativeWorkerTraceView":
        if self.profile_ref.workspace_id != self.workspace_id:
            raise ValueError("Trace profile is outside the workspace scope")
        if any(event.run_id != self.run_id for event in self.events):
            raise ValueError("Trace event is outside the Run scope")
        if self.events and self.events[-1].sequence != self.next_sequence:
            raise ValueError("Trace cursor does not match the final event")
        return self
