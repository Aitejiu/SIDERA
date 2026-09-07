from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.result_integration import (
    CriterionMode,
    CriterionSpecV1,
)

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_OID_PATTERN = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    UNKNOWN = "unknown"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"


class CriterionObservedStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"


class CriterionDecisionType(StrEnum):
    ACCEPT_PASSED = "accept_passed"
    MANUAL_PASSED = "manual_passed"
    EXCEPTION_ACCEPTED = "exception_accepted"


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


def _sequence(value: object, label: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValidationError(f"{label} must be an array")
    return tuple(value)


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValidationError(f"{label} must be a non-empty string")
    return value.strip()


def _uuid(value: object, label: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationError(f"{label} must be a UUID") from exc


def _hash(value: object, label: str) -> str:
    result = _text(value, label)
    if _HASH_PATTERN.fullmatch(result) is None:
        raise ValidationError(f"{label} must be a lowercase SHA-256 hash")
    return result


def _oid(value: object, label: str) -> str:
    result = _text(value, label).lower()
    if _OID_PATTERN.fullmatch(result) is None:
        raise ValidationError(f"{label} must be a Git object id")
    return result


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"{label} must be a positive integer")
    return value


def _optional_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{label} must be an integer or null")
    return value


def _aware_datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{label} must be an ISO datetime") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _unique_uuids(value: object, label: str) -> tuple[UUID, ...]:
    result = tuple(_uuid(item, f"{label} item") for item in _sequence(value, label))
    if len(set(result)) != len(result):
        raise ValidationError(f"{label} must not contain duplicates")
    return result


def _canonical_hash(value: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("verification manifest cannot be canonicalized") from exc
    return hashlib.sha256(encoded).hexdigest()


_CHECK_REF_FIELDS = frozenset({"key", "version", "spec_hash"})


@dataclass(frozen=True, slots=True)
class VerificationCheckRefV1:
    key: str
    version: int
    spec_hash: str

    @classmethod
    def from_mapping(cls, value: object) -> VerificationCheckRefV1:
        mapping = _mapping(value, "verification check", _CHECK_REF_FIELDS)
        return cls(
            key=_text(mapping["key"], "verification check key"),
            version=_positive_int(mapping["version"], "verification check version"),
            spec_hash=_hash(mapping["spec_hash"], "verification check spec_hash"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"key": self.key, "version": self.version, "spec_hash": self.spec_hash}


_ENVIRONMENT_FIELDS = frozenset({"ref", "version"})


@dataclass(frozen=True, slots=True)
class EnvironmentBindingV1:
    ref: str
    version: int

    @classmethod
    def from_mapping(cls, value: object) -> EnvironmentBindingV1:
        mapping = _mapping(value, "environment binding", _ENVIRONMENT_FIELDS)
        return cls(
            ref=_text(mapping["ref"], "environment ref"),
            version=_positive_int(mapping["version"], "environment version"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"ref": self.ref, "version": self.version}


_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "candidate_hash",
        "result_commit",
        "result_tree",
        "checks",
        "environment_binding",
        "verification_basis_hash",
    }
)


@dataclass(frozen=True, slots=True)
class VerificationManifestV1:
    schema_version: int
    candidate_id: UUID
    candidate_hash: str
    result_commit: str
    result_tree: str
    checks: tuple[VerificationCheckRefV1, ...]
    environment_binding: EnvironmentBindingV1
    verification_basis_hash: str

    @classmethod
    def from_mapping(cls, value: object) -> VerificationManifestV1:
        mapping = _mapping(value, "verification manifest", _MANIFEST_FIELDS)
        schema_version = mapping["schema_version"]
        if isinstance(schema_version, bool) or schema_version != 1:
            raise ValidationError("unsupported verification manifest schema_version")
        checks = tuple(
            VerificationCheckRefV1.from_mapping(item)
            for item in _sequence(mapping["checks"], "verification checks")
        )
        if not checks:
            raise ValidationError("verification checks must not be empty")
        if len({item.key for item in checks}) != len(checks):
            raise ValidationError("verification checks must not contain duplicates")
        return cls(
            schema_version=1,
            candidate_id=_uuid(mapping["candidate_id"], "verification candidate_id"),
            candidate_hash=_hash(mapping["candidate_hash"], "verification candidate_hash"),
            result_commit=_oid(mapping["result_commit"], "verification result_commit"),
            result_tree=_oid(mapping["result_tree"], "verification result_tree"),
            checks=checks,
            environment_binding=EnvironmentBindingV1.from_mapping(
                mapping["environment_binding"]
            ),
            verification_basis_hash=_hash(
                mapping["verification_basis_hash"], "verification basis hash"
            ),
        )

    def _basis_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": str(self.candidate_id),
            "candidate_hash": self.candidate_hash,
            "result_commit": self.result_commit,
            "result_tree": self.result_tree,
            "checks": [item.to_mapping() for item in self.checks],
            "environment_binding": self.environment_binding.to_mapping(),
        }

    def compute_basis_hash(self) -> str:
        return _canonical_hash(self._basis_mapping())

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValidationError("unsupported verification manifest schema_version")
        _uuid(self.candidate_id, "verification candidate_id")
        _hash(self.candidate_hash, "verification candidate_hash")
        _oid(self.result_commit, "verification result_commit")
        _oid(self.result_tree, "verification result_tree")
        _hash(self.verification_basis_hash, "verification basis hash")
        if not self.checks:
            raise ValidationError("verification checks must not be empty")
        if len({item.key for item in self.checks}) != len(self.checks):
            raise ValidationError("verification checks must not contain duplicates")
        if self.compute_basis_hash() != self.verification_basis_hash:
            raise ValidationError("verification basis hash mismatch")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": str(self.candidate_id),
            "candidate_hash": self.candidate_hash,
            "result_commit": self.result_commit,
            "result_tree": self.result_tree,
            "checks": [item.to_mapping() for item in self.checks],
            "environment_binding": self.environment_binding.to_mapping(),
            "verification_basis_hash": self.verification_basis_hash,
        }


_RESULT_FIELDS = frozenset(
    {
        "id",
        "schema_version",
        "workspace_id",
        "root_task_id",
        "candidate_id",
        "execution_id",
        "check_key",
        "check_version",
        "check_hash",
        "environment_ref",
        "environment_hash",
        "actual_commit",
        "actual_tree",
        "operation_id",
        "attempt",
        "status",
        "exit_code",
        "started_at",
        "finished_at",
        "artifact_ids",
        "failure_code",
    }
)


@dataclass(frozen=True, slots=True)
class VerificationResultV1:
    id: UUID
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    candidate_id: UUID
    execution_id: UUID
    check_key: str
    check_version: int
    check_hash: str
    environment_ref: str
    environment_hash: str
    actual_commit: str
    actual_tree: str
    operation_id: UUID
    attempt: int
    status: VerificationStatus
    exit_code: int | None
    started_at: datetime | None
    finished_at: datetime | None
    artifact_ids: tuple[UUID, ...]
    failure_code: str | None

    @classmethod
    def from_mapping(cls, value: object) -> VerificationResultV1:
        mapping = _mapping(value, "verification result", _RESULT_FIELDS)
        schema_version = mapping["schema_version"]
        if isinstance(schema_version, bool) or schema_version != 1:
            raise ValidationError("unsupported verification result schema_version")
        try:
            status = VerificationStatus(mapping["status"])
        except (TypeError, ValueError) as exc:
            raise ValidationError("verification result status is unsupported") from exc
        failure_code = mapping["failure_code"]
        if failure_code is not None:
            failure_code = _text(failure_code, "verification failure_code")
        return cls(
            id=_uuid(mapping["id"], "verification result id"),
            schema_version=1,
            workspace_id=_uuid(mapping["workspace_id"], "verification result workspace_id"),
            root_task_id=_uuid(mapping["root_task_id"], "verification result root_task_id"),
            candidate_id=_uuid(mapping["candidate_id"], "verification result candidate_id"),
            execution_id=_uuid(mapping["execution_id"], "verification result execution_id"),
            check_key=_text(mapping["check_key"], "verification check_key"),
            check_version=_positive_int(mapping["check_version"], "verification check_version"),
            check_hash=_hash(mapping["check_hash"], "verification check_hash"),
            environment_ref=_text(mapping["environment_ref"], "verification environment_ref"),
            environment_hash=_hash(mapping["environment_hash"], "verification environment_hash"),
            actual_commit=_oid(mapping["actual_commit"], "verification actual_commit"),
            actual_tree=_oid(mapping["actual_tree"], "verification actual_tree"),
            operation_id=_uuid(mapping["operation_id"], "verification operation_id"),
            attempt=_positive_int(mapping["attempt"], "verification attempt"),
            status=status,
            exit_code=_optional_int(mapping["exit_code"], "verification exit_code"),
            started_at=_aware_datetime(mapping["started_at"], "verification started_at"),
            finished_at=_aware_datetime(mapping["finished_at"], "verification finished_at"),
            artifact_ids=_unique_uuids(mapping["artifact_ids"], "verification artifact_ids"),
            failure_code=failure_code,
        )

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValidationError("unsupported verification result schema_version")
        for value, label in (
            (self.id, "verification result id"),
            (self.workspace_id, "verification result workspace_id"),
            (self.root_task_id, "verification result root_task_id"),
            (self.candidate_id, "verification result candidate_id"),
            (self.execution_id, "verification result execution_id"),
            (self.operation_id, "verification result operation_id"),
        ):
            _uuid(value, label)
        _text(self.check_key, "verification check_key")
        _positive_int(self.check_version, "verification check_version")
        _hash(self.check_hash, "verification check_hash")
        _text(self.environment_ref, "verification environment_ref")
        _hash(self.environment_hash, "verification environment_hash")
        _oid(self.actual_commit, "verification actual_commit")
        _oid(self.actual_tree, "verification actual_tree")
        _positive_int(self.attempt, "verification attempt")
        if not isinstance(self.status, VerificationStatus):
            raise ValidationError("verification result status is unsupported")
        _optional_int(self.exit_code, "verification exit_code")
        _aware_datetime(self.started_at, "verification started_at")
        _aware_datetime(self.finished_at, "verification finished_at")
        if not isinstance(self.artifact_ids, tuple):
            raise ValidationError("verification artifact_ids must be a tuple")
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValidationError("verification artifact_ids must not contain duplicates")
        if self.failure_code is not None:
            _text(self.failure_code, "verification failure_code")
        if self.status is VerificationStatus.PASSED and self.exit_code not in (0,):
            raise ValidationError("passed verification requires a successful exit code")

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "schema_version": self.schema_version,
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "candidate_id": str(self.candidate_id),
            "execution_id": str(self.execution_id),
            "check_key": self.check_key,
            "check_version": self.check_version,
            "check_hash": self.check_hash,
            "environment_ref": self.environment_ref,
            "environment_hash": self.environment_hash,
            "actual_commit": self.actual_commit,
            "actual_tree": self.actual_tree,
            "operation_id": str(self.operation_id),
            "attempt": self.attempt,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "started_at": None if self.started_at is None else self.started_at.isoformat(),
            "finished_at": None if self.finished_at is None else self.finished_at.isoformat(),
            "artifact_ids": [str(item) for item in self.artifact_ids],
            "failure_code": self.failure_code,
        }


_EVALUATION_FIELDS = frozenset(
    {"criterion_key", "observed_status", "verification_result_ids", "eligible_manual", "failure_codes"}
)


@dataclass(frozen=True, slots=True)
class CriterionEvaluationV1:
    criterion_key: str
    observed_status: CriterionObservedStatus
    verification_result_ids: tuple[UUID, ...]
    eligible_manual: bool
    failure_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.criterion_key, "criterion evaluation key")
        if not isinstance(self.observed_status, CriterionObservedStatus):
            raise ValidationError("criterion observed_status is unsupported")
        if not isinstance(self.eligible_manual, bool):
            raise ValidationError("criterion eligible_manual must be a boolean")
        if len(set(self.verification_result_ids)) != len(self.verification_result_ids):
            raise ValidationError("criterion verification_result_ids must be unique")
        for value in self.verification_result_ids:
            _uuid(value, "criterion verification_result_id")
        for value in self.failure_codes:
            _text(value, "criterion failure code")

    @classmethod
    def from_mapping(cls, value: object) -> CriterionEvaluationV1:
        mapping = _mapping(value, "criterion evaluation", _EVALUATION_FIELDS)
        raw_status = mapping["observed_status"]
        try:
            status = CriterionObservedStatus(raw_status)
        except (TypeError, ValueError) as exc:
            raise ValidationError("criterion observed_status is unsupported") from exc
        eligible_manual = mapping["eligible_manual"]
        if not isinstance(eligible_manual, bool):
            raise ValidationError("criterion eligible_manual must be a boolean")
        return cls(
            criterion_key=_text(mapping["criterion_key"], "criterion evaluation key"),
            observed_status=status,
            verification_result_ids=_unique_uuids(
                mapping["verification_result_ids"], "criterion verification_result_ids"
            ),
            eligible_manual=eligible_manual,
            failure_codes=tuple(
                _text(item, "criterion failure code")
                for item in _sequence(mapping["failure_codes"], "criterion failure_codes")
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "criterion_key": self.criterion_key,
            "observed_status": self.observed_status.value,
            "verification_result_ids": [str(item) for item in self.verification_result_ids],
            "eligible_manual": self.eligible_manual,
            "failure_codes": list(self.failure_codes),
        }


_DECISION_FIELDS = frozenset({"criterion_key", "decision", "reason"})


@dataclass(frozen=True, slots=True)
class CriterionDecisionV1:
    criterion_key: str
    decision: CriterionDecisionType
    reason: str | None

    def __post_init__(self) -> None:
        _text(self.criterion_key, "criterion decision key")
        if not isinstance(self.decision, CriterionDecisionType):
            try:
                object.__setattr__(self, "decision", CriterionDecisionType(self.decision))
            except (TypeError, ValueError) as exc:
                raise ValidationError("criterion decision is unsupported") from exc
        if self.decision is CriterionDecisionType.EXCEPTION_ACCEPTED and not (
            self.reason or ""
        ).strip():
            raise ValidationError("exception acceptance requires a reason")
        if self.reason is not None:
            _text(self.reason, "criterion decision reason")

    @classmethod
    def from_mapping(cls, value: object) -> CriterionDecisionV1:
        mapping = _mapping(value, "criterion decision", _DECISION_FIELDS)
        return cls(
            criterion_key=_text(mapping["criterion_key"], "criterion decision key"),
            decision=mapping["decision"],  # type: ignore[arg-type]
            reason=(None if mapping["reason"] is None else _text(mapping["reason"], "criterion decision reason")),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "criterion_key": self.criterion_key,
            "decision": self.decision.value,
            "reason": self.reason,
        }


_ACCEPTANCE_FIELDS = frozenset(
    {
        "id",
        "workspace_id",
        "root_task_id",
        "candidate_id",
        "candidate_hash",
        "plan_content_hash",
        "acceptance_basis_hash",
        "result_version",
        "criteria",
        "actor_type",
        "actor_id",
        "command_id",
        "created_at",
    }
)


@dataclass(frozen=True, slots=True)
class TaskAcceptanceDecisionV1:
    id: UUID
    workspace_id: UUID
    root_task_id: UUID
    candidate_id: UUID
    candidate_hash: str
    plan_content_hash: str
    acceptance_basis_hash: str
    result_version: int
    criteria: tuple[CriterionDecisionV1, ...]
    actor_type: str
    actor_id: str
    command_id: UUID
    created_at: datetime

    @classmethod
    def from_mapping(cls, value: object) -> TaskAcceptanceDecisionV1:
        mapping = _mapping(value, "task acceptance decision", _ACCEPTANCE_FIELDS)
        criteria = tuple(
            CriterionDecisionV1.from_mapping(item)
            for item in _sequence(mapping["criteria"], "acceptance criteria")
        )
        if not criteria:
            raise ValidationError("acceptance criteria must not be empty")
        if len({item.criterion_key for item in criteria}) != len(criteria):
            raise ValidationError("acceptance criteria must not contain duplicates")
        return cls(
            id=_uuid(mapping["id"], "acceptance id"),
            workspace_id=_uuid(mapping["workspace_id"], "acceptance workspace_id"),
            root_task_id=_uuid(mapping["root_task_id"], "acceptance root_task_id"),
            candidate_id=_uuid(mapping["candidate_id"], "acceptance candidate_id"),
            candidate_hash=_hash(mapping["candidate_hash"], "acceptance candidate_hash"),
            plan_content_hash=_hash(mapping["plan_content_hash"], "acceptance plan_content_hash"),
            acceptance_basis_hash=_hash(
                mapping["acceptance_basis_hash"], "acceptance basis hash"
            ),
            result_version=_positive_int(mapping["result_version"], "acceptance result_version"),
            criteria=criteria,
            actor_type=_text(mapping["actor_type"], "acceptance actor_type"),
            actor_id=_text(mapping["actor_id"], "acceptance actor_id"),
            command_id=_uuid(mapping["command_id"], "acceptance command_id"),
            created_at=_aware_datetime(mapping["created_at"], "acceptance created_at"),  # type: ignore[arg-type]
        )

    def __post_init__(self) -> None:
        for value, label in (
            (self.id, "acceptance id"),
            (self.workspace_id, "acceptance workspace_id"),
            (self.root_task_id, "acceptance root_task_id"),
            (self.candidate_id, "acceptance candidate_id"),
            (self.command_id, "acceptance command_id"),
        ):
            _uuid(value, label)
        _hash(self.candidate_hash, "acceptance candidate_hash")
        _hash(self.plan_content_hash, "acceptance plan_content_hash")
        _hash(self.acceptance_basis_hash, "acceptance basis hash")
        _positive_int(self.result_version, "acceptance result_version")
        _text(self.actor_type, "acceptance actor_type")
        _text(self.actor_id, "acceptance actor_id")
        _aware_datetime(self.created_at, "acceptance created_at")
        if not self.criteria or len({item.criterion_key for item in self.criteria}) != len(
            self.criteria
        ):
            raise ValidationError("acceptance criteria must be unique and non-empty")

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "candidate_id": str(self.candidate_id),
            "candidate_hash": self.candidate_hash,
            "plan_content_hash": self.plan_content_hash,
            "acceptance_basis_hash": self.acceptance_basis_hash,
            "result_version": self.result_version,
            "criteria": [item.to_mapping() for item in self.criteria],
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "command_id": str(self.command_id),
            "created_at": self.created_at.isoformat(),
        }


def evaluate_criteria(
    criteria: Sequence[CriterionSpecV1],
    results: Sequence[VerificationResultV1],
    decisions: Sequence[CriterionDecisionV1] = (),
) -> tuple[CriterionEvaluationV1, ...]:
    result_by_check: dict[str, VerificationResultV1] = {}
    for result in results:
        current = result_by_check.get(result.check_key)
        if current is None or result.attempt > current.attempt:
            result_by_check[result.check_key] = result

    criterion_by_key = {criterion.key: criterion for criterion in criteria}
    if len(criterion_by_key) != len(criteria):
        raise ValidationError("criteria must not contain duplicates")
    decision_by_key: dict[str, CriterionDecisionV1] = {}
    for decision in decisions:
        if decision.criterion_key in decision_by_key:
            raise ValidationError("criterion decisions must not contain duplicates")
        criterion = criterion_by_key.get(decision.criterion_key)
        if criterion is None:
            raise ValidationError("criterion decision references an unknown criterion")
        decision_by_key[decision.criterion_key] = decision

    evaluations: list[CriterionEvaluationV1] = []
    for criterion in criteria:
        matching = tuple(
            result_by_check[key]
            for key in criterion.check_keys
            if key in result_by_check
        )
        failure_codes = tuple(
            result.failure_code or result.status.value
            for result in matching
            if result.status is not VerificationStatus.PASSED
        )
        if criterion.mode is CriterionMode.MANUAL or not matching or any(
            result.status is VerificationStatus.NOT_RUN for result in matching
        ):
            observed = CriterionObservedStatus.PENDING
        elif failure_codes:
            observed = CriterionObservedStatus.FAILED
        else:
            observed = CriterionObservedStatus.PASSED
        evaluation = CriterionEvaluationV1(
            criterion_key=criterion.key,
            observed_status=observed,
            verification_result_ids=tuple(result.id for result in matching),
            eligible_manual=criterion.mode is CriterionMode.MANUAL,
            failure_codes=failure_codes,
        )
        decision = decision_by_key.get(criterion.key)
        if decision is not None:
            if (
                decision.decision is CriterionDecisionType.MANUAL_PASSED
                and not evaluation.eligible_manual
            ):
                raise ValidationError("manual_passed is only valid for manual criteria")
            if (
                decision.decision is CriterionDecisionType.ACCEPT_PASSED
                and evaluation.observed_status is not CriterionObservedStatus.PASSED
            ):
                raise ValidationError("accept_passed requires an observed passed criterion")
        evaluations.append(evaluation)
    return tuple(evaluations)


__all__ = [
    "CriterionDecisionType",
    "CriterionDecisionV1",
    "CriterionEvaluationV1",
    "CriterionObservedStatus",
    "EnvironmentBindingV1",
    "TaskAcceptanceDecisionV1",
    "VerificationCheckRefV1",
    "VerificationManifestV1",
    "VerificationResultV1",
    "VerificationStatus",
    "evaluate_criteria",
]
