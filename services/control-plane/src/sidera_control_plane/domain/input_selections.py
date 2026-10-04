from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.result_inputs import (
    ResultInputArtifact,
    ResultInputEvidence,
)

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT_LENGTH = 16_384


@dataclass(frozen=True, slots=True)
class InputSelectionSourceV1:
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
    artifacts: tuple[ResultInputArtifact, ...]
    evidence: tuple[ResultInputEvidence, ...]

    @classmethod
    def from_mapping(cls, value: object) -> InputSelectionSourceV1:
        mapping = _strict_mapping(
            value,
            "selection source",
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
                "artifacts",
                "evidence",
            },
        )
        return cls(
            worker_item_key=_key(mapping.get("worker_item_key"), "worker_item_key"),
            worker_item_id=_uuid(mapping.get("worker_item_id"), "worker_item_id"),
            subtask_path=_text_array(mapping.get("subtask_path"), "subtask_path"),
            effective_request_id=_uuid(
                mapping.get("effective_request_id"), "effective_request_id"
            ),
            allowed_lineage_ids=_uuid_array(
                mapping.get("allowed_lineage_ids"),
                "allowed_lineage_ids",
                require_non_empty=True,
            ),
            effective_lineage_id=_uuid(
                mapping.get("effective_lineage_id"), "effective_lineage_id"
            ),
            effective_execution_id=_uuid(
                mapping.get("effective_execution_id"), "effective_execution_id"
            ),
            attempt=_positive_int(mapping.get("attempt"), "attempt"),
            inheritance_refs=_text_array(
                mapping.get("inheritance_refs"),
                "inheritance_refs",
                require_non_empty=False,
                sort=True,
            ),
            priority=_positive_int(mapping.get("priority"), "priority"),
            source_basis_hash=_hash(
                mapping.get("source_basis_hash"), "source_basis_hash"
            ),
            artifacts=_artifact_array(mapping.get("artifacts")),
            evidence=_evidence_array(mapping.get("evidence")),
        )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "worker_item_key", _key(self.worker_item_key, "worker_item_key")
        )
        _uuid(self.worker_item_id, "worker_item_id")
        object.__setattr__(
            self, "subtask_path", _text_array(self.subtask_path, "subtask_path")
        )
        _uuid(self.effective_request_id, "effective_request_id")
        object.__setattr__(
            self,
            "allowed_lineage_ids",
            _uuid_array(
                self.allowed_lineage_ids,
                "allowed_lineage_ids",
                require_non_empty=True,
            ),
        )
        _uuid(self.effective_lineage_id, "effective_lineage_id")
        if self.effective_lineage_id not in self.allowed_lineage_ids:
            raise ValidationError(
                "effective_lineage_id must be present in allowed_lineage_ids"
            )
        _uuid(self.effective_execution_id, "effective_execution_id")
        _positive_int(self.attempt, "attempt")
        object.__setattr__(
            self,
            "inheritance_refs",
            _text_array(
                self.inheritance_refs,
                "inheritance_refs",
                require_non_empty=False,
                sort=True,
            ),
        )
        _positive_int(self.priority, "priority")
        object.__setattr__(
            self,
            "source_basis_hash",
            _hash(self.source_basis_hash, "source_basis_hash"),
        )
        object.__setattr__(self, "artifacts", _artifact_array(self.artifacts))
        object.__setattr__(self, "evidence", _evidence_array(self.evidence))
        artifact_ids = {artifact.artifact_id for artifact in self.artifacts}
        evidence_artifact_ids = {
            artifact_id
            for evidence in self.evidence
            for artifact_id in evidence.artifact_ids
        }
        if not evidence_artifact_ids.issubset(artifact_ids):
            raise ValidationError(
                "selection source Evidence references an artifact outside its closure"
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
            "artifacts": [item.to_mapping() for item in self.artifacts],
            "evidence": [item.to_mapping() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class InputSelectionGroupV1:
    group_key: str
    rule_hash: str
    sources: tuple[InputSelectionSourceV1, ...]

    @classmethod
    def from_mapping(cls, value: object) -> InputSelectionGroupV1:
        mapping = _strict_mapping(
            value,
            "selection group",
            {"group_key", "rule_hash", "sources"},
        )
        return cls(
            group_key=_key(mapping.get("group_key"), "group_key"),
            rule_hash=_hash(mapping.get("rule_hash"), "rule_hash"),
            sources=_source_array(mapping.get("sources")),
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_key", _key(self.group_key, "group_key"))
        object.__setattr__(self, "rule_hash", _hash(self.rule_hash, "rule_hash"))
        object.__setattr__(self, "sources", _source_array(self.sources))
        worker_keys = tuple(item.worker_item_key for item in self.sources)
        priorities = tuple(item.priority for item in self.sources)
        if len(worker_keys) != len(set(worker_keys)):
            raise ValidationError("selection group source WorkerItems must be unique")
        if len(priorities) != len(set(priorities)):
            raise ValidationError("selection group source priorities must be unique")

    def to_mapping(self) -> dict[str, object]:
        return {
            "group_key": self.group_key,
            "rule_hash": self.rule_hash,
            "sources": [item.to_mapping() for item in self.sources],
        }


@dataclass(frozen=True, slots=True)
class InputSelectionHardInputV1:
    dependency_key: str
    rule_hash: str
    source: InputSelectionSourceV1

    @classmethod
    def from_mapping(cls, value: object) -> InputSelectionHardInputV1:
        mapping = _strict_mapping(
            value,
            "selection hard input",
            {"dependency_key", "rule_hash", "source"},
        )
        return cls(
            dependency_key=_key(mapping.get("dependency_key"), "dependency_key"),
            rule_hash=_hash(mapping.get("rule_hash"), "rule_hash"),
            source=InputSelectionSourceV1.from_mapping(mapping.get("source")),
        )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "dependency_key", _key(self.dependency_key, "dependency_key")
        )
        object.__setattr__(self, "rule_hash", _hash(self.rule_hash, "rule_hash"))
        if not isinstance(self.source, InputSelectionSourceV1):
            raise ValidationError("selection hard input source must be typed")

    def to_mapping(self) -> dict[str, object]:
        return {
            "dependency_key": self.dependency_key,
            "rule_hash": self.rule_hash,
            "source": self.source.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class CollaborationInputSelectionV1:
    id: UUID
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    run_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    plan_content_hash: str
    target_worker_item_id: UUID
    target_execution_request_id: UUID
    target_request_version: int
    projection_snapshot_id: UUID
    projection_hash: str
    decision_id: UUID
    intent_id: UUID
    groups: tuple[InputSelectionGroupV1, ...]
    hard_inputs: tuple[InputSelectionHardInputV1, ...]
    selection_hash: str
    created_at: datetime

    @classmethod
    def from_mapping(cls, value: object) -> CollaborationInputSelectionV1:
        mapping = _strict_mapping(
            value,
            "selection",
            {
                "id",
                "schema_version",
                "workspace_id",
                "root_task_id",
                "run_id",
                "plan_snapshot_id",
                "plan_generation",
                "plan_content_hash",
                "target_worker_item_id",
                "target_execution_request_id",
                "target_request_version",
                "projection_snapshot_id",
                "projection_hash",
                "decision_id",
                "intent_id",
                "groups",
                "hard_inputs",
                "selection_hash",
                "created_at",
            },
        )
        value_object = cls(
            id=_uuid(mapping.get("id"), "id"),
            schema_version=_positive_int(
                mapping.get("schema_version"), "schema_version"
            ),
            workspace_id=_uuid(mapping.get("workspace_id"), "workspace_id"),
            root_task_id=_uuid(mapping.get("root_task_id"), "root_task_id"),
            run_id=_uuid(mapping.get("run_id"), "run_id"),
            plan_snapshot_id=_uuid(mapping.get("plan_snapshot_id"), "plan_snapshot_id"),
            plan_generation=_positive_int(
                mapping.get("plan_generation"), "plan_generation"
            ),
            plan_content_hash=_hash(
                mapping.get("plan_content_hash"), "plan_content_hash"
            ),
            target_worker_item_id=_uuid(
                mapping.get("target_worker_item_id"), "target_worker_item_id"
            ),
            target_execution_request_id=_uuid(
                mapping.get("target_execution_request_id"),
                "target_execution_request_id",
            ),
            target_request_version=_positive_int(
                mapping.get("target_request_version"), "target_request_version"
            ),
            projection_snapshot_id=_uuid(
                mapping.get("projection_snapshot_id"), "projection_snapshot_id"
            ),
            projection_hash=_hash(mapping.get("projection_hash"), "projection_hash"),
            decision_id=_uuid(mapping.get("decision_id"), "decision_id"),
            intent_id=_uuid(mapping.get("intent_id"), "intent_id"),
            groups=_group_array(mapping.get("groups")),
            hard_inputs=_hard_input_array(mapping.get("hard_inputs")),
            selection_hash=_hash(mapping.get("selection_hash"), "selection_hash"),
            created_at=_datetime(mapping.get("created_at"), "created_at"),
        )
        if value_object.selection_hash != value_object.compute_selection_hash():
            raise ValidationError("selection_hash does not match selection contents")
        return value_object

    def __post_init__(self) -> None:
        _uuid(self.id, "id")
        if self.schema_version != 1:
            raise ValidationError("unsupported input selection schema_version")
        for name in (
            "workspace_id",
            "root_task_id",
            "run_id",
            "plan_snapshot_id",
            "target_worker_item_id",
            "target_execution_request_id",
            "projection_snapshot_id",
            "decision_id",
            "intent_id",
        ):
            _uuid(getattr(self, name), name)
        _positive_int(self.plan_generation, "plan_generation")
        _positive_int(self.target_request_version, "target_request_version")
        for name in ("plan_content_hash", "projection_hash", "selection_hash"):
            object.__setattr__(self, name, _hash(getattr(self, name), name))
        object.__setattr__(self, "groups", _group_array(self.groups))
        object.__setattr__(self, "hard_inputs", _hard_input_array(self.hard_inputs))
        if not self.groups and not self.hard_inputs:
            raise ValidationError("selection must contain groups or hard inputs")
        group_keys = tuple(item.group_key for item in self.groups)
        if len(group_keys) != len(set(group_keys)):
            raise ValidationError("selection group keys must be unique")
        dependency_keys = tuple(item.dependency_key for item in self.hard_inputs)
        if len(dependency_keys) != len(set(dependency_keys)):
            raise ValidationError("selection hard input keys must be unique")
        source_worker_ids = {
            source.worker_item_id for group in self.groups for source in group.sources
        }
        source_worker_ids.update(
            item.source.worker_item_id for item in self.hard_inputs
        )
        if self.target_worker_item_id in source_worker_ids:
            raise ValidationError("input selection cannot use its target as a source")
        _datetime(self.created_at, "created_at")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": str(self.id),
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "run_id": str(self.run_id),
            "plan_snapshot_id": str(self.plan_snapshot_id),
            "plan_generation": self.plan_generation,
            "plan_content_hash": self.plan_content_hash,
            "target_worker_item_id": str(self.target_worker_item_id),
            "target_execution_request_id": str(self.target_execution_request_id),
            "target_request_version": self.target_request_version,
            "projection_snapshot_id": str(self.projection_snapshot_id),
            "projection_hash": self.projection_hash,
            "decision_id": str(self.decision_id),
            "intent_id": str(self.intent_id),
            "groups": [item.to_mapping() for item in self.groups],
            "hard_inputs": [item.to_mapping() for item in self.hard_inputs],
            "selection_hash": self.selection_hash,
            "created_at": self.created_at.isoformat(),
        }

    def compute_selection_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "run_id": str(self.run_id),
            "plan_snapshot_id": str(self.plan_snapshot_id),
            "plan_generation": self.plan_generation,
            "plan_content_hash": self.plan_content_hash,
            "target_worker_item_id": str(self.target_worker_item_id),
            "target_execution_request_id": str(self.target_execution_request_id),
            "target_request_version": self.target_request_version,
            "groups": [item.to_mapping() for item in self.groups],
            "hard_inputs": [item.to_mapping() for item in self.hard_inputs],
        }
        return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _strict_mapping(
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


def _uuid(value: object, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationError(f"{name} must be a UUID") from exc


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValidationError(f"{name} must be a positive integer")
    return value


def _hash(value: object, name: str) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{name} must be a lowercase SHA-256 hash")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    normalized = value.strip()
    if len(normalized) > _MAX_TEXT_LENGTH:
        raise ValidationError(f"{name} is too long")
    return normalized


def _key(value: object, name: str) -> str:
    normalized = _text(value, name)
    if any(character.isspace() for character in normalized):
        raise ValidationError(f"{name} cannot contain whitespace")
    return normalized


def _text_array(
    value: object,
    name: str,
    *,
    require_non_empty: bool = True,
    sort: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{name} must be an array")
    items = tuple(_text(item, name) for item in value)
    if require_non_empty and not items:
        raise ValidationError(f"{name} must not be empty")
    if len(items) != len(set(items)):
        raise ValidationError(f"{name} must be unique")
    return tuple(sorted(items)) if sort else items


def _uuid_array(
    value: object, name: str, *, require_non_empty: bool = False
) -> tuple[UUID, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{name} must be an array")
    items = tuple(sorted((_uuid(item, name) for item in value), key=str))
    if require_non_empty and not items:
        raise ValidationError(f"{name} must not be empty")
    if len(items) != len(set(items)):
        raise ValidationError(f"{name} must be unique")
    return items


def _artifact_array(value: object) -> tuple[ResultInputArtifact, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("selection source artifacts must be an array")
    items = tuple(
        item
        if isinstance(item, ResultInputArtifact)
        else ResultInputArtifact.from_mapping(item)
        for item in value
    )
    if not items:
        raise ValidationError("selection source artifacts must not be empty")
    items = tuple(sorted(items, key=lambda item: str(item.artifact_id)))
    if len(items) != len({item.artifact_id for item in items}):
        raise ValidationError("selection source artifact ids must be unique")
    return items


def _evidence_array(value: object) -> tuple[ResultInputEvidence, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("selection source evidence must be an array")
    items = tuple(
        item
        if isinstance(item, ResultInputEvidence)
        else ResultInputEvidence.from_mapping(item)
        for item in value
    )
    items = tuple(sorted(items, key=lambda item: str(item.evidence_id)))
    if len(items) != len({item.evidence_id for item in items}):
        raise ValidationError("selection source evidence ids must be unique")
    return items


def _source_array(value: object) -> tuple[InputSelectionSourceV1, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("selection sources must be an array")
    items = tuple(
        item
        if isinstance(item, InputSelectionSourceV1)
        else InputSelectionSourceV1.from_mapping(item)
        for item in value
    )
    if not items:
        raise ValidationError("selection sources must not be empty")
    return tuple(sorted(items, key=lambda item: (item.priority, item.worker_item_key)))


def _group_array(value: object) -> tuple[InputSelectionGroupV1, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("selection groups must be an array")
    items = tuple(
        item
        if isinstance(item, InputSelectionGroupV1)
        else InputSelectionGroupV1.from_mapping(item)
        for item in value
    )
    return tuple(sorted(items, key=lambda item: item.group_key))


def _hard_input_array(value: object) -> tuple[InputSelectionHardInputV1, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("selection hard_inputs must be an array")
    items = tuple(
        item
        if isinstance(item, InputSelectionHardInputV1)
        else InputSelectionHardInputV1.from_mapping(item)
        for item in value
    )
    return tuple(sorted(items, key=lambda item: item.dependency_key))


def _datetime(value: object, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{name} must be an ISO-8601 timestamp") from exc
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValidationError(f"{name} must be timezone aware")
    return value


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValidationError("input selection cannot be canonicalized") from exc
