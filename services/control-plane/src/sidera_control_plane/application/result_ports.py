from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from sidera_control_plane.domain.code_results import (
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.result_operations import (
    ResultApplyOperationV1,
    ResultError,
    ResultOperationStatus,
    ReworkRecordV1,
    TaskResultHead,
)
from sidera_control_plane.domain.result_verification import (
    TaskAcceptanceDecisionV1,
    VerificationResultV1,
)

if TYPE_CHECKING:
    from sidera_control_plane.domain.workspace import ExecutionWorkspaceManifest


_GIT_OID_PATTERN = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


@dataclass(frozen=True, slots=True)
class CapturedCode:
    """Immutable receipt for a Git result captured from one execution workspace."""

    operation_id: UUID
    repository_ref: str
    input_commit: str
    input_tree: str
    result_commit: str
    result_tree: str
    observed_agent_head: str
    receipt_ref: str
    created_at: datetime
    _repository_path: Path | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, UUID):
            raise TypeError("capture operation_id must be a UUID")
        if not isinstance(self.repository_ref, str) or not self.repository_ref.strip():
            raise ValueError("capture repository_ref is required")
        for value, label in (
            (self.input_commit, "input_commit"),
            (self.input_tree, "input_tree"),
            (self.result_commit, "result_commit"),
            (self.result_tree, "result_tree"),
            (self.observed_agent_head, "observed_agent_head"),
        ):
            if not isinstance(value, str) or _GIT_OID_PATTERN.fullmatch(value) is None:
                raise ValueError(f"capture {label} must be a Git object id")
        expected_ref = f"refs/sidera/results/{self.operation_id}"
        if self.receipt_ref != f"{expected_ref}.metadata":
            raise ValueError("capture receipt_ref does not match operation_id")
        if (
            not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
        ):
            raise ValueError("capture created_at must be timezone-aware")
        if self._repository_path is not None and not isinstance(
            self._repository_path, Path
        ):
            raise TypeError("capture repository path must be a Path")

    @property
    def result_ref(self) -> str:
        return f"refs/sidera/results/{self.operation_id}"

    def to_mapping(self) -> dict[str, object]:
        return {
            "operation_id": str(self.operation_id),
            "repository_ref": self.repository_ref,
            "input_commit": self.input_commit,
            "input_tree": self.input_tree,
            "result_commit": self.result_commit,
            "result_tree": self.result_tree,
            "observed_agent_head": self.observed_agent_head,
            "receipt_ref": self.receipt_ref,
            "created_at": self.created_at.astimezone(UTC).isoformat(),
        }


class CodeResultCapturePort(Protocol):
    def capture(
        self,
        *,
        manifest: ExecutionWorkspaceManifest,
        input_commit: str,
        operation_id: UUID,
        writable_paths: Sequence[str],
    ) -> CapturedCode:
        raise NotImplementedError

    def inspect_capture(self, operation_id: UUID) -> CapturedCode | None:
        raise NotImplementedError

    def export_patch(self, snapshot: CapturedCode) -> bytes:
        raise NotImplementedError

    def inspect_revision(
        self, *, repository_ref: str, revision: str
    ) -> tuple[str, str]:
        """Resolve an exact commit and its tree without inspecting a worktree."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ResultOperationAttemptV1:
    """Durable identity and observation for a non-task result operation."""

    operation_id: UUID
    workspace_id: UUID
    root_task_id: UUID
    operation_kind: str
    attempt: int
    status: ResultOperationStatus
    payload: Mapping[str, object]
    lease_owner: str | None
    lease_token: str | None
    lease_expiry: datetime | None
    receipt_ref: str | None
    failure_code: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for value, label in (
            (self.operation_id, "operation_id"),
            (self.workspace_id, "operation workspace_id"),
            (self.root_task_id, "operation root_task_id"),
        ):
            if not isinstance(value, UUID):
                raise TypeError(f"{label} must be a UUID")
        if not isinstance(self.operation_kind, str) or not self.operation_kind.strip():
            raise ValueError("operation_kind must be a non-empty string")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("operation attempt must be positive")
        if not isinstance(self.status, ResultOperationStatus):
            raise TypeError("operation status must be a ResultOperationStatus")
        if not isinstance(self.payload, Mapping):
            raise TypeError("operation payload must be a mapping")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("operation version must be positive")
        for value, label in (
            (self.created_at, "operation created_at"),
            (self.updated_at, "operation updated_at"),
        ):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{label} must be timezone-aware")
        if (self.lease_owner is None) != (self.lease_token is None) or (
            self.lease_owner is None
        ) != (self.lease_expiry is None):
            raise ValueError("operation lease fields must be present together")

    def to_mapping(self) -> dict[str, object]:
        return {
            "operation_id": str(self.operation_id),
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "operation_kind": self.operation_kind,
            "attempt": self.attempt,
            "status": self.status.value,
            "payload": dict(self.payload),
            "lease_owner": self.lease_owner,
            "lease_token": self.lease_token,
            "lease_expiry": (
                None
                if self.lease_expiry is None
                else self.lease_expiry.astimezone(UTC).isoformat()
            ),
            "receipt_ref": self.receipt_ref,
            "failure_code": self.failure_code,
            "version": self.version,
            "created_at": self.created_at.astimezone(UTC).isoformat(),
            "updated_at": self.updated_at.astimezone(UTC).isoformat(),
        }


type ResultRecord = (
    CodeResultSnapshotV1
    | IntegrationCandidateV1
    | VerificationResultV1
    | TaskAcceptanceDecisionV1
    | ReworkRecordV1
    | ResultApplyOperationV1
)
type ResultOperation = ResultOperationAttemptV1 | ResultApplyOperationV1


class ResultRepositoryPort(Protocol):
    def get_head(
        self, workspace_id: UUID, root_task_id: UUID, *, for_update: bool = False
    ) -> TaskResultHead | None:
        raise NotImplementedError

    def add_immutable(self, kind: str, record: ResultRecord) -> ResultRecord:
        raise NotImplementedError

    def get_record(
        self, kind: str, record_id: UUID, workspace_id: UUID
    ) -> ResultRecord | None:
        raise NotImplementedError

    def get_code_result_by_capture_operation(
        self, operation_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> CodeResultSnapshotV1 | None:
        raise NotImplementedError

    def get_operation(
        self, operation_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ResultOperation | None:
        raise NotImplementedError

    def save_head(self, head: TaskResultHead, *, expected_version: int) -> None:
        raise NotImplementedError

    def claim_operations(
        self, kind: str, now: datetime, limit: int
    ) -> tuple[ResultOperationAttemptV1, ...]:
        raise NotImplementedError

    def save_operation(
        self, operation: ResultOperation, *, expected_version: int
    ) -> ResultOperation:
        raise NotImplementedError

    def list_rework(
        self, root_task_id: UUID, workspace_id: UUID
    ) -> tuple[ReworkRecordV1, ...]:
        raise NotImplementedError

    def retained_code_results(
        self, workspace_id: UUID
    ) -> tuple[CodeResultSnapshotV1, ...]:
        raise NotImplementedError


__all__ = [
    "CapturedCode",
    "CodeResultCapturePort",
    "ResultApplyOperationV1",
    "ResultError",
    "ResultOperation",
    "ResultOperationAttemptV1",
    "ResultOperationStatus",
    "ResultRecord",
    "ResultRepositoryPort",
    "TaskResultHead",
]
