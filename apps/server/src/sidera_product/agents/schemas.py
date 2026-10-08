from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sidera_agents.contracts import (
    CapabilityResourceDraft,
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    ProfileStatus,
    ResourceRef,
    ResourceRevision,
    ResourceStatus,
    RuntimeRef,
    RuntimeSnapshot,
)
from sidera_control_plane.composition.native_runtime import ProfileWorkPage


class ManagementModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, hide_input_in_errors=True
    )


class ProfileCreateBody(ManagementModel):
    operation_id: str = Field(
        alias="operationId", min_length=1, max_length=128, strict=True
    )
    draft: ProfileDraft


class ProfileRevisionBody(ProfileCreateBody):
    expected_ref: ProfileRef


class ResourceCreateBody(ManagementModel):
    operation_id: str = Field(
        alias="operationId", min_length=1, max_length=128, strict=True
    )
    draft: CapabilityResourceDraft


class ResourceRevisionBody(ResourceCreateBody):
    expected_ref: ResourceRef


class StatusBody(ManagementModel):
    enabled: StrictBool
    expected_version: StrictInt = Field(ge=1)


class RuntimeRegisterBody(ManagementModel):
    harness_id: str = Field(min_length=1, max_length=128)
    executable: str = Field(min_length=1, max_length=4096)
    max_active: StrictInt = Field(ge=1)
    max_sessions: StrictInt = Field(ge=1)


class RuntimeRefreshBody(ManagementModel):
    ref: RuntimeRef


class RuntimeLimitsBody(RuntimeRefreshBody):
    max_active: StrictInt = Field(ge=1)
    max_sessions: StrictInt = Field(ge=1)
    expected_version: StrictInt = Field(ge=1)


class RuntimeStatusBody(RuntimeRefreshBody, StatusBody):
    pass


class ProfileView(ManagementModel):
    profile: ProfileRevision
    status: ProfileStatus


class ProfileListView(ManagementModel):
    items: tuple[ProfileView, ...]


class ResourceView(ManagementModel):
    resource: ResourceRevision
    status: ResourceStatus


class ResourceListView(ManagementModel):
    items: tuple[ResourceView, ...]


class RuntimeListView(ManagementModel):
    items: tuple[RuntimeSnapshot, ...]


class ToolConnectionView(ManagementModel):
    workspace_id: UUID
    connection_id: UUID
    name: Literal["SIDERA Task Tools"]
    availability: Literal["configured"]


class ConversationWorkView(ManagementModel):
    conversation_id: UUID
    title: str
    profile_ref: ProfileRef
    run_id: UUID | None
    stop_state: str | None
    updated_at: AwareDatetime


class ProfileWorkView(ManagementModel):
    task_work: ProfileWorkPage
    conversations: tuple[ConversationWorkView, ...]


class BootstrapSessionView(ManagementModel):
    session_id: UUID
    expires_at: AwareDatetime


class LocalSessionView(BootstrapSessionView):
    workspace_id: UUID
    principal_id: str


class LogoutView(ManagementModel):
    status: str
