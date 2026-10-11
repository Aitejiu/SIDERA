import re
from dataclasses import dataclass
from uuid import UUID

from sidera_agents.contracts import (
    CredentialRef,
    ExecutionWorkspace,
    ProfileRef,
    ResourceRef,
    RunStart,
    RuntimeRef,
    SourceRef,
    StartReceipt,
    ToolBinding,
)

from sidera_control_plane.domain.errors import DomainError, ValidationError


class NativePlanningCleanupPending(DomainError):
    code = "native_cleanup_pending"


@dataclass(frozen=True, slots=True)
class PlanningAuthority:
    id: UUID
    workspace_id: UUID
    planning_request_id: UUID
    planning_run_id: UUID
    planning_profile_id: UUID
    planning_profile_version: int
    operation_id: UUID
    principal_id: str
    upstream_grant_id: UUID
    profile_ref: ProfileRef
    runtime_ref: RuntimeRef
    lease_id: UUID
    owner_id: str
    fencing_token: int
    execution_workspace: ExecutionWorkspace
    actions: tuple[str, ...]
    sources: tuple[SourceRef, ...] = ()
    credentials: tuple[CredentialRef, ...] = ()
    tools: tuple[ToolBinding, ...] = ()
    resources: tuple[ResourceRef, ...] = ()
    revoked: bool = False

    def __post_init__(self):
        if not self.principal_id or not self.owner_id:
            raise ValidationError("planning authority requires trusted owners")
        if any(
            type(value) is not int or value < 1
            for value in (self.planning_profile_version, self.fencing_token)
        ):
            raise ValidationError("planning authority versions must be positive")
        if any(
            ref.workspace_id != self.workspace_id
            for ref in (
                self.profile_ref,
                self.runtime_ref,
                *self.credentials,
                *self.resources,
            )
        ):
            raise ValidationError("planning authority workspace mismatch")
        if any(
            tool.resource_ref is not None
            and tool.resource_ref.workspace_id != self.workspace_id
            for tool in self.tools
        ):
            raise ValidationError("planning authority tool workspace mismatch")


@dataclass(frozen=True, slots=True)
class PlanningAgentLink:
    workspace_id: UUID
    planning_run_id: UUID
    authority_id: UUID
    start_operation_id: UUID
    profile_ref: ProfileRef
    start_request: RunStart
    request_sha256: str
    status: str
    run_id: UUID | None = None
    start_receipt: StartReceipt | None = None
    event_cursor: int = 0
    response_id: UUID | None = None
    closed: bool = False
    reason_code: str | None = None

    def __post_init__(self):
        if self.profile_ref.workspace_id != self.workspace_id or (
            self.start_request.operation_id,
            self.start_request.profile_ref,
            self.start_request.grant_id,
        ) != (self.start_operation_id, self.profile_ref, self.authority_id):
            raise ValidationError("planning start identity mismatch")
        if not re.fullmatch(r"[0-9a-f]{64}", self.request_sha256):
            raise ValidationError("planning request fingerprint must be SHA-256")
        if type(self.event_cursor) is not int or self.event_cursor < 0:
            raise ValidationError("planning event cursor must be non-negative")
        if self.status not in {
            "pending",
            "starting",
            "waiting_capacity",
            "running",
            "unknown",
            "cleanup_pending",
            "closed",
            "rejected",
        }:
            raise ValidationError("invalid native planning link status")
        if self.start_receipt is not None and (
            self.start_receipt.operation_id != self.start_operation_id
            or (
                self.start_receipt.run_id is not None
                and self.start_receipt.run_id != self.run_id
            )
        ):
            raise ValidationError("planning receipt identity mismatch")
        if self.status == "waiting_capacity" and not self.not_admitted:
            raise ValidationError(
                "planning capacity wait requires durable BUSY evidence"
            )
        if self.status == "closed" and (not self.closed or self.run_id is None):
            raise ValidationError("planning closure requires its actual original Run")
        if self.closed and self.status != "closed":
            raise ValidationError("closed native planning link is terminal")

    @property
    def not_admitted(self):
        return (
            self.status in {"waiting_capacity", "rejected"}
            and self.run_id is None
            and self.event_cursor == 0
            and self.start_receipt is not None
            and self.start_receipt.status.value
            == ("busy" if self.status == "waiting_capacity" else "rejected")
            and self.start_receipt.run_id is None
        )

    @property
    def retry_boundary_closed(self):
        return self.not_admitted or (self.closed and self.run_id is not None)
