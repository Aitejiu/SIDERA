from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from math import ceil
from types import MappingProxyType
from typing import cast
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
    InputSelectionSourceV1,
)
from sidera_control_plane.domain.result_inputs import (
    ResultInputArtifact,
    ResultInputEvidence,
)
from sidera_control_plane.domain.work_context import (
    AcceptedDecision,
    ClarificationDecision,
    ContextTextItem,
    UnresolvedQuestion,
    WorkReference,
)


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


class ContextRevocationScope(StrEnum):
    NEW_STARTS = "new_starts"
    ALL_MATERIALIZATION = "all_materialization"


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


class ContextMaterializationOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ContextMaterializationMode(StrEnum):
    CONSUMER_START = "consumer_start"
    OWNER_AUDIT = "owner_audit"


def context_package_reference(package_id: UUID, package_hash: str) -> str:
    """Build the stable opaque reference carried by an ExecutionSpec."""
    if not isinstance(package_id, UUID):
        raise ValidationError("context package reference ID must be a UUID")
    return f"context-package://{package_id}/{_hash(package_hash, 'package_hash')}"


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
class ContextMetadata:
    package_id: UUID
    package_hash: str
    purpose: ContextPurpose
    consumer_type: ContextConsumerType
    consumer_id: UUID
    context_version: int | None
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.package_id, UUID) or not isinstance(self.consumer_id, UUID):
            raise ValidationError("materialization package/consumer IDs must be UUIDs")
        object.__setattr__(self, "package_hash", _hash(self.package_hash, "package_hash"))
        try:
            object.__setattr__(self, "purpose", ContextPurpose(self.purpose))
            object.__setattr__(self, "consumer_type", ContextConsumerType(self.consumer_type))
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid materialization metadata enum") from exc
        if self.consumer_type is ContextConsumerType.PLANNER and self.context_version is not None:
            raise ValidationError("planner materialization cannot have context_version")
        if self.consumer_type is ContextConsumerType.WORKER:
            _positive(self.context_version or 0, "worker materialization context_version")
        _aware(self.expires_at, "materialization expires_at")

    def to_mapping(self) -> dict[str, object]:
        return {
            "package_id": str(self.package_id),
            "package_hash": self.package_hash,
            "purpose": self.purpose.value,
            "consumer_type": self.consumer_type.value,
            "consumer_id": str(self.consumer_id),
            "context_version": self.context_version,
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
        }


@dataclass(frozen=True, slots=True)
class SystemConstraintSection:
    objective: str
    constraints: tuple[ContextTextItem, ...]
    acceptance_criteria: tuple[ContextTextItem, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "objective", _text(self.objective, "system objective"))
        if any(not isinstance(item, ContextTextItem) for item in self.constraints):
            raise ValidationError("system constraints must be typed")
        if any(not isinstance(item, ContextTextItem) for item in self.acceptance_criteria):
            raise ValidationError("system acceptance criteria must be typed")
        object.__setattr__(self, "constraints", tuple(self.constraints))
        object.__setattr__(self, "acceptance_criteria", tuple(self.acceptance_criteria))

    def to_mapping(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "constraints": [item.to_mapping() for item in self.constraints],
            "acceptance_criteria": [item.to_mapping() for item in self.acceptance_criteria],
        }


@dataclass(frozen=True, slots=True)
class WorkContextSection:
    revision_id: UUID
    revision_number: int
    content_hash: str
    snapshot_schema_version: int
    requested_outputs: tuple[ContextTextItem, ...]
    clarification_decisions: tuple[ClarificationDecision, ...]
    accepted_decisions: tuple[AcceptedDecision, ...]
    unresolved_questions: tuple[UnresolvedQuestion, ...]
    checkpoint_refs: tuple[WorkReference, ...]
    missing_fact_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.revision_id, UUID):
            raise ValidationError("work context materialization revision_id must be a UUID")
        _positive(self.revision_number, "work context materialization revision_number")
        object.__setattr__(self, "content_hash", _hash(self.content_hash, "work context content_hash"))
        _positive(self.snapshot_schema_version, "work context snapshot_schema_version")
        typed_fields = {
            "requested_outputs": ContextTextItem,
            "clarification_decisions": ClarificationDecision,
            "accepted_decisions": AcceptedDecision,
            "unresolved_questions": UnresolvedQuestion,
            "checkpoint_refs": WorkReference,
        }
        for field_name, item_type in typed_fields.items():
            values = getattr(self, field_name)
            if any(not isinstance(item, item_type) for item in values):
                raise ValidationError(f"work context {field_name} must be typed")
            object.__setattr__(self, field_name, tuple(values))
        object.__setattr__(
            self,
            "missing_fact_codes",
            tuple(sorted(_text(item, "missing fact code") for item in self.missing_fact_codes)),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "revision_id": str(self.revision_id),
            "revision_number": self.revision_number,
            "content_hash": self.content_hash,
            "snapshot_schema_version": self.snapshot_schema_version,
            "requested_outputs": [item.to_mapping() for item in self.requested_outputs],
            "clarification_decisions": [
                item.to_mapping() for item in self.clarification_decisions
            ],
            "accepted_decisions": [item.to_mapping() for item in self.accepted_decisions],
            "unresolved_questions": [item.to_mapping() for item in self.unresolved_questions],
            "checkpoint_refs": [item.to_mapping() for item in self.checkpoint_refs],
            "missing_fact_codes": list(self.missing_fact_codes),
        }


@dataclass(frozen=True, slots=True)
class ControlPlaneFactSection:
    facts: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "facts",
            cast(Mapping[str, object], _freeze_json(self.facts, "materialization facts")),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"facts": _plain_json(self.facts)}


@dataclass(frozen=True, slots=True)
class MaterializedContextSource:
    source_id: UUID
    workspace_id: UUID
    source_type: str
    label: str
    uri: str | None
    workspace_relative_path: str | None
    git_revision: str | None
    digest: str | None
    metadata: Mapping[str, object]
    availability: str
    safe_reason_code: str | None
    last_checked_at: datetime | None
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, UUID) or not isinstance(self.workspace_id, UUID):
            raise ValidationError("materialized source IDs must be UUIDs")
        object.__setattr__(self, "source_type", _text(self.source_type, "source_type"))
        object.__setattr__(self, "label", _text(self.label, "source label"))
        object.__setattr__(
            self,
            "metadata",
            cast(Mapping[str, object], _freeze_json(self.metadata, "source metadata")),
        )
        object.__setattr__(self, "availability", _text(self.availability, "source availability"))
        _positive(self.version, "source version")
        if self.last_checked_at is not None:
            _aware(self.last_checked_at, "source last_checked_at")

    def to_mapping(self) -> dict[str, object]:
        return {
            "source_id": str(self.source_id),
            "workspace_id": str(self.workspace_id),
            "source_type": self.source_type,
            "label": self.label,
            "uri": self.uri,
            "workspace_relative_path": self.workspace_relative_path,
            "git_revision": self.git_revision,
            "digest": self.digest,
            "metadata": _plain_json(self.metadata),
            "availability": self.availability,
            "safe_reason_code": self.safe_reason_code,
            "last_checked_at": (
                self.last_checked_at.astimezone(UTC).isoformat()
                if self.last_checked_at is not None
                else None
            ),
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class ProjectKnowledgeSection:
    ordinal: int
    page_id: UUID
    revision_id: UUID
    revision_hash: str
    section_key: str
    section_hash: str
    body_markdown: str
    sources: tuple[MaterializedContextSource, ...]
    trust_level: ContextTrustLevel
    section_byte_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.page_id, UUID) or not isinstance(self.revision_id, UUID):
            raise ValidationError("project knowledge IDs must be UUIDs")
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool) or self.ordinal < 0:
            raise ValidationError("project knowledge ordinal must be non-negative")
        object.__setattr__(self, "revision_hash", _hash(self.revision_hash, "knowledge revision_hash"))
        object.__setattr__(self, "section_hash", _hash(self.section_hash, "knowledge section_hash"))
        object.__setattr__(self, "section_key", _text(self.section_key, "knowledge section_key"))
        if not isinstance(self.body_markdown, str):
            raise ValidationError("knowledge body_markdown must be text")
        if any(not isinstance(item, MaterializedContextSource) for item in self.sources):
            raise ValidationError("knowledge sources must be typed")
        object.__setattr__(self, "sources", tuple(self.sources))
        try:
            object.__setattr__(self, "trust_level", ContextTrustLevel(self.trust_level))
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid knowledge trust level") from exc
        _non_negative(self.section_byte_count, "knowledge section_byte_count")
        if len(self.body_markdown.encode("utf-8")) != self.section_byte_count:
            raise ValidationError("knowledge body byte count mismatch")

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "page_id": str(self.page_id),
            "revision_id": str(self.revision_id),
            "revision_hash": self.revision_hash,
            "section_key": self.section_key,
            "section_hash": self.section_hash,
            "body_markdown": self.body_markdown,
            "sources": [item.to_mapping() for item in self.sources],
            "trust": self.trust_level.value,
            "section_byte_count": self.section_byte_count,
        }


@dataclass(frozen=True, slots=True)
class TypedContextRef:
    category: str
    ref_id: UUID
    ref_type: str
    ordinal: int

    def __post_init__(self) -> None:
        if self.category not in {"artifact", "evidence"}:
            raise ValidationError("context ref category must be artifact or evidence")
        if not isinstance(self.ref_id, UUID):
            raise ValidationError("context ref ID must be a UUID")
        object.__setattr__(self, "ref_type", _text(self.ref_type, "context ref type"))
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool) or self.ordinal < 0:
            raise ValidationError("context ref ordinal must be non-negative")

    def to_mapping(self) -> dict[str, object]:
        return {
            "category": self.category,
            "ref_id": str(self.ref_id),
            "ref_type": self.ref_type,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True, slots=True)
class ContextMaterializationV1:
    metadata: ContextMetadata
    system_constraints: SystemConstraintSection
    work_context: WorkContextSection
    control_plane_facts: ControlPlaneFactSection
    project_knowledge: tuple[ProjectKnowledgeSection, ...]
    artifact_and_evidence_refs: tuple[TypedContextRef, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        _positive(self.schema_version, "materialization schema_version")
        if self.schema_version != 1:
            raise ValidationError("unsupported context materialization schema_version")
        for field_name, item_type in (
            ("metadata", ContextMetadata),
            ("system_constraints", SystemConstraintSection),
            ("work_context", WorkContextSection),
            ("control_plane_facts", ControlPlaneFactSection),
        ):
            if not isinstance(getattr(self, field_name), item_type):
                raise ValidationError(f"materialization {field_name} must be typed")
        if any(not isinstance(item, ProjectKnowledgeSection) for item in self.project_knowledge):
            raise ValidationError("materialization project knowledge must be typed")
        if len({item.ordinal for item in self.project_knowledge}) != len(self.project_knowledge):
            raise ValidationError("materialization project knowledge ordinals must be unique")
        object.__setattr__(
            self,
            "project_knowledge",
            tuple(sorted(self.project_knowledge, key=lambda item: item.ordinal)),
        )
        if any(not isinstance(item, TypedContextRef) for item in self.artifact_and_evidence_refs):
            raise ValidationError("materialization artifact/evidence refs must be typed")
        object.__setattr__(
            self,
            "artifact_and_evidence_refs",
            tuple(self.artifact_and_evidence_refs),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "metadata": self.metadata.to_mapping(),
            "system_constraints": self.system_constraints.to_mapping(),
            "work_context": self.work_context.to_mapping(),
            "control_plane_facts": self.control_plane_facts.to_mapping(),
            "project_knowledge": [item.to_mapping() for item in self.project_knowledge],
            "artifact_and_evidence_refs": [
                item.to_mapping() for item in self.artifact_and_evidence_refs
            ],
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_mapping())

    @property
    def total_byte_count(self) -> int:
        return len(self.canonical_bytes())

    @property
    def input_token_count(self) -> int:
        return ceil(self.total_byte_count / 4)

    @property
    def knowledge_byte_count(self) -> int:
        return sum(item.section_byte_count for item in self.project_knowledge)

    @property
    def knowledge_input_token_count(self) -> int:
        return sum(ceil(item.section_byte_count / 4) for item in self.project_knowledge)


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
class ResultInputBudgetV1:
    max_artifact_bytes: int = 32_768
    max_total_artifact_bytes: int = 131_072
    max_artifact_count: int = 128
    max_payload_bytes: int = 262_144
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValidationError("unsupported result input budget schema_version")
        for name in (
            "max_artifact_bytes",
            "max_total_artifact_bytes",
            "max_artifact_count",
            "max_payload_bytes",
        ):
            _positive(getattr(self, name), f"result input budget {name}")
        if self.max_total_artifact_bytes < self.max_artifact_bytes:
            raise ValidationError(
                "result input budget total bytes cannot be below artifact bytes"
            )
        if self.max_payload_bytes < self.max_total_artifact_bytes:
            raise ValidationError(
                "result input budget payload bytes cannot be below total bytes"
            )

    @classmethod
    def from_mapping(cls, value: object) -> ResultInputBudgetV1:
        mapping = _result_input_mapping(
            value,
            "result input budget",
            {
                "schema_version",
                "max_artifact_bytes",
                "max_total_artifact_bytes",
                "max_artifact_count",
                "max_payload_bytes",
            },
        )
        return cls(
            schema_version=_result_positive_int(
                mapping["schema_version"], "schema_version"
            ),
            max_artifact_bytes=_result_positive_int(
                mapping["max_artifact_bytes"], "max_artifact_bytes"
            ),
            max_total_artifact_bytes=_result_positive_int(
                mapping["max_total_artifact_bytes"], "max_total_artifact_bytes"
            ),
            max_artifact_count=_result_positive_int(
                mapping["max_artifact_count"], "max_artifact_count"
            ),
            max_payload_bytes=_result_positive_int(
                mapping["max_payload_bytes"], "max_payload_bytes"
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "max_artifact_bytes": self.max_artifact_bytes,
            "max_total_artifact_bytes": self.max_total_artifact_bytes,
            "max_artifact_count": self.max_artifact_count,
            "max_payload_bytes": self.max_payload_bytes,
        }


@dataclass(frozen=True, slots=True)
class ResultInputSourceProvenanceV1:
    worker_item_key: str
    worker_item_id: UUID
    subtask_path: tuple[str, ...]
    effective_request_id: UUID
    allowed_lineage_ids: tuple[UUID, ...]
    effective_lineage_id: UUID
    effective_execution_id: UUID
    attempt: int
    inheritance_refs: tuple[str, ...]
    priority: int
    source_basis_hash: str

    @classmethod
    def from_selection_source(
        cls, source: InputSelectionSourceV1
    ) -> ResultInputSourceProvenanceV1:
        if not isinstance(source, InputSelectionSourceV1):
            raise ValidationError("result input source provenance requires a selection source")
        return cls(
            worker_item_key=source.worker_item_key,
            worker_item_id=source.worker_item_id,
            subtask_path=source.subtask_path,
            effective_request_id=source.effective_request_id,
            allowed_lineage_ids=source.allowed_lineage_ids,
            effective_lineage_id=source.effective_lineage_id,
            effective_execution_id=source.effective_execution_id,
            attempt=source.attempt,
            inheritance_refs=source.inheritance_refs,
            priority=source.priority,
            source_basis_hash=source.source_basis_hash,
        )

    @classmethod
    def from_mapping(cls, value: object) -> ResultInputSourceProvenanceV1:
        mapping = _result_input_mapping(
            value,
            "result input source provenance",
            {
                "worker_item_key",
                "worker_item_id",
                "subtask_path",
                "effective_request_id",
                "allowed_lineage_ids",
                "effective_lineage_id",
                "effective_execution_id",
                "attempt",
                "inheritance_refs",
                "priority",
                "source_basis_hash",
            },
        )
        return cls(
            worker_item_key=_result_text(mapping["worker_item_key"], "worker_item_key"),
            worker_item_id=_result_uuid(mapping["worker_item_id"], "worker_item_id"),
            subtask_path=_result_text_array(mapping["subtask_path"], "subtask_path"),
            effective_request_id=_result_uuid(
                mapping["effective_request_id"], "effective_request_id"
            ),
            allowed_lineage_ids=_result_uuid_array(
                mapping["allowed_lineage_ids"], "allowed_lineage_ids"
            ),
            effective_lineage_id=_result_uuid(
                mapping["effective_lineage_id"], "effective_lineage_id"
            ),
            effective_execution_id=_result_uuid(
                mapping["effective_execution_id"], "effective_execution_id"
            ),
            attempt=_result_positive_int(mapping["attempt"], "attempt"),
            inheritance_refs=_result_text_array(
                mapping["inheritance_refs"], "inheritance_refs", allow_empty=True
            ),
            priority=_result_positive_int(mapping["priority"], "priority"),
            source_basis_hash=_result_hash(
                mapping["source_basis_hash"], "source_basis_hash"
            ),
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_item_key", _result_text(self.worker_item_key, "worker_item_key"))
        _result_uuid(self.worker_item_id, "worker_item_id")
        object.__setattr__(self, "subtask_path", _result_text_array(self.subtask_path, "subtask_path"))
        _result_uuid(self.effective_request_id, "effective_request_id")
        object.__setattr__(
            self,
            "allowed_lineage_ids",
            _result_uuid_array(self.allowed_lineage_ids, "allowed_lineage_ids"),
        )
        _result_uuid(self.effective_lineage_id, "effective_lineage_id")
        if self.effective_lineage_id not in self.allowed_lineage_ids:
            raise ValidationError(
                "effective_lineage_id must be present in allowed_lineage_ids"
            )
        _result_uuid(self.effective_execution_id, "effective_execution_id")
        _positive(self.attempt, "result input source attempt")
        object.__setattr__(
            self,
            "inheritance_refs",
            _result_text_array(self.inheritance_refs, "inheritance_refs", allow_empty=True),
        )
        _positive(self.priority, "result input source priority")
        object.__setattr__(
            self,
            "source_basis_hash",
            _result_hash(self.source_basis_hash, "source_basis_hash"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "worker_item_key": self.worker_item_key,
            "worker_item_id": str(self.worker_item_id),
            "subtask_path": list(self.subtask_path),
            "effective_request_id": str(self.effective_request_id),
            "allowed_lineage_ids": [str(item) for item in self.allowed_lineage_ids],
            "effective_lineage_id": str(self.effective_lineage_id),
            "effective_execution_id": str(self.effective_execution_id),
            "attempt": self.attempt,
            "inheritance_refs": list(self.inheritance_refs),
            "priority": self.priority,
            "source_basis_hash": self.source_basis_hash,
        }


@dataclass(frozen=True, slots=True)
class ResultInputGroupManifestV1:
    group_key: str
    rule_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_key", _result_text(self.group_key, "group_key"))
        object.__setattr__(self, "rule_hash", _result_hash(self.rule_hash, "rule_hash"))

    @classmethod
    def from_mapping(cls, value: object) -> ResultInputGroupManifestV1:
        mapping = _result_input_mapping(value, "result input group", {"group_key", "rule_hash"})
        return cls(
            group_key=_result_text(mapping["group_key"], "group_key"),
            rule_hash=_result_hash(mapping["rule_hash"], "rule_hash"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"group_key": self.group_key, "rule_hash": self.rule_hash}


@dataclass(frozen=True, slots=True)
class ResultInputHardInputManifestV1:
    dependency_key: str
    rule_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "dependency_key", _result_text(self.dependency_key, "dependency_key")
        )
        object.__setattr__(self, "rule_hash", _result_hash(self.rule_hash, "rule_hash"))

    @classmethod
    def from_mapping(cls, value: object) -> ResultInputHardInputManifestV1:
        mapping = _result_input_mapping(
            value, "result input hard input", {"dependency_key", "rule_hash"}
        )
        return cls(
            dependency_key=_result_text(mapping["dependency_key"], "dependency_key"),
            rule_hash=_result_hash(mapping["rule_hash"], "rule_hash"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"dependency_key": self.dependency_key, "rule_hash": self.rule_hash}


@dataclass(frozen=True, slots=True)
class ResultInputArtifactManifestV1:
    artifact_id: UUID
    artifact_type: str
    content_hash: str
    size_bytes: int
    media_type: str
    sources: tuple[ResultInputSourceProvenanceV1, ...]
    group_keys: tuple[str, ...]
    hard_refs: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: object) -> ResultInputArtifactManifestV1:
        mapping = _result_input_mapping(
            value,
            "result input artifact manifest",
            {
                "artifact_id",
                "artifact_type",
                "content_hash",
                "size_bytes",
                "media_type",
                "sources",
                "group_keys",
                "hard_refs",
            },
        )
        return cls(
            artifact_id=_result_uuid(mapping["artifact_id"], "artifact_id"),
            artifact_type=_result_text(mapping["artifact_type"], "artifact_type"),
            content_hash=_result_hash(mapping["content_hash"], "content_hash"),
            size_bytes=_result_non_negative_int(mapping["size_bytes"], "size_bytes"),
            media_type=_result_text(mapping["media_type"], "media_type"),
            sources=_result_sources(mapping["sources"]),
            group_keys=_result_text_array(mapping["group_keys"], "group_keys", allow_empty=True),
            hard_refs=_result_text_array(mapping["hard_refs"], "hard_refs", allow_empty=True),
        )

    @classmethod
    def from_artifact(
        cls,
        artifact: ResultInputArtifact,
        *,
        sources: tuple[ResultInputSourceProvenanceV1, ...],
        group_keys: tuple[str, ...],
        hard_refs: tuple[str, ...],
    ) -> ResultInputArtifactManifestV1:
        return cls(
            artifact_id=artifact.artifact_id,
            artifact_type=artifact.artifact_type.value,
            content_hash=artifact.content_hash,
            size_bytes=artifact.size_bytes,
            media_type=artifact.media_type,
            sources=sources,
            group_keys=group_keys,
            hard_refs=hard_refs,
        )

    def __post_init__(self) -> None:
        _result_uuid(self.artifact_id, "artifact_id")
        object.__setattr__(self, "artifact_type", _result_text(self.artifact_type, "artifact_type"))
        object.__setattr__(self, "content_hash", _result_hash(self.content_hash, "content_hash"))
        _result_non_negative_int(self.size_bytes, "size_bytes")
        object.__setattr__(self, "media_type", _result_text(self.media_type, "media_type"))
        object.__setattr__(self, "sources", _result_sources(self.sources))
        object.__setattr__(self, "group_keys", _result_text_array(self.group_keys, "group_keys", allow_empty=True))
        object.__setattr__(self, "hard_refs", _result_text_array(self.hard_refs, "hard_refs", allow_empty=True))
        if not self.sources:
            raise ValidationError("result input artifact must retain source provenance")

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_id": str(self.artifact_id),
            "artifact_type": self.artifact_type,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "sources": [item.to_mapping() for item in self.sources],
            "group_keys": list(self.group_keys),
            "hard_refs": list(self.hard_refs),
        }


@dataclass(frozen=True, slots=True)
class ResultInputEvidenceManifestV1:
    evidence_id: UUID
    acceptance_criterion_ref: str
    artifact_ids: tuple[UUID, ...]
    sources: tuple[ResultInputSourceProvenanceV1, ...]
    group_keys: tuple[str, ...]
    hard_refs: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: object) -> ResultInputEvidenceManifestV1:
        mapping = _result_input_mapping(
            value,
            "result input evidence manifest",
            {
                "evidence_id",
                "acceptance_criterion_ref",
                "artifact_ids",
                "sources",
                "group_keys",
                "hard_refs",
            },
        )
        return cls(
            evidence_id=_result_uuid(mapping["evidence_id"], "evidence_id"),
            acceptance_criterion_ref=_result_text(
                mapping["acceptance_criterion_ref"], "acceptance_criterion_ref"
            ),
            artifact_ids=_result_uuid_array(mapping["artifact_ids"], "artifact_ids"),
            sources=_result_sources(mapping["sources"]),
            group_keys=_result_text_array(mapping["group_keys"], "group_keys", allow_empty=True),
            hard_refs=_result_text_array(mapping["hard_refs"], "hard_refs", allow_empty=True),
        )

    def __post_init__(self) -> None:
        _result_uuid(self.evidence_id, "evidence_id")
        object.__setattr__(
            self,
            "acceptance_criterion_ref",
            _result_text(self.acceptance_criterion_ref, "acceptance_criterion_ref"),
        )
        object.__setattr__(self, "artifact_ids", _result_uuid_array(self.artifact_ids, "artifact_ids"))
        object.__setattr__(self, "sources", _result_sources(self.sources))
        object.__setattr__(self, "group_keys", _result_text_array(self.group_keys, "group_keys", allow_empty=True))
        object.__setattr__(self, "hard_refs", _result_text_array(self.hard_refs, "hard_refs", allow_empty=True))
        if not self.sources:
            raise ValidationError("result input evidence must retain source provenance")

    def to_mapping(self) -> dict[str, object]:
        return {
            "evidence_id": str(self.evidence_id),
            "acceptance_criterion_ref": self.acceptance_criterion_ref,
            "artifact_ids": [str(item) for item in self.artifact_ids],
            "sources": [item.to_mapping() for item in self.sources],
            "group_keys": list(self.group_keys),
            "hard_refs": list(self.hard_refs),
        }


@dataclass(frozen=True, slots=True)
class ResultInputManifestV1:
    input_selection_id: UUID
    input_selection_hash: str
    groups: tuple[ResultInputGroupManifestV1, ...]
    hard_inputs: tuple[ResultInputHardInputManifestV1, ...]
    artifacts: tuple[ResultInputArtifactManifestV1, ...]
    evidence: tuple[ResultInputEvidenceManifestV1, ...]
    budget: ResultInputBudgetV1
    schema_version: int = 1
    delivery_mode: str = "bounded_text_v1"

    @classmethod
    def from_selection(
        cls,
        selection: CollaborationInputSelectionV1,
        *,
        budget: ResultInputBudgetV1 | None = None,
    ) -> ResultInputManifestV1:
        if not isinstance(selection, CollaborationInputSelectionV1):
            raise ValidationError("result input manifest requires a typed selection")

        group_values = tuple(
            ResultInputGroupManifestV1(group_key=item.group_key, rule_hash=item.rule_hash)
            for item in selection.groups
        )
        hard_values = tuple(
            ResultInputHardInputManifestV1(
                dependency_key=item.dependency_key, rule_hash=item.rule_hash
            )
            for item in selection.hard_inputs
        )
        artifact_rows: dict[
            UUID,
            tuple[
                ResultInputArtifact,
                set[str],
                set[str],
                list[ResultInputSourceProvenanceV1],
            ],
        ] = {}
        evidence_rows: dict[
            UUID,
            tuple[
                ResultInputEvidence,
                set[str],
                set[str],
                list[ResultInputSourceProvenanceV1],
            ],
        ] = {}

        def add_source(
            source: InputSelectionSourceV1,
            *,
            group_key: str | None = None,
            hard_ref: str | None = None,
        ) -> None:
            provenance = ResultInputSourceProvenanceV1.from_selection_source(source)
            for artifact in source.artifacts:
                row = artifact_rows.get(artifact.artifact_id)
                if row is None:
                    row = (artifact, set(), set(), [])
                    artifact_rows[artifact.artifact_id] = row
                elif row[0] != artifact:
                    raise ValidationError(
                        "same artifact ID has conflicting result input artifact hash or metadata"
                    )
                if group_key is not None:
                    row[1].add(group_key)
                if hard_ref is not None:
                    row[2].add(hard_ref)
                if provenance not in row[3]:
                    row[3].append(provenance)
            for evidence in source.evidence:
                row = evidence_rows.get(evidence.evidence_id)
                if row is None:
                    row = (evidence, set(), set(), [])
                    evidence_rows[evidence.evidence_id] = row
                elif row[0] != evidence:
                    raise ValidationError(
                        "same evidence ID has conflicting result input evidence metadata"
                    )
                if group_key is not None:
                    row[1].add(group_key)
                if hard_ref is not None:
                    row[2].add(hard_ref)
                if provenance not in row[3]:
                    row[3].append(provenance)

        for group in selection.groups:
            for source in group.sources:
                add_source(source, group_key=group.group_key)
        for hard_input in selection.hard_inputs:
            add_source(hard_input.source, hard_ref=hard_input.dependency_key)

        artifacts = tuple(
            ResultInputArtifactManifestV1.from_artifact(
                row[0],
                sources=tuple(sorted(row[3], key=_result_source_sort_key)),
                group_keys=tuple(sorted(row[1])),
                hard_refs=tuple(sorted(row[2])),
            )
            for row in sorted(artifact_rows.values(), key=lambda item: str(item[0].artifact_id))
        )
        evidence = tuple(
            ResultInputEvidenceManifestV1(
                evidence_id=row[0].evidence_id,
                acceptance_criterion_ref=row[0].acceptance_criterion_ref,
                artifact_ids=row[0].artifact_ids,
                sources=tuple(sorted(row[3], key=_result_source_sort_key)),
                group_keys=tuple(sorted(row[1])),
                hard_refs=tuple(sorted(row[2])),
            )
            for row in sorted(evidence_rows.values(), key=lambda item: str(item[0].evidence_id))
        )
        return cls(
            input_selection_id=selection.id,
            input_selection_hash=selection.selection_hash,
            groups=group_values,
            hard_inputs=hard_values,
            artifacts=artifacts,
            evidence=evidence,
            budget=budget or ResultInputBudgetV1(),
        )

    @classmethod
    def from_mapping(cls, value: object) -> ResultInputManifestV1:
        mapping = _result_input_mapping(
            value,
            "result input manifest",
            {
                "schema_version",
                "input_selection_id",
                "input_selection_hash",
                "delivery_mode",
                "groups",
                "hard_inputs",
                "artifacts",
                "evidence",
                "budget",
            },
        )
        return cls(
            schema_version=_result_positive_int(mapping["schema_version"], "schema_version"),
            input_selection_id=_result_uuid(
                mapping["input_selection_id"], "input_selection_id"
            ),
            input_selection_hash=_result_hash(
                mapping["input_selection_hash"], "input_selection_hash"
            ),
            delivery_mode=_result_text(mapping["delivery_mode"], "delivery_mode"),
            groups=_result_groups(mapping["groups"]),
            hard_inputs=_result_hard_inputs(mapping["hard_inputs"]),
            artifacts=_result_artifacts(mapping["artifacts"]),
            evidence=_result_evidence(mapping["evidence"]),
            budget=ResultInputBudgetV1.from_mapping(mapping["budget"]),
        )

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValidationError("unsupported result input manifest schema_version")
        if self.delivery_mode != "bounded_text_v1":
            raise ValidationError("unsupported result input delivery_mode")
        _result_uuid(self.input_selection_id, "input_selection_id")
        object.__setattr__(
            self,
            "input_selection_hash",
            _result_hash(self.input_selection_hash, "input_selection_hash"),
        )
        object.__setattr__(self, "groups", _result_groups(self.groups))
        object.__setattr__(self, "hard_inputs", _result_hard_inputs(self.hard_inputs))
        object.__setattr__(self, "artifacts", _result_artifacts(self.artifacts))
        object.__setattr__(self, "evidence", _result_evidence(self.evidence))
        if not isinstance(self.budget, ResultInputBudgetV1):
            raise ValidationError("result input manifest budget must be typed")
        artifact_ids = [item.artifact_id for item in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValidationError("result input artifact IDs must be unique")
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValidationError("result input evidence IDs must be unique")
        artifact_id_set = set(artifact_ids)
        if any(
            not set(item.artifact_ids).issubset(artifact_id_set) for item in self.evidence
        ):
            raise ValidationError("result input Evidence references an artifact outside the manifest")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "input_selection_id": str(self.input_selection_id),
            "input_selection_hash": self.input_selection_hash,
            "delivery_mode": self.delivery_mode,
            "groups": [item.to_mapping() for item in self.groups],
            "hard_inputs": [item.to_mapping() for item in self.hard_inputs],
            "artifacts": [item.to_mapping() for item in self.artifacts],
            "evidence": [item.to_mapping() for item in self.evidence],
            "budget": self.budget.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class MaterializedResultInput:
    artifact_id: UUID
    content_hash: str
    size_bytes: int
    media_type: str
    sources: tuple[ResultInputSourceProvenanceV1, ...]
    group_keys: tuple[str, ...]
    hard_refs: tuple[str, ...]
    body_text: str
    candidate_evidence: tuple[ResultInputEvidenceManifestV1, ...] = ()

    def __post_init__(self) -> None:
        _result_uuid(self.artifact_id, "materialized result input artifact_id")
        object.__setattr__(self, "content_hash", _result_hash(self.content_hash, "content_hash"))
        _result_non_negative_int(self.size_bytes, "materialized result input size_bytes")
        object.__setattr__(self, "media_type", _result_text(self.media_type, "media_type"))
        object.__setattr__(self, "sources", _result_sources(self.sources))
        object.__setattr__(self, "group_keys", _result_text_array(self.group_keys, "group_keys", allow_empty=True))
        object.__setattr__(self, "hard_refs", _result_text_array(self.hard_refs, "hard_refs", allow_empty=True))
        if not isinstance(self.body_text, str):
            raise ValidationError("materialized result input body_text must be text")
        object.__setattr__(self, "candidate_evidence", _result_evidence(self.candidate_evidence))

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_id": str(self.artifact_id),
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "sources": [item.to_mapping() for item in self.sources],
            "group_keys": list(self.group_keys),
            "hard_refs": list(self.hard_refs),
            "body_text": self.body_text,
            "candidate_evidence": [item.to_mapping() for item in self.candidate_evidence],
        }


@dataclass(frozen=True, slots=True)
class ContextMaterializationV2:
    metadata: ContextMetadata
    system_constraints: SystemConstraintSection
    work_context: WorkContextSection
    control_plane_facts: ControlPlaneFactSection
    project_knowledge: tuple[ProjectKnowledgeSection, ...]
    artifact_and_evidence_refs: tuple[TypedContextRef, ...]
    result_inputs: tuple[MaterializedResultInput, ...]
    schema_version: int = 2

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValidationError("unsupported context materialization schema_version")
        for field_name, item_type in (
            ("metadata", ContextMetadata),
            ("system_constraints", SystemConstraintSection),
            ("work_context", WorkContextSection),
            ("control_plane_facts", ControlPlaneFactSection),
        ):
            if not isinstance(getattr(self, field_name), item_type):
                raise ValidationError(f"materialization {field_name} must be typed")
        if any(not isinstance(item, ProjectKnowledgeSection) for item in self.project_knowledge):
            raise ValidationError("materialization project knowledge must be typed")
        object.__setattr__(
            self,
            "project_knowledge",
            tuple(sorted(self.project_knowledge, key=lambda item: item.ordinal)),
        )
        if any(not isinstance(item, TypedContextRef) for item in self.artifact_and_evidence_refs):
            raise ValidationError("materialization artifact/evidence refs must be typed")
        object.__setattr__(self, "artifact_and_evidence_refs", tuple(self.artifact_and_evidence_refs))
        if any(not isinstance(item, MaterializedResultInput) for item in self.result_inputs):
            raise ValidationError("materialization result inputs must be typed")
        object.__setattr__(self, "result_inputs", tuple(self.result_inputs))

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "metadata": self.metadata.to_mapping(),
            "system_constraints": self.system_constraints.to_mapping(),
            "work_context": self.work_context.to_mapping(),
            "control_plane_facts": self.control_plane_facts.to_mapping(),
            "project_knowledge": [item.to_mapping() for item in self.project_knowledge],
            "artifact_and_evidence_refs": [item.to_mapping() for item in self.artifact_and_evidence_refs],
            "result_inputs": [item.to_mapping() for item in self.result_inputs],
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_mapping())

    @property
    def total_byte_count(self) -> int:
        return len(self.canonical_bytes())

    @property
    def input_token_count(self) -> int:
        return ceil(self.total_byte_count / 4)

    @property
    def knowledge_byte_count(self) -> int:
        return sum(item.section_byte_count for item in self.project_knowledge)

    @property
    def knowledge_input_token_count(self) -> int:
        return sum(ceil(item.section_byte_count / 4) for item in self.project_knowledge)


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
    revocation_reason: str | None = None
    revocation_scope: ContextRevocationScope | None = None

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
        if self.revocation_reason is not None:
            object.__setattr__(
                self,
                "revocation_reason",
                _text(self.revocation_reason, "package revocation_reason"),
            )
        if self.revocation_scope is not None:
            try:
                object.__setattr__(
                    self,
                    "revocation_scope",
                    ContextRevocationScope(self.revocation_scope),
                )
            except (TypeError, ValueError) as exc:
                raise ValidationError("invalid package revocation scope") from exc
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

    @property
    def manifest_schema_version(self) -> int:
        return 1

    def compute_package_hash(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.hash_document())).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextPackageManifestV2(ContextPackageManifestV1):
    result_input_manifest: ResultInputManifestV1

    @classmethod
    def create(  # pyright: ignore[reportIncompatibleMethodOverride]
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
        result_input_manifest: ResultInputManifestV1,
    ) -> ContextPackageManifestV2:
        return cls(
            package_id=package_id,
            workspace_id=workspace_id,
            purpose=purpose,
            consumer_type=consumer_type,
            consumer_id=consumer_id,
            worker_item_id=worker_item_id,
            work_context_revision_id=work_context_revision_id,
            context_version=context_version,
            control_plane_fact_snapshot=control_plane_fact_snapshot,
            knowledge_refs=tuple(knowledge_refs),
            artifact_refs=tuple(artifact_refs),
            evidence_refs=tuple(evidence_refs),
            budget=budget,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
            status=ContextPackageStatus.ACTIVE,
            created_at=created_at,
            result_input_manifest=result_input_manifest,
        )

    @property
    def manifest_schema_version(self) -> int:
        return 2

    def __post_init__(self) -> None:
        if not isinstance(self.result_input_manifest, ResultInputManifestV1):
            raise ValidationError("context package v2 result input manifest must be typed")
        ContextPackageManifestV1.__post_init__(self)

    def hash_document(self) -> dict[str, object]:
        document = ContextPackageManifestV1.hash_document(self)
        document["manifest_schema_version"] = 2
        document["result_input_manifest"] = self.result_input_manifest.to_mapping()
        return document


ContextPackageManifest = ContextPackageManifestV1 | ContextPackageManifestV2
ContextMaterialization = ContextMaterializationV1 | ContextMaterializationV2


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
    schema_version: int = 1
    operation_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.package_id, UUID) or not isinstance(self.consumer_id, UUID):
            raise ValidationError("ack package/consumer IDs must be UUIDs")
        if not isinstance(self.adapter_module_id, UUID):
            raise ValidationError("ack adapter_module_id must be a UUID")
        if self.operation_id is not None and not isinstance(self.operation_id, UUID):
            raise ValidationError("ack operation_id must be a UUID")
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
        _positive(self.schema_version, "ack schema_version")
        _aware(self.acknowledged_at, "acknowledged_at")


@dataclass(frozen=True, slots=True)
class ContextAssemblyRequest:
    request_id: UUID
    workspace_id: UUID
    purpose: ContextPurpose
    consumer_type: ContextConsumerType
    consumer_id: UUID
    worker_item_id: UUID | None
    work_context_revision_id: UUID
    context_version: int | None
    knowledge_search_scope: Mapping[str, object]
    knowledge_filter_refs: tuple[object, ...]
    fact_scope_refs: tuple[object, ...]
    budget: Mapping[str, object]
    expires_at: datetime
    status: ContextAssemblyStatus
    lease_owner: str | None
    lease_expires_at: datetime | None
    attempt: int
    next_attempt_at: datetime | None
    error_code: str | None
    request_fingerprint: str
    idempotency_key: str
    version: int
    created_at: datetime
    updated_at: datetime
    manifest_schema_version: int = 1
    input_selection_id: UUID | None = None
    input_selection_hash: str | None = None
    result_input_budget: ResultInputBudgetV1 | None = None

    @classmethod
    def create(
        cls,
        *,
        request_id: UUID,
        workspace_id: UUID,
        purpose: ContextPurpose,
        consumer_type: ContextConsumerType,
        consumer_id: UUID,
        worker_item_id: UUID | None,
        work_context_revision_id: UUID,
        context_version: int | None,
        knowledge_search_scope: Mapping[str, object],
        knowledge_filter_refs: Sequence[object],
        fact_scope_refs: Sequence[object],
        budget: Mapping[str, object],
        expires_at: datetime,
        request_fingerprint: str,
        idempotency_key: str,
        created_at: datetime,
        manifest_schema_version: int = 1,
        input_selection_id: UUID | None = None,
        input_selection_hash: str | None = None,
        result_input_budget: ResultInputBudgetV1 | None = None,
    ) -> ContextAssemblyRequest:
        return cls(
            request_id=request_id,
            workspace_id=workspace_id,
            purpose=purpose,
            consumer_type=consumer_type,
            consumer_id=consumer_id,
            worker_item_id=worker_item_id,
            work_context_revision_id=work_context_revision_id,
            context_version=context_version,
            knowledge_search_scope=knowledge_search_scope,
            knowledge_filter_refs=tuple(knowledge_filter_refs),
            fact_scope_refs=tuple(fact_scope_refs),
            budget=budget,
            expires_at=expires_at,
            status=ContextAssemblyStatus.QUEUED,
            lease_owner=None,
            lease_expires_at=None,
            attempt=1,
            next_attempt_at=None,
            error_code=None,
            request_fingerprint=request_fingerprint,
            idempotency_key=idempotency_key,
            version=1,
            created_at=created_at,
            updated_at=created_at,
            manifest_schema_version=manifest_schema_version,
            input_selection_id=input_selection_id,
            input_selection_hash=input_selection_hash,
            result_input_budget=result_input_budget,
        )

    def __post_init__(self) -> None:
        for name in (
            "request_id",
            "workspace_id",
            "consumer_id",
            "work_context_revision_id",
        ):
            if not isinstance(getattr(self, name), UUID):
                raise ValidationError(f"assembly {name} must be a UUID")
        if self.worker_item_id is not None and not isinstance(self.worker_item_id, UUID):
            raise ValidationError("assembly worker_item_id must be a UUID")
        try:
            object.__setattr__(self, "purpose", ContextPurpose(self.purpose))
            object.__setattr__(self, "consumer_type", ContextConsumerType(self.consumer_type))
            object.__setattr__(self, "status", ContextAssemblyStatus(self.status))
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid assembly enum") from exc
        if self.purpose is ContextPurpose.PLANNING_RUN:
            if self.consumer_type is not ContextConsumerType.PLANNER:
                raise ValidationError("planning assembly must use planner consumer")
            if self.worker_item_id is not None or self.context_version is not None:
                raise ValidationError("planning assembly cannot have worker fields")
        elif self.purpose is ContextPurpose.EXECUTION:
            if self.consumer_type is not ContextConsumerType.WORKER:
                raise ValidationError("execution assembly must use worker consumer")
            if self.worker_item_id is None or (self.context_version or 0) < 1:
                raise ValidationError("execution assembly requires worker fields")
        else:
            raise ValidationError("invalid assembly purpose")
        if self.manifest_schema_version == 1:
            if any(
                value is not None
                for value in (
                    self.input_selection_id,
                    self.input_selection_hash,
                    self.result_input_budget,
                )
            ):
                raise ValidationError("v1 context assembly cannot contain result input fields")
        elif self.manifest_schema_version == 2:
            if self.purpose is not ContextPurpose.EXECUTION:
                raise ValidationError("v2 context assembly requires execution purpose")
            if not isinstance(self.input_selection_id, UUID):
                raise ValidationError("v2 context assembly requires input_selection_id")
            if self.input_selection_hash is None:
                raise ValidationError("v2 context assembly requires input_selection_hash")
            object.__setattr__(
                self,
                "input_selection_hash",
                _hash(self.input_selection_hash, "input_selection_hash"),
            )
            if not isinstance(self.result_input_budget, ResultInputBudgetV1):
                raise ValidationError("v2 context assembly requires result_input_budget")
        else:
            raise ValidationError("unsupported context assembly manifest_schema_version")
        _positive(self.manifest_schema_version, "assembly manifest_schema_version")
        if self.input_selection_id is not None and not isinstance(self.input_selection_id, UUID):
            raise ValidationError("assembly input_selection_id must be a UUID")
        if self.input_selection_hash is not None:
            object.__setattr__(
                self,
                "input_selection_hash",
                _hash(self.input_selection_hash, "input_selection_hash"),
            )
        object.__setattr__(
            self,
            "knowledge_search_scope",
            cast(
                Mapping[str, object],
                _freeze_json(self.knowledge_search_scope, "knowledge_search_scope"),
            ),
        )
        object.__setattr__(
            self,
            "knowledge_filter_refs",
            tuple(_freeze_json(item, "knowledge_filter_refs") for item in self.knowledge_filter_refs),
        )
        object.__setattr__(
            self,
            "fact_scope_refs",
            tuple(_freeze_json(item, "fact_scope_refs") for item in self.fact_scope_refs),
        )
        object.__setattr__(
            self,
            "budget",
            cast(Mapping[str, object], _freeze_json(self.budget, "budget")),
        )
        _aware(self.expires_at, "assembly expires_at")
        _aware(self.created_at, "assembly created_at")
        _aware(self.updated_at, "assembly updated_at")
        if self.expires_at <= self.created_at:
            raise ValidationError("assembly expires_at must be after created_at")
        if self.lease_expires_at is not None:
            _aware(self.lease_expires_at, "assembly lease_expires_at")
        if self.next_attempt_at is not None:
            _aware(self.next_attempt_at, "assembly next_attempt_at")
        _positive(self.attempt, "assembly attempt")
        _positive(self.version, "assembly version")
        object.__setattr__(
            self,
            "request_fingerprint",
            _hash(self.request_fingerprint, "assembly request_fingerprint"),
        )
        object.__setattr__(self, "idempotency_key", _text(self.idempotency_key, "assembly idempotency_key"))
        if self.error_code is not None:
            object.__setattr__(self, "error_code", _text(self.error_code, "assembly error_code"))


@dataclass(frozen=True, slots=True)
class ContextMaterializationAudit:
    audit_id: UUID
    package_id: UUID
    workspace_id: UUID
    consumer_type: ContextConsumerType
    consumer_id: UUID
    context_version: int | None
    attempt: int
    outcome: ContextMaterializationOutcome
    section_count: int
    byte_count: int
    input_token_count: int
    failure_code: str | None
    occurred_at: datetime

    def __post_init__(self) -> None:
        for name in ("audit_id", "package_id", "workspace_id", "consumer_id"):
            if not isinstance(getattr(self, name), UUID):
                raise ValidationError(f"materialization audit {name} must be a UUID")
        try:
            object.__setattr__(self, "consumer_type", ContextConsumerType(self.consumer_type))
            object.__setattr__(self, "outcome", ContextMaterializationOutcome(self.outcome))
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid materialization audit enum") from exc
        if self.consumer_type is ContextConsumerType.PLANNER and self.context_version is not None:
            raise ValidationError("planner audit cannot have context_version")
        if self.consumer_type is ContextConsumerType.WORKER and (self.context_version or 0) < 1:
            raise ValidationError("worker audit requires context_version")
        _positive(self.attempt, "materialization audit attempt")
        for name in ("section_count", "byte_count", "input_token_count"):
            _non_negative(getattr(self, name), f"materialization audit {name}")
        if self.failure_code is not None:
            object.__setattr__(self, "failure_code", _text(self.failure_code, "audit failure_code"))
        _aware(self.occurred_at, "materialization audit occurred_at")


def _sorted_knowledge_refs(
    values: Sequence[ContextPackageKnowledgeRef],
) -> tuple[ContextPackageKnowledgeRef, ...]:
    if any(not isinstance(item, ContextPackageKnowledgeRef) for item in values):
        raise ValidationError("package knowledge refs must be typed")
    return tuple(sorted(values, key=lambda item: (item.ordinal, item.page_id, item.revision_id, item.section_key)))


def _result_input_mapping(
    value: object, name: str, allowed: set[str]
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be a mapping")
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ValidationError(f"unknown {name} fields: {', '.join(unknown)}")
    missing = sorted(key for key in allowed if key not in value)
    if missing:
        raise ValidationError(f"missing {name} fields: {', '.join(missing)}")
    return cast(Mapping[str, object], value)


def _result_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    return value.strip()


def _result_positive_int(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValidationError(f"{name} must be a positive integer")
    return value


def _result_non_negative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValidationError(f"{name} must be a non-negative integer")
    return value


def _result_uuid(value: object, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationError(f"{name} must be a UUID") from exc


def _result_hash(value: object, name: str) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{name} must be a lowercase SHA-256 hash")
    return value


def _result_text_array(
    value: object,
    name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{name} must be an array")
    result = tuple(_result_text(item, name) for item in value)
    if not allow_empty and not result:
        raise ValidationError(f"{name} must not be empty")
    if len(result) != len(set(result)):
        raise ValidationError(f"{name} must not contain duplicates")
    return result


def _result_uuid_array(
    value: object,
    name: str,
    *,
    allow_empty: bool = False,
) -> tuple[UUID, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{name} must be an array")
    result = tuple(_result_uuid(item, name) for item in value)
    if not allow_empty and not result:
        raise ValidationError(f"{name} must not be empty")
    if len(result) != len(set(result)):
        raise ValidationError(f"{name} must not contain duplicates")
    return result


def _result_sources(value: object) -> tuple[ResultInputSourceProvenanceV1, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("result input sources must be an array")
    result = tuple(
        item
        if isinstance(item, ResultInputSourceProvenanceV1)
        else ResultInputSourceProvenanceV1.from_mapping(item)
        for item in value
    )
    if not result:
        raise ValidationError("result input sources must not be empty")
    if len(result) != len(set(result)):
        raise ValidationError("result input sources must not contain duplicates")
    return tuple(sorted(result, key=_result_source_sort_key))


def _result_source_sort_key(
    source: ResultInputSourceProvenanceV1,
) -> tuple[str, str, str, str]:
    return (
        source.worker_item_key,
        str(source.effective_execution_id),
        str(source.effective_request_id),
        source.source_basis_hash,
    )


def _result_groups(value: object) -> tuple[ResultInputGroupManifestV1, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("result input groups must be an array")
    result = tuple(
        item
        if isinstance(item, ResultInputGroupManifestV1)
        else ResultInputGroupManifestV1.from_mapping(item)
        for item in value
    )
    keys = tuple(item.group_key for item in result)
    if len(keys) != len(set(keys)):
        raise ValidationError("result input group keys must be unique")
    return result


def _result_hard_inputs(value: object) -> tuple[ResultInputHardInputManifestV1, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("result input hard inputs must be an array")
    result = tuple(
        item
        if isinstance(item, ResultInputHardInputManifestV1)
        else ResultInputHardInputManifestV1.from_mapping(item)
        for item in value
    )
    keys = tuple(item.dependency_key for item in result)
    if len(keys) != len(set(keys)):
        raise ValidationError("result input hard input keys must be unique")
    return result


def _result_artifacts(value: object) -> tuple[ResultInputArtifactManifestV1, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("result input artifacts must be an array")
    result = tuple(
        item
        if isinstance(item, ResultInputArtifactManifestV1)
        else ResultInputArtifactManifestV1.from_mapping(item)
        for item in value
    )
    return tuple(sorted(result, key=lambda item: str(item.artifact_id)))


def _result_evidence(value: object) -> tuple[ResultInputEvidenceManifestV1, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("result input evidence must be an array")
    result = tuple(
        item
        if isinstance(item, ResultInputEvidenceManifestV1)
        else ResultInputEvidenceManifestV1.from_mapping(item)
        for item in value
    )
    return tuple(sorted(result, key=lambda item: str(item.evidence_id)))


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
        "expires_at": expires_at.astimezone(UTC).isoformat(),
    }
