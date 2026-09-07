from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from sidera_control_plane.domain.errors import DomainError, ValidationError


class ResultError(DomainError):
    """A public, stable error for result integration commands and adapters."""

    def __init__(self, code: str, details: Mapping[str, object] | None = None) -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("result error code is required")
        self.code = code.strip()
        super().__init__(self.code, details=dict(details or {}))


class ReworkMode(StrEnum):
    AUTOMATIC = "automatic"
    USER = "user"


class ReworkStatus(StrEnum):
    REQUESTED = "requested"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"


class ResultOperationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ReworkSuggestionV1:
    """A structured rework proposal that still requires control-plane authorization."""

    candidate_id: UUID
    candidate_hash: str
    issue_keys: tuple[str, ...]
    target_worker_item_ids: tuple[UUID, ...]
    reason: str

    @classmethod
    def from_mapping(cls, value: object) -> ReworkSuggestionV1:
        fields = frozenset(
            {
                "candidate_id",
                "candidate_hash",
                "issue_keys",
                "target_worker_item_ids",
                "reason",
            }
        )
        mapping = _mapping(value, "rework suggestion", fields)
        return cls(
            candidate_id=_parse_uuid(mapping["candidate_id"], "suggestion candidate_id"),
            candidate_hash=_hash(mapping["candidate_hash"], "suggestion candidate_hash"),
            issue_keys=tuple(
                _text(item, "suggestion issue key")
                for item in _sequence(mapping["issue_keys"], "suggestion issue_keys")
            ),
            target_worker_item_ids=_unique_uuids(
                mapping["target_worker_item_ids"],
                "suggestion target_worker_item_ids",
            ),
            reason=_text(mapping["reason"], "suggestion reason"),
        )

    def __post_init__(self) -> None:
        _uuid(self.candidate_id, "suggestion candidate_id")
        _hash(self.candidate_hash, "suggestion candidate_hash")
        if not isinstance(self.issue_keys, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.issue_keys
        ):
            raise ValidationError("suggestion issue_keys must contain non-empty strings")
        _unique_uuids(self.target_worker_item_ids, "suggestion target_worker_item_ids")
        _text(self.reason, "suggestion reason")

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate_id": str(self.candidate_id),
            "candidate_hash": self.candidate_hash,
            "issue_keys": list(self.issue_keys),
            "target_worker_item_ids": [str(item) for item in self.target_worker_item_ids],
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ReworkImpactV1:
    """The deterministic impact report for replacing one code result."""

    changed_result_id: UUID
    replacement_result_id: UUID
    affected: tuple[UUID, ...]
    reusable: tuple[UUID, ...]
    requires_revision: bool
    reason_by_worker: Mapping[UUID, str]

    @classmethod
    def from_mapping(cls, value: object) -> ReworkImpactV1:
        fields = frozenset(
            {
                "changed_result_id",
                "replacement_result_id",
                "affected",
                "reusable",
                "requires_revision",
                "reason_by_worker",
            }
        )
        mapping = _mapping(value, "rework impact", fields)
        reasons = mapping["reason_by_worker"]
        if not isinstance(reasons, Mapping):
            raise ValidationError("rework impact reason_by_worker must be an object")
        return cls(
            changed_result_id=_parse_uuid(
                mapping["changed_result_id"], "rework impact changed_result_id"
            ),
            replacement_result_id=_parse_uuid(
                mapping["replacement_result_id"], "rework impact replacement_result_id"
            ),
            affected=_unique_uuids(mapping["affected"], "rework impact affected"),
            reusable=_unique_uuids(mapping["reusable"], "rework impact reusable"),
            requires_revision=mapping["requires_revision"],  # type: ignore[arg-type]
            reason_by_worker={
                _parse_uuid(key, "rework impact reason worker")
                if not isinstance(key, UUID)
                else key: _text(reason, "rework impact reason")
                for key, reason in reasons.items()
            },
        )

    def __post_init__(self) -> None:
        _uuid(self.changed_result_id, "rework impact changed_result_id")
        _uuid(self.replacement_result_id, "rework impact replacement_result_id")
        if self.changed_result_id == self.replacement_result_id:
            raise ValidationError("rework impact must use a new replacement result")
        if type(self.requires_revision) is not bool:
            raise ValidationError("rework impact requires_revision must be a boolean")

        affected = tuple(sorted(_unique_uuids(self.affected, "rework impact affected"), key=str))
        reusable = tuple(sorted(_unique_uuids(self.reusable, "rework impact reusable"), key=str))
        if self.changed_result_id in affected or self.changed_result_id in reusable:
            raise ValidationError("rework impact cannot classify the changed result as a worker")
        if set(affected) & set(reusable):
            raise ValidationError("rework impact affected and reusable must be disjoint")
        object.__setattr__(self, "affected", affected)
        object.__setattr__(self, "reusable", reusable)

        if not isinstance(self.reason_by_worker, Mapping):
            raise ValidationError("rework impact reason_by_worker must be an object")
        allowed_workers = set(affected) | set(reusable)
        normalized_reasons: dict[UUID, str] = {}
        for worker_id, reason in self.reason_by_worker.items():
            _uuid(worker_id, "rework impact reason worker")
            if worker_id not in allowed_workers:
                raise ValidationError("rework impact reason references an unknown worker")
            normalized_reasons[worker_id] = _text(reason, "rework impact reason")
        object.__setattr__(
            self,
            "reason_by_worker",
            MappingProxyType(
                dict(sorted(normalized_reasons.items(), key=lambda item: str(item[0])))
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "changed_result_id": str(self.changed_result_id),
            "replacement_result_id": str(self.replacement_result_id),
            "affected": [str(item) for item in self.affected],
            "reusable": [str(item) for item in self.reusable],
            "requires_revision": self.requires_revision,
            "reason_by_worker": {
                str(worker_id): reason
                for worker_id, reason in self.reason_by_worker.items()
            },
        }


def _uuid(value: object, label: str) -> UUID:
    if not isinstance(value, UUID):
        raise ValidationError(f"{label} must be a UUID")
    return value


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValidationError(f"{label} must be a non-empty string")
    return value.strip()


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"{label} must be a positive integer")
    return value


def _non_negative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{label} must be a non-negative integer")
    return value


def _aware(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_aware(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    return _aware(value, label)


def _sequence(value: object, label: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValidationError(f"{label} must be an array")
    return tuple(value)


def _unique_uuids(value: object, label: str) -> tuple[UUID, ...]:
    result = tuple(_parse_uuid(item, f"{label} item") for item in _sequence(value, label))
    if len(set(result)) != len(result):
        raise ValidationError(f"{label} must not contain duplicates")
    return result


def _parse_uuid(value: object, label: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationError(f"{label} must be a UUID") from exc


def _parse_datetime(value: object, label: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{label} must be an ISO datetime") from exc
    return _aware(value, label)


def _parse_optional_datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value, label)


def _mapping(value: object, label: str, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be an object")
    keys = set(value)
    unknown = sorted(keys - fields)
    missing = sorted(fields - keys)
    if unknown:
        raise ValidationError(f"unknown {label} fields: {', '.join(unknown)}")
    if missing:
        raise ValidationError(f"missing {label} fields: {', '.join(missing)}")
    if not all(isinstance(key, str) for key in keys):
        raise ValidationError(f"{label} field names must be strings")
    return value


def _oid(value: object, label: str) -> str:
    import re

    result = _text(value, label).lower()
    if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", result) is None:
        raise ValidationError(f"{label} must be a Git object id")
    return result


def _ref(value: object, label: str) -> str:
    result = _text(value, label, allow_empty=False)
    if not result.startswith("refs/") or "\n" in result or "\r" in result:
        raise ValidationError(f"{label} must be a full Git ref")
    return result


def _hash(value: object, label: str) -> str:
    import re

    result = _text(value, label)
    if re.fullmatch(r"[0-9a-f]{64}", result) is None:
        raise ValidationError(f"{label} must be a lowercase SHA-256 hash")
    return result


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


@dataclass(frozen=True, slots=True)
class TaskResultHead:
    workspace_id: UUID
    root_task_id: UUID
    current_candidate_id: UUID | None
    current_acceptance_id: UUID | None
    result_version: int
    active_operation_id: UUID | None
    auto_rounds_used: int
    auto_rounds_limit: int
    auto_seconds_limit: int
    auto_started_at: datetime | None
    auto_deadline: datetime | None

    def __post_init__(self) -> None:
        _uuid(self.workspace_id, "result head workspace_id")
        _uuid(self.root_task_id, "result head root_task_id")
        for value, label in (
            (self.current_candidate_id, "result head current_candidate_id"),
            (self.current_acceptance_id, "result head current_acceptance_id"),
            (self.active_operation_id, "result head active_operation_id"),
        ):
            if value is not None:
                _uuid(value, label)
        _positive(self.result_version, "result head result_version")
        _non_negative(self.auto_rounds_used, "result head auto_rounds_used")
        _non_negative(self.auto_rounds_limit, "result head auto_rounds_limit")
        _positive(self.auto_seconds_limit, "result head auto_seconds_limit")
        started = _optional_aware(self.auto_started_at, "result head auto_started_at")
        deadline = _optional_aware(self.auto_deadline, "result head auto_deadline")
        if (started is None) != (deadline is None):
            raise ValidationError("result head timer requires start and deadline together")
        if started is not None and deadline is not None and deadline <= started:
            raise ValidationError("result head deadline must be after start")


@dataclass(frozen=True, slots=True)
class ReworkRecordV1:
    id: UUID
    workspace_id: UUID
    root_task_id: UUID
    candidate_id: UUID
    source_issue_keys: tuple[str, ...]
    target_worker_item_ids: tuple[UUID, ...]
    impact_worker_item_ids: tuple[UUID, ...]
    reason: str
    mode: ReworkMode
    round_number: int | None
    source_command_id: UUID
    request_ids: tuple[UUID, ...]
    status: ReworkStatus
    created_at: datetime

    @classmethod
    def from_mapping(cls, value: object) -> ReworkRecordV1:
        fields = frozenset(
            {
                "id",
                "workspace_id",
                "root_task_id",
                "candidate_id",
                "source_issue_keys",
                "target_worker_item_ids",
                "impact_worker_item_ids",
                "reason",
                "mode",
                "round_number",
                "source_command_id",
                "request_ids",
                "status",
                "created_at",
            }
        )
        mapping = _mapping(value, "rework record", fields)
        try:
            mode = ReworkMode(mapping["mode"])
            status = ReworkStatus(mapping["status"])
        except (TypeError, ValueError) as exc:
            raise ValidationError("rework mode or status is unsupported") from exc
        round_number = mapping["round_number"]
        if round_number is not None:
            round_number = _positive(round_number, "rework round_number")
        return cls(
            id=_parse_uuid(mapping["id"], "rework id"),
            workspace_id=_parse_uuid(mapping["workspace_id"], "rework workspace_id"),
            root_task_id=_parse_uuid(mapping["root_task_id"], "rework root_task_id"),
            candidate_id=_parse_uuid(mapping["candidate_id"], "rework candidate_id"),
            source_issue_keys=tuple(
                _text(item, "rework source issue key")
                for item in _sequence(mapping["source_issue_keys"], "rework source_issue_keys")
            ),
            target_worker_item_ids=_unique_uuids(
                mapping["target_worker_item_ids"], "rework target_worker_item_ids"
            ),
            impact_worker_item_ids=_unique_uuids(
                mapping["impact_worker_item_ids"], "rework impact_worker_item_ids"
            ),
            reason=_text(mapping["reason"], "rework reason"),
            mode=mode,
            round_number=round_number,
            source_command_id=_parse_uuid(
                mapping["source_command_id"], "rework source_command_id"
            ),
            request_ids=_unique_uuids(mapping["request_ids"], "rework request_ids"),
            status=status,
            created_at=_parse_datetime(mapping["created_at"], "rework created_at"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "candidate_id": str(self.candidate_id),
            "source_issue_keys": list(self.source_issue_keys),
            "target_worker_item_ids": [str(item) for item in self.target_worker_item_ids],
            "impact_worker_item_ids": [str(item) for item in self.impact_worker_item_ids],
            "reason": self.reason,
            "mode": self.mode.value,
            "round_number": self.round_number,
            "source_command_id": str(self.source_command_id),
            "request_ids": [str(item) for item in self.request_ids],
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ResultApplyOperationV1:
    id: UUID
    workspace_id: UUID
    root_task_id: UUID
    acceptance_id: UUID
    candidate_id: UUID
    repository_ref: str
    target_ref: str
    expected_old_commit: str
    accepted_commit: str
    status: ResultOperationStatus
    attempt: int
    lease_owner: str | None
    lease_token: str | None
    lease_expiry: datetime | None
    receipt_ref: str | None
    failure_code: str | None
    version: int

    @classmethod
    def from_mapping(cls, value: object) -> ResultApplyOperationV1:
        fields = frozenset(
            {
                "id",
                "workspace_id",
                "root_task_id",
                "acceptance_id",
                "candidate_id",
                "repository_ref",
                "target_ref",
                "expected_old_commit",
                "accepted_commit",
                "status",
                "attempt",
                "lease_owner",
                "lease_token",
                "lease_expiry",
                "receipt_ref",
                "failure_code",
                "version",
            }
        )
        mapping = _mapping(value, "result apply operation", fields)
        try:
            status = ResultOperationStatus(mapping["status"])
        except (TypeError, ValueError) as exc:
            raise ValidationError("result apply operation status is unsupported") from exc
        return cls(
            id=_parse_uuid(mapping["id"], "apply operation id"),
            workspace_id=_parse_uuid(mapping["workspace_id"], "apply workspace_id"),
            root_task_id=_parse_uuid(mapping["root_task_id"], "apply root_task_id"),
            acceptance_id=_parse_uuid(mapping["acceptance_id"], "apply acceptance_id"),
            candidate_id=_parse_uuid(mapping["candidate_id"], "apply candidate_id"),
            repository_ref=_text(mapping["repository_ref"], "apply repository_ref"),
            target_ref=_ref(mapping["target_ref"], "apply target_ref"),
            expected_old_commit=_oid(mapping["expected_old_commit"], "apply expected_old_commit"),
            accepted_commit=_oid(mapping["accepted_commit"], "apply accepted_commit"),
            status=status,
            attempt=_positive(mapping["attempt"], "apply attempt"),
            lease_owner=_optional_text(mapping["lease_owner"], "apply lease_owner"),
            lease_token=_optional_text(mapping["lease_token"], "apply lease_token"),
            lease_expiry=_parse_optional_datetime(mapping["lease_expiry"], "apply lease_expiry"),
            receipt_ref=_optional_text(mapping["receipt_ref"], "apply receipt_ref"),
            failure_code=_optional_text(mapping["failure_code"], "apply failure_code"),
            version=_positive(mapping["version"], "apply version"),
        )

    def __post_init__(self) -> None:
        for value, label in (
            (self.id, "apply operation id"),
            (self.workspace_id, "apply workspace_id"),
            (self.root_task_id, "apply root_task_id"),
            (self.acceptance_id, "apply acceptance_id"),
            (self.candidate_id, "apply candidate_id"),
        ):
            _uuid(value, label)
        _text(self.repository_ref, "apply repository_ref")
        _ref(self.target_ref, "apply target_ref")
        _oid(self.expected_old_commit, "apply expected_old_commit")
        _oid(self.accepted_commit, "apply accepted_commit")
        if not isinstance(self.status, ResultOperationStatus):
            raise ValidationError("result apply operation status is unsupported")
        _positive(self.attempt, "apply attempt")
        _positive(self.version, "apply version")
        if (self.lease_owner is None) != (self.lease_token is None):
            raise ValidationError("apply lease owner and token must be paired")
        if self.lease_expiry is not None and self.lease_owner is None:
            raise ValidationError("apply lease expiry requires a lease")
        _optional_aware(self.lease_expiry, "apply lease_expiry")
        _optional_text(self.receipt_ref, "apply receipt_ref")
        _optional_text(self.failure_code, "apply failure_code")

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "acceptance_id": str(self.acceptance_id),
            "candidate_id": str(self.candidate_id),
            "repository_ref": self.repository_ref,
            "target_ref": self.target_ref,
            "expected_old_commit": self.expected_old_commit,
            "accepted_commit": self.accepted_commit,
            "status": self.status.value,
            "attempt": self.attempt,
            "lease_owner": self.lease_owner,
            "lease_token": self.lease_token,
            "lease_expiry": None if self.lease_expiry is None else self.lease_expiry.isoformat(),
            "receipt_ref": self.receipt_ref,
            "failure_code": self.failure_code,
            "version": self.version,
        }


def reserve_rework_round(head: TaskResultHead, now: datetime) -> TaskResultHead:
    now = _aware(now, "rework reservation time")
    if head.auto_rounds_used >= head.auto_rounds_limit:
        raise ResultError("rework_budget_exhausted", {})
    started_at = head.auto_started_at if head.auto_started_at is not None else now
    deadline = head.auto_deadline
    if deadline is None:
        deadline = started_at + timedelta(seconds=head.auto_seconds_limit)
    if now >= deadline:
        raise ResultError("rework_deadline_exceeded", {})
    return replace(
        head,
        auto_rounds_used=head.auto_rounds_used + 1,
        auto_started_at=started_at,
        auto_deadline=deadline,
        result_version=head.result_version + 1,
    )


def extend_rework_budget(
    head: TaskResultHead, *, additional_rounds: int, additional_seconds: int
) -> TaskResultHead:
    additional_rounds = _non_negative(additional_rounds, "additional_rounds")
    additional_seconds = _non_negative(additional_seconds, "additional_seconds")
    if additional_rounds == 0 and additional_seconds == 0:
        raise ResultError("rework_budget_increment_required", {})
    deadline = head.auto_deadline
    if deadline is not None:
        deadline += timedelta(seconds=additional_seconds)
    return replace(
        head,
        auto_rounds_limit=head.auto_rounds_limit + additional_rounds,
        auto_seconds_limit=head.auto_seconds_limit + additional_seconds,
        auto_deadline=deadline,
        result_version=head.result_version + 1,
    )


def transition_operation(
    operation: ResultApplyOperationV1, status: ResultOperationStatus
) -> ResultApplyOperationV1:
    if not isinstance(status, ResultOperationStatus):
        raise ResultError("result_operation_status_invalid", {})
    if operation.status is status:
        return operation
    if operation.status is ResultOperationStatus.SUCCEEDED:
        raise ResultError("result_operation_terminal", {"operation_id": str(operation.id)})
    if operation.status is ResultOperationStatus.UNKNOWN:
        if status is ResultOperationStatus.SUCCEEDED:
            # Inspection can prove that the external operation completed.  This
            # records that fact; it never starts the external operation again.
            return replace(operation, status=status, version=operation.version + 1)
        raise ResultError(
            "result_operation_inspect_required", {"operation_id": str(operation.id)}
        )
    allowed: dict[ResultOperationStatus, frozenset[ResultOperationStatus]] = {
        ResultOperationStatus.PENDING: frozenset(
            {
                ResultOperationStatus.RUNNING,
                ResultOperationStatus.BLOCKED,
                ResultOperationStatus.UNKNOWN,
                ResultOperationStatus.FAILED,
            }
        ),
        ResultOperationStatus.RUNNING: frozenset(
            {
                ResultOperationStatus.SUCCEEDED,
                ResultOperationStatus.BLOCKED,
                ResultOperationStatus.UNKNOWN,
                ResultOperationStatus.FAILED,
            }
        ),
        ResultOperationStatus.BLOCKED: frozenset(
            {ResultOperationStatus.RUNNING, ResultOperationStatus.FAILED}
        ),
        ResultOperationStatus.FAILED: frozenset({ResultOperationStatus.RUNNING}),
    }
    if status not in allowed.get(operation.status, frozenset()):
        raise ResultError(
            "result_operation_transition_invalid",
            {"operation_id": str(operation.id), "from": operation.status.value, "to": status.value},
        )
    return replace(operation, status=status, version=operation.version + 1)


__all__ = [
    "ResultApplyOperationV1",
    "ResultError",
    "ResultOperationStatus",
    "ReworkImpactV1",
    "ReworkMode",
    "ReworkRecordV1",
    "ReworkStatus",
    "ReworkSuggestionV1",
    "TaskResultHead",
    "extend_rework_budget",
    "reserve_rework_round",
    "transition_operation",
]
