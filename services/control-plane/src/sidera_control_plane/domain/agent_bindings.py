import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sidera_agents.contracts import (
    CredentialRef,
    ExecutionWorkspace,
    ProfileRef,
    ResourceRef,
    RuntimeRef,
    SourceRef,
    StartReceipt,
    ToolBinding,
)

from sidera_control_plane.domain.errors import DomainError, ValidationError

BindingTargetType = Literal["subtask", "worker_item"]


class AssignmentDenied(DomainError):
    code = "assignment_forbidden"


class HandoffRequired(DomainError):
    code = "handoff_required"


@dataclass(frozen=True, slots=True)
class AssigneeRef:
    kind: Literal["human", "profile"]
    id: UUID | str

    def __post_init__(self):
        if self.kind not in {"human", "profile"} or not str(self.id).strip():
            raise ValidationError("valid assignee required")
        if self.kind == "profile":
            try:
                object.__setattr__(self, "id", UUID(str(self.id)))
            except ValueError:
                raise ValidationError("profile id must be a UUID") from None
        else:
            object.__setattr__(self, "id", str(self.id))


@dataclass(frozen=True, slots=True)
class AgentBinding:
    workspace_id: UUID
    target_type: BindingTargetType
    target_id: UUID
    assignee: AssigneeRef
    profile_ref: ProfileRef | None
    version: int
    created_at: datetime
    changed_at: datetime
    runnable: bool = False
    not_runnable_reasons: tuple[str, ...] = ()

    def __post_init__(self):
        if self.target_type not in {"subtask", "worker_item"}:
            raise ValidationError("unknown assignment target")
        if type(self.version) is not int or self.version < 1:
            raise ValidationError("binding version must be positive")
        if self.created_at.tzinfo is None or self.changed_at.tzinfo is None:
            raise ValidationError("binding timestamps must be timezone-aware")
        if self.assignee.kind == "profile":
            if (
                self.profile_ref is None
                or self.profile_ref.workspace_id != self.workspace_id
                or self.profile_ref.profile_id != self.assignee.id
            ):
                raise ValidationError(
                    "binding needs an exact workspace-scoped profile pin"
                )
        elif self.profile_ref is not None or self.runnable:
            raise ValidationError("human is not an agent execution target")
        if self.runnable and self.not_runnable_reasons:
            raise ValidationError("not-runnable evidence cannot be upgraded")

    def to_mapping(self):
        return {
            "workspace_id": str(self.workspace_id),
            "target_type": self.target_type,
            "target_id": str(self.target_id),
            "assignee": {"kind": self.assignee.kind, "id": str(self.assignee.id)},
            "profile_ref": None
            if self.profile_ref is None
            else self.profile_ref.model_dump(mode="json"),
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "changed_at": self.changed_at.isoformat(),
            "runnable": self.runnable,
            "not_runnable_reasons": list(self.not_runnable_reasons),
        }


@dataclass(frozen=True, slots=True)
class AgentExecutionLink:
    workspace_id: UUID
    execution_id: UUID
    start_operation_id: UUID
    profile_ref: ProfileRef
    run_id: UUID | None
    event_cursor: int
    status: str
    authority_id: UUID
    binding_version: int
    request_sha256: str | None = None
    start_receipt: StartReceipt | None = None
    preparation: Mapping[str, object] | None = None

    def __post_init__(self):
        if self.profile_ref.workspace_id != self.workspace_id:
            raise ValidationError("run pin workspace mismatch")
        if type(self.binding_version) is not int or self.binding_version < 1:
            raise ValidationError("binding version must be positive")
        if type(self.event_cursor) is not int or self.event_cursor < 0:
            raise ValidationError("event cursor must be non-negative")
        if self.status not in {
            "pending",
            "starting",
            "running",
            "waiting",
            "waiting_capacity",
            "unknown",
            "closing",
            "succeeded",
            "failed",
            "stopped",
            "cancelled",
        }:
            raise ValidationError("unknown native execution status")
        if self.request_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.request_sha256
        ):
            raise ValidationError("native request fingerprint must be SHA-256")
        if self.start_receipt is not None and (
            self.start_receipt.operation_id != self.start_operation_id
        ):
            raise ValidationError("native admission operation mismatch")
        if self.status == "waiting_capacity" and not self.not_admitted:
            raise ValidationError("capacity waiting requires exact BUSY evidence")

    @property
    def not_admitted(self) -> bool:
        return (
            self.status == "waiting_capacity"
            and self.request_sha256 is not None
            and self.run_id is None
            and self.event_cursor == 0
            and self.start_receipt is not None
            and self.start_receipt.operation_id == self.start_operation_id
            and self.start_receipt.status.value == "busy"
            and self.start_receipt.run_id is None
        )

    @property
    def known_unadmitted(self) -> bool:
        if self.run_id is not None or self.event_cursor != 0:
            return False
        if self.status == "pending":
            return self.request_sha256 is None and self.start_receipt is None
        if self.not_admitted:
            return True
        return (
            self.status == "failed"
            and self.request_sha256 is not None
            and self.start_receipt is not None
            and self.start_receipt.operation_id == self.start_operation_id
            and self.start_receipt.status.value == "rejected"
            and self.start_receipt.run_id is None
        )


@dataclass(frozen=True, slots=True)
class NotAdmittedProof:
    link: AgentExecutionLink
    lease_id: UUID
    fencing_token: int

    def __post_init__(self):
        if not self.link.not_admitted or self.fencing_token < 1:
            raise ValidationError("pre-admission requeue requires typed BUSY proof")


@dataclass(frozen=True, slots=True)
class RetiredAdmissionPin:
    link: AgentExecutionLink
    lease_id: UUID
    fencing_token: int
    retired_at: datetime
    cause_command_id: UUID
    execution_binding_snapshot: Mapping[str, object] | None = None
    workspace_snapshot: Mapping[str, object] | None = None

    def __post_init__(self):
        NotAdmittedProof(self.link, self.lease_id, self.fencing_token)
        if self.retired_at.tzinfo is None:
            raise ValidationError("admission retirement time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ExecutionAuthority:
    id: UUID
    workspace_id: UUID
    principal_id: str
    execution_id: UUID
    target_type: BindingTargetType
    target_id: UUID
    binding_version: int
    profile_ref: ProfileRef
    runtime_ref: RuntimeRef
    lease_id: UUID
    fencing_token: int
    workspace_ref: UUID
    execution_workspace: ExecutionWorkspace
    actions: tuple[str, ...]
    sources: tuple[SourceRef, ...] = ()
    credentials: tuple[CredentialRef, ...] = ()
    tools: tuple[ToolBinding, ...] = ()
    resources: tuple[ResourceRef, ...] = ()
    revoked: bool = False
    upstream_grant_id: UUID | None = None

    def __post_init__(self):
        if self.target_type not in {"subtask", "worker_item"} or not self.principal_id:
            raise ValidationError("execution authority target and principal required")
        if self.upstream_grant_id is not None and (
            not isinstance(self.upstream_grant_id, UUID)
            or self.upstream_grant_id == self.id
        ):
            raise ValidationError("execution authority requires distinct upstream UUID")
        if (
            self.profile_ref.workspace_id != self.workspace_id
            or self.runtime_ref.workspace_id != self.workspace_id
        ):
            raise ValidationError("authority profile workspace mismatch")
        for value in (self.binding_version, self.fencing_token):
            if type(value) is not int or value < 1:
                raise ValidationError("authority versions must be positive")
        if any(
            ref.workspace_id != self.workspace_id
            for ref in (*self.credentials, *self.resources)
        ):
            raise ValidationError("authority target workspace mismatch")
        for tool in self.tools:
            if (
                tool.resource_ref is not None
                and tool.resource_ref.workspace_id != self.workspace_id
            ):
                raise ValidationError("authority tool workspace mismatch")
