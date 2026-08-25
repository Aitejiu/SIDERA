from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import cast
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.work_context import WorkReference


class ContextPurpose(StrEnum):
    PLANNING_RUN = "planning_run"
    EXECUTION = "execution"


class ContextConsumerType(StrEnum):
    PLANNER = "planner"
    WORKER = "worker"


class ContextPackageStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    CORRUPT = "corrupt"


class ContextAssemblyStatus(StrEnum):
    QUEUED = "queued"
    ASSEMBLING = "assembling"
    READY = "ready"
    BLOCKED = "blocked"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"


class ContextSelectionReason(StrEnum):
    EXPLICIT = "explicit"
    WORK_CONTEXT = "work_context"
    RETRIEVAL = "retrieval"


class ContextTrustLevel(StrEnum):
    WORKSPACE_CONFIRMED = "workspace_confirmed"
    EXTERNAL_REFERENCE = "external_reference"


_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_FACT_KEY_PARTS = ("conversation", "trace", "body", "markdown", "snapshot")


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} is required")
    return value.strip()


def _positive(value: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{field_name} must be positive")
    return value


def _non_negative(value: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{field_name} must be non-negative")
    return value


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must be timezone aware")
    return value


def _hash(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{field_name} must be a lowercase SHA-256 hash")
    return value


def _freeze_json(value: object, field_name: str) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        raise ValidationError(f"{field_name} cannot contain floating point values")
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"{field_name} keys must be strings")
            lowered = key.lower()
            if any(part in lowered for part in _FORBIDDEN_FACT_KEY_PARTS):
                raise ValidationError(f"{field_name} contains forbidden field {key}")
            result[key] = _freeze_json(item, f"{field_name}.{key}")
        return MappingProxyType(result)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(item, field_name) for item in value)
    raise ValidationError(f"{field_name} contains unsupported value")


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            _plain_json(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("context package cannot be canonicalized") from exc


@dataclass(frozen=True, slots=True)
class ContextBudgetV1:
    profile_id: UUID
    profile_version: int
    max_sections: int
    max_chars: int
    max_input_tokens: int
    actual_sections: int = 0
    actual_chars: int = 0
    actual_input_tokens: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, UUID):
            raise ValidationError("budget profile_id must be a UUID")
        _positive(self.profile_version, "budget profile_version")
        for name in ("max_sections", "max_chars", "max_input_tokens"):
            _positive(getattr(self, name), f"budget {name}")
        for name in ("actual_sections", "actual_chars", "actual_input_tokens"):
            value = getattr(self, name)
            _non_negative(value, f"budget {name}")
        if self.actual_sections > self.max_sections:
            raise ValidationError("actual_sections exceeds max_sections")
        if self.actual_chars > self.max_chars:
            raise ValidationError("actual_chars exceeds max_chars")
        if self.actual_input_tokens > self.max_input_tokens:
            raise ValidationError("actual_input_tokens exceeds max_input_tokens")

    def to_mapping(self) -> dict[str, object]:
        return {
            "profile_id": str(self.profile_id),
            "profile_version": self.profile_version,
            "max_sections": self.max_sections,
            "max_chars": self.max_chars,
            "max_input_tokens": self.max_input_tokens,
            "actual_sections": self.actual_sections,
            "actual_chars": self.actual_chars,
            "actual_input_tokens": self.actual_input_tokens,
        }


@dataclass(frozen=True, slots=True)
class ContextPackageKnowledgeRef:
    page_id: UUID
    revision_id: UUID
    revision_hash: str
    section_key: str
    section_hash: str
    source_ids: tuple[UUID, ...]
    selection_reason: ContextSelectionReason
    trust_level: ContextTrustLevel
    ordinal: int

    def __post_init__(self) -> None:
        if not isinstance(self.page_id, UUID) or not isinstance(self.revision_id, UUID):
            raise ValidationError("package page/revision IDs must be UUIDs")
        object.__setattr__(self, "revision_hash", _hash(self.revision_hash, "revision_hash"))
        object.__setattr__(self, "section_hash", _hash(self.section_hash, "section_hash"))
        object.__setattr__(self, "section_key", _text(self.section_key, "section_key"))
        if any(not isinstance(item, UUID) for item in self.source_ids):
            raise ValidationError("package source IDs must be UUIDs")
        object.__setattr__(self, "source_ids", tuple(sorted(set(self.source_ids), key=str)))
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool) or self.ordinal < 0:
            raise ValidationError("package knowledge ref ordinal must be non-negative")
        try:
            object.__setattr__(self, "selection_reason", ContextSelectionReason(self.selection_reason))
            object.__setattr__(self, "trust_level", ContextTrustLevel(self.trust_level))
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid package knowledge ref enum") from exc

    def to_mapping(self) -> dict[str, object]:
        return {
            "page_id": str(self.page_id),
            "revision_id": str(self.revision_id),
            "revision_hash": self.revision_hash,
            "section_key": self.section_key,
            "section_hash": self.section_hash,
            "source_ids": [str(item) for item in self.source_ids],
            "selection_reason": self.selection_reason.value,
            "trust_level": self.trust_level.value,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True, slots=True)
class ContextPackageManifestV1:
    package_id: UUID
    workspace_id: UUID
    purpose: ContextPurpose
    consumer_type: ContextConsumerType
    consumer_id: UUID
    worker_item_id: UUID | None
    work_context_revision_id: UUID
    context_version: int | None
    control_plane_fact_snapshot: Mapping[str, object]
    knowledge_refs: tuple[ContextPackageKnowledgeRef, ...]
    artifact_refs: tuple[WorkReference, ...]
    evidence_refs: tuple[WorkReference, ...]
    budget: ContextBudgetV1
    expires_at: datetime
    idempotency_key: str
    package_hash: str = field(init=False)
    status: ContextPackageStatus
    created_at: datetime
    materialized_at: datetime | None = None
    revoked_at: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        package_id: UUID,
        workspace_id: UUID,
        purpose: ContextPurpose,
        consumer_type: ContextConsumerType,
        consumer_id: UUID,
        worker_item_id: UUID | None,
        work_context_revision_id: UUID,
        context_version: int | None,
        control_plane_fact_snapshot: Mapping[str, object],
        knowledge_refs: Sequence[ContextPackageKnowledgeRef],
        artifact_refs: Sequence[WorkReference],
        evidence_refs: Sequence[WorkReference],
        budget: ContextBudgetV1,
        expires_at: datetime,
        idempotency_key: str,
        created_at: datetime,
    ) -> ContextPackageManifestV1:
        try:
            normalized_purpose = ContextPurpose(purpose)
            normalized_consumer_type = ContextConsumerType(consumer_type)
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid context package enum") from exc
        normalized_facts = cast(
            Mapping[str, object],
            _freeze_json(control_plane_fact_snapshot, "control_plane_fact_snapshot"),
        )
        normalized_knowledge_refs = _sorted_knowledge_refs(tuple(knowledge_refs))
        normalized_artifact_refs = tuple(artifact_refs)
        normalized_evidence_refs = tuple(evidence_refs)
        if any(
            not isinstance(item, WorkReference)
            for item in (*normalized_artifact_refs, *normalized_evidence_refs)
        ):
            raise ValidationError("package artifact/evidence refs must be typed")
        if not isinstance(budget, ContextBudgetV1):
            raise ValidationError("package budget must be typed")
        _aware(expires_at, "package expires_at")
        _aware(created_at, "package created_at")
        if expires_at <= created_at:
            raise ValidationError("package expires_at must be after created_at")
        candidate = cls(
            package_id=package_id,
            workspace_id=workspace_id,
            purpose=normalized_purpose,
            consumer_type=normalized_consumer_type,
            consumer_id=consumer_id,
            worker_item_id=worker_item_id,
            work_context_revision_id=work_context_revision_id,
            context_version=context_version,
            control_plane_fact_snapshot=normalized_facts,
            knowledge_refs=normalized_knowledge_refs,
            artifact_refs=normalized_artifact_refs,
            evidence_refs=normalized_evidence_refs,
            budget=budget,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
            status=ContextPackageStatus.ACTIVE,
            created_at=created_at,
        )
        return candidate

    def __post_init__(self) -> None:
        for name in ("package_id", "workspace_id", "consumer_id", "work_context_revision_id"):
            if not isinstance(getattr(self, name), UUID):
                raise ValidationError(f"package {name} must be a UUID")
        if self.worker_item_id is not None and not isinstance(self.worker_item_id, UUID):
            raise ValidationError("worker_item_id must be a UUID")
        try:
            object.__setattr__(self, "purpose", ContextPurpose(self.purpose))
            object.__setattr__(self, "consumer_type", ContextConsumerType(self.consumer_type))
            object.__setattr__(self, "status", ContextPackageStatus(self.status))
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid context package enum") from exc
        if self.purpose is ContextPurpose.PLANNING_RUN:
            if self.consumer_type is not ContextConsumerType.PLANNER:
                raise ValidationError("planning package must use planner consumer")
            if self.worker_item_id is not None:
                raise ValidationError("planning package cannot have worker_item_id")
            if self.context_version is not None:
                raise ValidationError("planning package cannot have context_version")
        else:
            if self.consumer_type is not ContextConsumerType.WORKER:
                raise ValidationError("execution package must use worker consumer")
            if self.worker_item_id is None:
                raise ValidationError("execution package requires worker_item_id")
            _positive(self.context_version or 0, "execution context_version")
        if not isinstance(self.control_plane_fact_snapshot, Mapping):
            raise ValidationError("control_plane_fact_snapshot must be a mapping")
        object.__setattr__(
            self,
            "control_plane_fact_snapshot",
            _freeze_json(self.control_plane_fact_snapshot, "control_plane_fact_snapshot"),
        )
        object.__setattr__(self, "knowledge_refs", _sorted_knowledge_refs(self.knowledge_refs))
        if any(not isinstance(item, WorkReference) for item in (*self.artifact_refs, *self.evidence_refs)):
            raise ValidationError("package artifact/evidence refs must be typed")
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if not isinstance(self.budget, ContextBudgetV1):
            raise ValidationError("package budget must be typed")
        _aware(self.expires_at, "package expires_at")
        if self.expires_at <= self.created_at:
            raise ValidationError("package expires_at must be after created_at")
        object.__setattr__(self, "idempotency_key", _text(self.idempotency_key, "idempotency_key"))
        _aware(self.created_at, "package created_at")
        if self.materialized_at is not None:
            _aware(self.materialized_at, "package materialized_at")
        if self.revoked_at is not None:
            _aware(self.revoked_at, "package revoked_at")
        object.__setattr__(self, "package_hash", self.compute_package_hash())

    def hash_document(self) -> dict[str, object]:
        return _package_hash_document(
            purpose=self.purpose,
            consumer_type=self.consumer_type,
            consumer_id=self.consumer_id,
            worker_item_id=self.worker_item_id,
            work_context_revision_id=self.work_context_revision_id,
            context_version=self.context_version,
            control_plane_fact_snapshot=self.control_plane_fact_snapshot,
            knowledge_refs=self.knowledge_refs,
            artifact_refs=self.artifact_refs,
            evidence_refs=self.evidence_refs,
            budget=self.budget,
            expires_at=self.expires_at,
        )

    def compute_package_hash(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.hash_document())).hexdigest()


@dataclass(frozen=True, slots=True)
class ContextAcknowledgement:
    package_id: UUID
    package_hash: str
    consumer_type: ContextConsumerType
    consumer_id: UUID
    context_version: int | None
    adapter_module_id: UUID
    adapter_config_version: int
    acknowledged_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.package_id, UUID) or not isinstance(self.consumer_id, UUID):
            raise ValidationError("ack package/consumer IDs must be UUIDs")
        if not isinstance(self.adapter_module_id, UUID):
            raise ValidationError("ack adapter_module_id must be a UUID")
        _hash(self.package_hash, "package_hash")
        try:
            object.__setattr__(self, "consumer_type", ContextConsumerType(self.consumer_type))
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid ack consumer type") from exc
        if self.consumer_type is ContextConsumerType.PLANNER and self.context_version is not None:
            raise ValidationError("planner acknowledgement cannot have context_version")
        if self.consumer_type is ContextConsumerType.WORKER:
            _positive(self.context_version or 0, "worker acknowledgement context_version")
        _positive(self.adapter_config_version, "adapter_config_version")
        _aware(self.acknowledged_at, "acknowledged_at")


def _sorted_knowledge_refs(
    values: Sequence[ContextPackageKnowledgeRef],
) -> tuple[ContextPackageKnowledgeRef, ...]:
    if any(not isinstance(item, ContextPackageKnowledgeRef) for item in values):
        raise ValidationError("package knowledge refs must be typed")
    return tuple(sorted(values, key=lambda item: (item.ordinal, item.page_id, item.revision_id, item.section_key)))


def _package_hash_document(
    *,
    purpose: ContextPurpose,
    consumer_type: ContextConsumerType,
    consumer_id: UUID,
    worker_item_id: UUID | None,
    work_context_revision_id: UUID,
    context_version: int | None,
    control_plane_fact_snapshot: Mapping[str, object],
    knowledge_refs: Sequence[ContextPackageKnowledgeRef],
    artifact_refs: Sequence[WorkReference],
    evidence_refs: Sequence[WorkReference],
    budget: ContextBudgetV1,
    expires_at: datetime,
) -> dict[str, object]:
    return {
        "purpose": purpose.value,
        "consumer_type": consumer_type.value,
        "consumer_id": str(consumer_id),
        "worker_item_id": str(worker_item_id) if worker_item_id else None,
        "work_context_revision_id": str(work_context_revision_id),
        "context_version": context_version,
        "control_plane_fact_snapshot": _plain_json(control_plane_fact_snapshot),
        "knowledge_refs": [item.to_mapping() for item in knowledge_refs],
        "artifact_refs": [item.to_mapping() for item in artifact_refs],
        "evidence_refs": [item.to_mapping() for item in evidence_refs],
        "budget": budget.to_mapping(),
        "expires_at": expires_at.isoformat(),
    }
