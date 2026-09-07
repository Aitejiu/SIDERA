from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID

from sidera_control_plane.domain.code_results import (
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.result_integration import CheckSpecV1
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
    from sidera_control_plane.domain.code_results import CodeInputManifestV1
    from sidera_control_plane.domain.workspace import ExecutionWorkspaceManifest


_GIT_OID_PATTERN = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CHECK_REFERENCE_PATTERN = re.compile(r"^[^\x00]+$")


class CheckRunStatus(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ENVIRONMENT_BLOCKED = "environment_blocked"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class ReportAvailabilityStatus(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    OUT_OF_BOUNDS = "out_of_bounds"
    TOO_LARGE = "too_large"
    READ_ERROR = "read_error"


def _require_uuid(value: object, label: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{label} must be a UUID")
    return value


def _require_text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{label} must be a non-empty string")
    return value if allow_empty else value.strip()


def _require_hash(value: object, label: str) -> str:
    result = _require_text(value, label).lower()
    if _SHA256_PATTERN.fullmatch(result) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 hash")
    return result


def _require_git_oid(value: object, label: str) -> str:
    result = _require_text(value, label).lower()
    if _GIT_OID_PATTERN.fullmatch(result) is None:
        raise ValueError(f"{label} must be a Git object id")
    return result


def _require_relative_reference(value: object, label: str) -> str:
    result = _require_text(value, label)
    if (
        not _CHECK_REFERENCE_PATTERN.fullmatch(result)
        or result.startswith(("/", "\\", "http:", "https:", "file:"))
        or "\\" in result
        or "://" in result
    ):
        raise ValueError(f"{label} must be a relative local path")
    path = PurePosixPath(result)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a relative local path")
    return result


def _require_aware_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ProcessStartIdentity:
    pid: int
    process_group_id: int
    started_at: datetime
    nonce: str

    def __post_init__(self) -> None:
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise ValueError("process identity pid must be positive")
        if (
            isinstance(self.process_group_id, bool)
            or not isinstance(self.process_group_id, int)
            or self.process_group_id <= 0
        ):
            raise ValueError("process identity process_group_id must be positive")
        object.__setattr__(
            self, "started_at", _require_aware_datetime(self.started_at, "process identity started_at")
        )
        _require_text(self.nonce, "process identity nonce")

    def to_mapping(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "process_group_id": self.process_group_id,
            "started_at": self.started_at.isoformat(),
            "nonce": self.nonce,
        }

    @classmethod
    def from_mapping(cls, value: object) -> ProcessStartIdentity:
        if not isinstance(value, Mapping):
            raise TypeError("process identity must be an object")
        started_at = value.get("started_at")
        if isinstance(started_at, str):
            started_at = datetime.fromisoformat(started_at)
        return cls(
            pid=value.get("pid"),  # type: ignore[arg-type]
            process_group_id=value.get("process_group_id"),  # type: ignore[arg-type]
            started_at=started_at,  # type: ignore[arg-type]
            nonce=value.get("nonce"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ReportAvailability:
    path: str
    status: ReportAvailabilityStatus
    size_bytes: int | None
    sha256: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "path", _require_relative_reference(self.path, "report path")
        )
        if not isinstance(self.status, ReportAvailabilityStatus):
            object.__setattr__(
                self, "status", ReportAvailabilityStatus(self.status)  # type: ignore[arg-type]
            )
        if self.size_bytes is not None and (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("report size_bytes must be non-negative or null")
        if self.sha256 is not None:
            object.__setattr__(self, "sha256", _require_hash(self.sha256, "report sha256"))
        if self.status is ReportAvailabilityStatus.PRESENT and self.sha256 is None:
            raise ValueError("present report must have a sha256")

    def to_mapping(self) -> dict[str, object]:
        return {
            "path": self.path,
            "status": self.status.value,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> ReportAvailability:
        if not isinstance(value, Mapping):
            raise TypeError("report availability must be an object")
        return cls(
            path=value.get("path"),  # type: ignore[arg-type]
            status=ReportAvailabilityStatus(value.get("status")),  # type: ignore[arg-type]
            size_bytes=value.get("size_bytes"),  # type: ignore[arg-type]
            sha256=value.get("sha256"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class CheckRunRequest:
    operation_id: UUID
    candidate_id: UUID
    candidate_hash: str
    actual_commit: str
    actual_tree: str
    check: CheckSpecV1
    workspace_location: Path
    deadline: datetime
    log_ref: str

    def __post_init__(self) -> None:
        _require_uuid(self.operation_id, "check operation_id")
        _require_uuid(self.candidate_id, "check candidate_id")
        object.__setattr__(
            self, "candidate_hash", _require_hash(self.candidate_hash, "check candidate_hash")
        )
        object.__setattr__(
            self, "actual_commit", _require_git_oid(self.actual_commit, "check actual_commit")
        )
        object.__setattr__(
            self, "actual_tree", _require_git_oid(self.actual_tree, "check actual_tree")
        )
        if not isinstance(self.check, CheckSpecV1):
            raise TypeError("check request check must be a CheckSpecV1")
        if not isinstance(self.workspace_location, Path):
            raise TypeError("check workspace_location must be a Path")
        if not self.workspace_location.is_absolute():
            raise ValueError("check workspace_location must be absolute")
        object.__setattr__(
            self, "deadline", _require_aware_datetime(self.deadline, "check deadline")
        )
        object.__setattr__(
            self, "log_ref", _require_relative_reference(self.log_ref, "check log_ref")
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "operation_id": str(self.operation_id),
            "candidate_id": str(self.candidate_id),
            "candidate_hash": self.candidate_hash,
            "actual_commit": self.actual_commit,
            "actual_tree": self.actual_tree,
            "check": self.check.to_mapping(),
            "workspace_location": str(self.workspace_location),
            "deadline": self.deadline.isoformat(),
            "log_ref": self.log_ref,
        }

    @classmethod
    def from_mapping(cls, value: object) -> CheckRunRequest:
        if not isinstance(value, Mapping):
            raise TypeError("check request must be an object")
        deadline = value.get("deadline")
        if isinstance(deadline, str):
            deadline = datetime.fromisoformat(deadline)
        workspace_location = value.get("workspace_location")
        if isinstance(workspace_location, str):
            workspace_location = Path(workspace_location)
        return cls(
            operation_id=UUID(str(value.get("operation_id"))),
            candidate_id=UUID(str(value.get("candidate_id"))),
            candidate_hash=value.get("candidate_hash"),  # type: ignore[arg-type]
            actual_commit=value.get("actual_commit"),  # type: ignore[arg-type]
            actual_tree=value.get("actual_tree"),  # type: ignore[arg-type]
            check=CheckSpecV1.from_mapping(value.get("check")),
            workspace_location=workspace_location,  # type: ignore[arg-type]
            deadline=deadline,  # type: ignore[arg-type]
            log_ref=value.get("log_ref"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class CheckRunObservation:
    operation_id: UUID
    status: CheckRunStatus
    pid: int | None
    process_start_identity: ProcessStartIdentity | None
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    log_refs: tuple[str, ...]
    actual_commit: str
    actual_tree: str
    source_dirty: bool | None
    report_availability: tuple[ReportAvailability, ...]
    failure_code: str | None

    def __post_init__(self) -> None:
        _require_uuid(self.operation_id, "check observation operation_id")
        if not isinstance(self.status, CheckRunStatus):
            object.__setattr__(
                self, "status", CheckRunStatus(self.status)  # type: ignore[arg-type]
            )
        if self.pid is not None and (
            isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0
        ):
            raise ValueError("check observation pid must be positive or null")
        if self.process_start_identity is not None and not isinstance(
            self.process_start_identity, ProcessStartIdentity
        ):
            raise TypeError("check observation process identity is invalid")
        if (
            self.pid is not None
            and self.process_start_identity is not None
            and self.pid != self.process_start_identity.pid
        ):
            raise ValueError("check observation pid does not match process identity")
        for value, label in (
            (self.started_at, "check observation started_at"),
            (self.finished_at, "check observation finished_at"),
        ):
            if value is not None:
                _require_aware_datetime(value, label)
        if self.status is CheckRunStatus.RUNNING and (
            self.exit_code is not None or self.finished_at is not None
        ):
            raise ValueError("running check observation cannot have exit facts")
        if self.exit_code is not None and isinstance(self.exit_code, bool):
            raise ValueError("check observation exit_code must be an integer or null")
        if not isinstance(self.log_refs, tuple):
            raise TypeError("check observation log_refs must be a tuple")
        object.__setattr__(
            self,
            "log_refs",
            tuple(_require_relative_reference(item, "check log_ref") for item in self.log_refs),
        )
        object.__setattr__(
            self, "actual_commit", _require_git_oid(self.actual_commit, "check observation actual_commit")
        )
        object.__setattr__(
            self, "actual_tree", _require_git_oid(self.actual_tree, "check observation actual_tree")
        )
        if self.source_dirty is not None and not isinstance(self.source_dirty, bool):
            raise ValueError("check observation source_dirty must be boolean or null")
        if not isinstance(self.report_availability, tuple):
            raise TypeError("check observation report_availability must be a tuple")
        if self.failure_code is not None:
            _require_text(self.failure_code, "check observation failure_code")

    def to_mapping(self) -> dict[str, object]:
        return {
            "operation_id": str(self.operation_id),
            "status": self.status.value,
            "pid": self.pid,
            "process_start_identity": (
                None
                if self.process_start_identity is None
                else self.process_start_identity.to_mapping()
            ),
            "started_at": None if self.started_at is None else self.started_at.isoformat(),
            "finished_at": None if self.finished_at is None else self.finished_at.isoformat(),
            "exit_code": self.exit_code,
            "log_refs": list(self.log_refs),
            "actual_commit": self.actual_commit,
            "actual_tree": self.actual_tree,
            "source_dirty": self.source_dirty,
            "report_availability": [item.to_mapping() for item in self.report_availability],
            "failure_code": self.failure_code,
        }

    @classmethod
    def from_mapping(cls, value: object) -> CheckRunObservation:
        if not isinstance(value, Mapping):
            raise TypeError("check observation must be an object")
        started_at = value.get("started_at")
        finished_at = value.get("finished_at")
        if isinstance(started_at, str):
            started_at = datetime.fromisoformat(started_at)
        if isinstance(finished_at, str):
            finished_at = datetime.fromisoformat(finished_at)
        identity = value.get("process_start_identity")
        return cls(
            operation_id=UUID(str(value.get("operation_id"))),
            status=CheckRunStatus(value.get("status")),  # type: ignore[arg-type]
            pid=value.get("pid"),  # type: ignore[arg-type]
            process_start_identity=(
                None if identity is None else ProcessStartIdentity.from_mapping(identity)
            ),
            started_at=started_at,  # type: ignore[arg-type]
            finished_at=finished_at,  # type: ignore[arg-type]
            exit_code=value.get("exit_code"),  # type: ignore[arg-type]
            log_refs=tuple(value.get("log_refs", ())),  # type: ignore[arg-type]
            actual_commit=value.get("actual_commit"),  # type: ignore[arg-type]
            actual_tree=value.get("actual_tree"),  # type: ignore[arg-type]
            source_dirty=value.get("source_dirty"),  # type: ignore[arg-type]
            report_availability=tuple(
                ReportAvailability.from_mapping(item)
                for item in cast(Sequence[object], value.get("report_availability", ()))
            ),
            failure_code=value.get("failure_code"),  # type: ignore[arg-type]
        )


class LocalCheckRunnerPort(Protocol):
    def start(self, request: CheckRunRequest) -> CheckRunObservation:
        raise NotImplementedError

    def inspect(self, operation_id: UUID) -> CheckRunObservation | None:
        raise NotImplementedError

    def stop(self, operation_id: UUID) -> CheckRunObservation | None:
        raise NotImplementedError


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


@dataclass(frozen=True, slots=True)
class CodeInputReceipt:
    """Immutable receipt for one physical code-input materialization."""

    execution_id: UUID
    input_hash: str
    actual_commit: str
    actual_tree: str
    conflict_paths: tuple[str, ...]
    operation_id: UUID
    _repository_path: Path | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        for value, label in (
            (self.execution_id, "input execution_id"),
            (self.operation_id, "input operation_id"),
        ):
            if not isinstance(value, UUID):
                raise TypeError(f"{label} must be a UUID")
        if not isinstance(self.input_hash, str) or _SHA256_PATTERN.fullmatch(
            self.input_hash
        ) is None:
            raise ValueError("input input_hash must be a lowercase SHA-256 hash")
        for value, label in (
            (self.actual_commit, "input actual_commit"),
            (self.actual_tree, "input actual_tree"),
        ):
            if not isinstance(value, str) or _GIT_OID_PATTERN.fullmatch(value) is None:
                raise ValueError(f"{label} must be a Git object id")
        if not isinstance(self.conflict_paths, tuple):
            raise TypeError("input conflict_paths must be a tuple")
        normalized = tuple(sorted(set(self.conflict_paths)))
        if any(
            not isinstance(path, str)
            or not path.strip()
            or path.startswith("/")
            or "\x00" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            for path in normalized
        ):
            raise ValueError("input conflict_paths must be relative Git paths")
        object.__setattr__(self, "conflict_paths", normalized)
        if self._repository_path is not None and not isinstance(
            self._repository_path, Path
        ):
            raise TypeError("input repository path must be a Path")

    @property
    def succeeded(self) -> bool:
        return not self.conflict_paths

    def to_mapping(self) -> dict[str, object]:
        return {
            "execution_id": str(self.execution_id),
            "input_hash": self.input_hash,
            "actual_commit": self.actual_commit,
            "actual_tree": self.actual_tree,
            "conflict_paths": list(self.conflict_paths),
            "operation_id": str(self.operation_id),
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


class CodeResultMaterializationPort(Protocol):
    def materialize(
        self,
        *,
        workspace_manifest: ExecutionWorkspaceManifest,
        code_inputs: CodeInputManifestV1,
        operation_id: UUID,
    ) -> CodeInputReceipt:
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

    def get_verification_by_operation(
        self, operation_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> VerificationResultV1 | None:
        raise NotImplementedError

    def list_verification_results(
        self, candidate_id: UUID, workspace_id: UUID
    ) -> tuple[VerificationResultV1, ...]:
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

    def get_rework_by_source_command(
        self, source_command_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ReworkRecordV1 | None:
        raise NotImplementedError

    def retained_code_results(
        self, workspace_id: UUID
    ) -> tuple[CodeResultSnapshotV1, ...]:
        raise NotImplementedError


__all__ = [
    "CapturedCode",
    "CheckRunObservation",
    "CheckRunRequest",
    "CheckRunStatus",
    "CodeInputReceipt",
    "CodeResultCapturePort",
    "CodeResultMaterializationPort",
    "LocalCheckRunnerPort",
    "ProcessStartIdentity",
    "ReportAvailability",
    "ReportAvailabilityStatus",
    "ResultApplyOperationV1",
    "ResultError",
    "ResultOperation",
    "ResultOperationAttemptV1",
    "ResultOperationStatus",
    "ResultRecord",
    "ResultRepositoryPort",
    "TaskResultHead",
]
