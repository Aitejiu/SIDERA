from dataclasses import dataclass, field
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sidera_agents.contracts import ProfileRef, RunState, RuntimeRef

from sidera_control_plane.domain.errors import ValidationError

type AssignmentKind = Literal["subtask", "worker_item"]
type ProfileWorkKind = Literal["assignment", "execution", "planning"]


@dataclass(frozen=True, slots=True)
class ProfileWorkTarget:
    workspace_id: UUID
    profile_id: UUID
    principal_id: str

    def __post_init__(self) -> None:
        if (
            type(self.workspace_id) is not UUID
            or type(self.profile_id) is not UUID
            or type(self.principal_id) is not str
            or not self.principal_id.strip()
        ):
            raise ValidationError("invalid profile work scope")


@dataclass(frozen=True, slots=True)
class ProfileWorkRunTarget:
    workspace_id: UUID
    kind: ProfileWorkKind
    work_id: UUID
    principal_id: str
    target_type: AssignmentKind | None = None

    def __post_init__(self) -> None:
        if (
            type(self.workspace_id) is not UUID
            or type(self.work_id) is not UUID
            or type(self.principal_id) is not str
            or not self.principal_id.strip()
            or self.kind not in {"assignment", "execution", "planning"}
            or (
                self.target_type not in {"subtask", "worker_item"}
                if self.kind == "assignment"
                else self.target_type is not None
            )
        ):
            raise ValidationError("invalid original work run scope")


class ReadView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssigneeView(ReadView):
    kind: Literal["human", "profile"]
    id: str


class AgentBindingView(ReadView):
    workspace_id: UUID
    target_type: AssignmentKind
    target_id: UUID
    assignee: AssigneeView
    profile_ref: ProfileRef | None
    version: int = Field(ge=1, strict=True)
    created_at: AwareDatetime
    changed_at: AwareDatetime
    runnable: bool
    not_runnable_reasons: tuple[str, ...]


class ProfileWorkRow(ReadView):
    workspace_id: UUID
    work_id: UUID
    kind: ProfileWorkKind
    title: str
    status: str
    updated_at: AwareDatetime
    profile_ref: ProfileRef
    root_task_id: UUID | None = None
    subtask_id: UUID | None = None
    worker_item_id: UUID | None = None
    planning_request_id: UUID | None = None
    planning_run_id: UUID | None = None
    execution_id: UUID | None = None
    target_type: AssignmentKind | None = None
    binding_version: int | None = Field(default=None, ge=1)
    run_id: UUID | None = None
    link_status: str | None = None
    event_cursor: int | None = Field(default=None, ge=0)


class ProfileWorkPage(ReadView):
    items: tuple[ProfileWorkRow, ...]
    next_cursor: str | None


class AgentExecutionView(ReadView):
    workspace_id: UUID
    execution_id: UUID
    root_task_id: UUID | None
    subtask_id: UUID
    worker_item_id: UUID | None
    status: str
    updated_at: AwareDatetime
    profile_ref: ProfileRef | None
    binding_version: int | None
    run_id: UUID | None
    link_status: str | None
    event_cursor: int | None


class NativeRunView(ReadView):
    state: RunState
    input_open: bool
    profile_ref: ProfileRef
    config_hash: str
    runtime_ref: RuntimeRef
    actual_runtime_version: str
    selected_model: str | None
    observed_model: str | None
    applied_fields: tuple[str, ...]
    reason_code: str | None


class NativeRunSummary(ReadView):
    workspace_id: UUID
    kind: ProfileWorkKind
    work_id: UUID
    available: bool
    run_id: UUID | None
    reason_code: str | None
    run: NativeRunView | None = None


class WorkCursor(ReadView):
    version: Literal[1] = 1
    workspace_id: UUID
    profile_id: UUID
    updated_at: AwareDatetime
    kind: ProfileWorkKind
    work_id: UUID
    target_type: Literal["", "subtask", "worker_item"] = ""

    @model_validator(mode="after")
    def _check_discriminator(self) -> Self:
        if (self.kind == "assignment") != (self.target_type != ""):
            raise ValueError("cursor target type must match work kind")
        return self


@dataclass(frozen=True, slots=True)
class OriginalRunEvidence:
    workspace_id: UUID
    run_id: UUID | None
    profile_ref: ProfileRef | None
    operation_id: UUID | None
    grant_id: UUID | None = field(repr=False)
    target_type: AssignmentKind | None = None
    target_id: UUID | None = None
    verified: bool = False
    reason_code: str | None = None
