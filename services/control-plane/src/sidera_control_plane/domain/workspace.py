import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextMaterialization,
    ContextMaterializationV3,
    ContextPurpose,
    context_package_reference,
)
from sidera_control_plane.domain.errors import InvalidTransition, ValidationError


class CheckoutMode(StrEnum):
    WORKTREE = "worktree"
    CLONE = "clone"


class BranchPolicy(StrEnum):
    EXECUTION = "execution"
    PROVIDED = "provided"


class WorkspaceLifecycle(StrEnum):
    REQUESTED = "requested"
    PREPARING = "preparing"
    READY = "ready"
    ACTIVE = "active"
    FROZEN = "frozen"
    RETAINED = "retained"
    CLEANING = "cleaning"
    RELEASED = "released"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class RepositoryCheckoutSpec:
    repository_ref: str
    base_revision: str
    checkout_mode: CheckoutMode
    writable_paths: tuple[str, ...]
    readable_paths: tuple[str, ...]
    branch_policy: BranchPolicy

    def __post_init__(self) -> None:
        if not self.repository_ref.strip():
            raise ValidationError("repository_ref is required")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", self.base_revision.strip()):
            raise ValidationError("base_revision must be an absolute commit revision")
        if not self.writable_paths:
            raise ValidationError("writable scope cannot be empty")
        for path in (*self.writable_paths, *self.readable_paths):
            _validate_relative_path(path)
        object.__setattr__(
            self,
            "checkout_mode",
            _enum_value(self.checkout_mode, CheckoutMode, "checkout mode"),
        )
        object.__setattr__(
            self,
            "branch_policy",
            _enum_value(self.branch_policy, BranchPolicy, "branch policy"),
        )
        object.__setattr__(self, "writable_paths", tuple(self.writable_paths))
        object.__setattr__(self, "readable_paths", tuple(self.readable_paths))


@dataclass(frozen=True, slots=True)
class ExecutionSpec:
    schema_version: Literal[1, 2, 3]
    workspace_id: UUID
    task_id: UUID
    execution_id: UUID
    worker_id: UUID
    lease_id: UUID
    fencing_token: int
    goal: str
    acceptance_criteria: tuple[str, ...]
    context_package_ref: str | None
    repositories: tuple[RepositoryCheckoutSpec, ...]
    tool_policy: Mapping[str, Any]
    sandbox_policy: Mapping[str, Any]
    budget: Mapping[str, Any]
    deadline: datetime | None
    expected_outputs: tuple[str, ...]
    adapter_module_ref: UUID
    adapter_config_version: int
    workspace_module_ref: UUID
    workspace_config_version: int
    context_package: ContextMaterialization | None = None
    execution_purpose: Literal["code", "verification"] | None = None
    physical_input_ack: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.schema_version not in (1, 2, 3):
            raise ValidationError("unsupported ExecutionSpec schema_version")
        if self.schema_version == 1:
            if self.context_package_ref is not None or self.context_package is not None:
                raise ValidationError("schema v1 cannot carry context package data")
            if self.execution_purpose is not None or self.physical_input_ack is not None:
                raise ValidationError("schema v1 cannot carry execution input data")
        elif self.schema_version == 2:
            if self.context_package_ref is None or self.context_package is None:
                raise ValidationError("schema v2 requires context package data")
            if self.execution_purpose is not None or self.physical_input_ack is not None:
                raise ValidationError("schema v2 cannot carry execution input data")
            metadata = self.context_package.metadata
            if (
                metadata.purpose is not ContextPurpose.EXECUTION
                or metadata.consumer_type is not ContextConsumerType.WORKER
                or self.context_package_ref
                != context_package_reference(metadata.package_id, metadata.package_hash)
            ):
                raise ValidationError(
                    "schema v2 context package reference does not match metadata"
                )
        else:
            if self.context_package_ref is None or self.context_package is None:
                raise ValidationError("schema v3 requires context package data")
            if not isinstance(self.context_package, ContextMaterializationV3):
                raise ValidationError("schema v3 requires v3 context materialization")
            if self.execution_purpose not in {"code", "verification"}:
                raise ValidationError(
                    "schema v3 requires execution_purpose code or verification"
                )
            if not isinstance(self.physical_input_ack, Mapping) or not self.physical_input_ack:
                raise ValidationError("schema v3 requires physical input acknowledgement")
            has_code = self.context_package.code_input_manifest is not None
            has_verification = self.context_package.verification_manifest is not None
            expected_code = self.execution_purpose == "code"
            if has_code != expected_code or has_verification == expected_code:
                raise ValidationError(
                    "schema v3 execution purpose does not match context manifest"
                )
            metadata = self.context_package.metadata
            if (
                metadata.purpose is not ContextPurpose.EXECUTION
                or metadata.consumer_type is not ContextConsumerType.WORKER
                or self.context_package_ref
                != context_package_reference(metadata.package_id, metadata.package_hash)
            ):
                raise ValidationError(
                    "schema v3 context package reference does not match metadata"
                )
            acknowledged_execution_id = self.physical_input_ack.get("execution_id")
            if acknowledged_execution_id is not None and str(acknowledged_execution_id) != str(
                self.execution_id
            ):
                raise ValidationError("schema v3 physical input acknowledgement is for another execution")
            if expected_code:
                code_manifest = self.context_package.code_input_manifest
                assert code_manifest is not None
                if self.physical_input_ack.get("input_hash") != code_manifest.input_hash:
                    raise ValidationError(
                        "schema v3 physical input acknowledgement does not match code input"
                    )
        if self.fencing_token < 1:
            raise ValidationError("fencing_token must be positive")
        if not self.goal.strip():
            raise ValidationError("goal is required")
        if not self.acceptance_criteria or any(
            not item.strip() for item in self.acceptance_criteria
        ):
            raise ValidationError("acceptance_criteria cannot be empty")
        if len(self.repositories) != 1:
            raise ValidationError("ExecutionSpec requires exactly one repository")
        if self.adapter_config_version < 1 or self.workspace_config_version < 1:
            raise ValidationError("module config versions must be positive")
        if self.deadline is not None and self.deadline.tzinfo is None:
            raise ValidationError("deadline must be timezone-aware")
        if not self.expected_outputs or any(
            not item.strip() for item in self.expected_outputs
        ):
            raise ValidationError("expected_outputs cannot be empty")
        object.__setattr__(self, "repositories", tuple(self.repositories))
        object.__setattr__(self, "acceptance_criteria", tuple(self.acceptance_criteria))
        object.__setattr__(self, "expected_outputs", tuple(self.expected_outputs))


@dataclass(frozen=True, slots=True)
class WorkspaceCheckout:
    repository_ref: str
    base_revision: str
    branch: str
    current_head: str

    def __post_init__(self) -> None:
        if not self.repository_ref.strip() or not self.branch.strip():
            raise ValidationError("workspace checkout identity is required")
        for name, value in (
            ("base_revision", self.base_revision),
            ("current_head", self.current_head),
        ):
            if not re.fullmatch(r"[0-9a-fA-F]{40,64}", value.strip()):
                raise ValidationError(f"{name} must be an absolute commit revision")


@dataclass(slots=True)
class ExecutionWorkspaceManifest:
    id: UUID
    workspace_id: UUID
    task_id: UUID
    execution_id: UUID
    checkouts: tuple[WorkspaceCheckout, ...]
    location_ref: str
    lifecycle: WorkspaceLifecycle
    retention_seconds: int
    retain_until: datetime | None
    fencing_token: int
    created_at: datetime
    frozen_at: datetime | None
    released_at: datetime | None
    version: int

    @classmethod
    def create(
        cls,
        *,
        id: UUID,
        workspace_id: UUID,
        task_id: UUID,
        execution_id: UUID,
        checkouts: tuple[WorkspaceCheckout, ...],
        location_ref: str,
        retention_seconds: int,
        fencing_token: int,
        created_at: datetime,
    ) -> "ExecutionWorkspaceManifest":
        _validate_manifest_values(location_ref, retention_seconds, fencing_token)
        if created_at.tzinfo is None:
            raise ValidationError("created_at must be timezone-aware")
        return cls(
            id=id,
            workspace_id=workspace_id,
            task_id=task_id,
            execution_id=execution_id,
            checkouts=tuple(checkouts),
            location_ref=location_ref.strip(),
            lifecycle=WorkspaceLifecycle.REQUESTED,
            retention_seconds=retention_seconds,
            retain_until=None,
            fencing_token=fencing_token,
            created_at=created_at,
            frozen_at=None,
            released_at=None,
            version=1,
        )

    def mark_preparing(self) -> None:
        self._transition(WorkspaceLifecycle.REQUESTED, WorkspaceLifecycle.PREPARING)

    def mark_ready(self) -> None:
        self._transition(WorkspaceLifecycle.PREPARING, WorkspaceLifecycle.READY)

    def mark_active(self) -> None:
        self._transition(WorkspaceLifecycle.READY, WorkspaceLifecycle.ACTIVE)

    def freeze(self, *, now: datetime) -> None:
        if self.lifecycle is not WorkspaceLifecycle.ACTIVE:
            raise InvalidTransition("only an active workspace can be frozen")
        self.lifecycle = WorkspaceLifecycle.FROZEN
        self.frozen_at = now
        self.version += 1

    def retain(self, *, until: datetime) -> None:
        if self.lifecycle is not WorkspaceLifecycle.FROZEN:
            raise InvalidTransition("only a frozen workspace can be retained")
        if until <= self.frozen_at if self.frozen_at is not None else True:
            raise ValidationError("retain_until must be after frozen_at")
        self.lifecycle = WorkspaceLifecycle.RETAINED
        self.retain_until = until
        self.version += 1

    def begin_cleanup(self) -> None:
        self._transition(WorkspaceLifecycle.RETAINED, WorkspaceLifecycle.CLEANING)

    def release(self, *, now: datetime) -> None:
        self._transition(WorkspaceLifecycle.CLEANING, WorkspaceLifecycle.RELEASED)
        self.released_at = now

    def quarantine(self) -> None:
        if self.lifecycle is WorkspaceLifecycle.RELEASED:
            raise InvalidTransition("released workspace cannot be quarantined")
        self.lifecycle = WorkspaceLifecycle.QUARANTINED
        self.version += 1

    def _transition(
        self, expected: WorkspaceLifecycle, target: WorkspaceLifecycle
    ) -> None:
        if self.lifecycle is not expected:
            raise InvalidTransition(
                f"workspace cannot move from {self.lifecycle} to {target}"
            )
        self.lifecycle = target
        self.version += 1


def workspace_manifest_digest(manifest: ExecutionWorkspaceManifest) -> str:
    """Hash the exact repository state owned by one workspace manifest."""

    if not isinstance(manifest, ExecutionWorkspaceManifest):
        raise ValidationError("workspace digest requires a workspace manifest")
    document = {
        "id": str(manifest.id),
        "workspace_id": str(manifest.workspace_id),
        "execution_id": str(manifest.execution_id),
        "fencing_token": manifest.fencing_token,
        "checkouts": [
            {
                "repository_ref": checkout.repository_ref,
                "base_revision": checkout.base_revision,
                "branch": checkout.branch,
                "current_head": checkout.current_head,
            }
            for checkout in manifest.checkouts
        ],
    }
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def workspace_location_ref_hash(location_ref: str) -> str:
    """Return a stable digest for an opaque physical location reference."""

    _validate_manifest_values(location_ref, 1, 1)
    return hashlib.sha256(location_ref.strip().encode("utf-8")).hexdigest()


def workspace_repository_state_digest(manifest: ExecutionWorkspaceManifest) -> str:
    """Hash checkout state without changing the manifest's ownership identity."""

    if not isinstance(manifest, ExecutionWorkspaceManifest):
        raise ValidationError("workspace repository digest requires a manifest")
    document = [
        {
            "repository_ref": checkout.repository_ref,
            "base_revision": checkout.base_revision,
            "branch": checkout.branch,
            "current_head": checkout.current_head,
        }
        for checkout in manifest.checkouts
    ]
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ProcessDescriptor:
    pid: int
    started_at: datetime
    nonce: str
    process_group_id: int

    def __post_init__(self) -> None:
        if self.pid < 1 or self.process_group_id < 1:
            raise ValidationError("process identity must be positive")
        if self.started_at.tzinfo is None:
            raise ValidationError("process start time must be timezone-aware")
        if not self.nonce.strip():
            raise ValidationError("process nonce is required")


def _validate_manifest_values(
    location_ref: str, retention_seconds: int, fencing_token: int
) -> None:
    if not location_ref.strip() or location_ref.startswith("/"):
        raise ValidationError("location_ref must be a non-absolute opaque reference")
    if retention_seconds <= 0:
        raise ValidationError("retention policy must be positive")
    if fencing_token < 1:
        raise ValidationError("fencing_token must be positive")


def _validate_relative_path(path: str) -> None:
    clean = path.strip()
    if not clean or clean.startswith("/") or ".." in clean.split("/"):
        raise ValidationError("workspace paths must be non-empty and relative")


def _enum_value(value: Any, enum_type: type[StrEnum], label: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported {label}: {value!r}") from exc
