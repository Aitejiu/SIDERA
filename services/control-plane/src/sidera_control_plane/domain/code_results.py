from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID_PATTERN = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


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


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
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


def _oid(value: object, label: str) -> str:
    result = _text(value, label).lower()
    if _GIT_OID_PATTERN.fullmatch(result) is None:
        raise ValidationError(f"{label} must be a Git object id")
    return result


def _hash(value: object, label: str) -> str:
    result = _text(value, label)
    if _HASH_PATTERN.fullmatch(result) is None:
        raise ValidationError(f"{label} must be a lowercase SHA-256 hash")
    return result


def _aware_datetime(value: object, label: str) -> datetime:
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
        raise ValidationError("code result cannot be canonicalized") from exc
    return hashlib.sha256(encoded).hexdigest()


def _verify_content_hash(actual: str, expected: str, label: str) -> None:
    if actual != expected:
        raise ValidationError(f"{label} content hash mismatch")


_SNAPSHOT_FIELDS = frozenset(
    {
        "id",
        "schema_version",
        "workspace_id",
        "root_task_id",
        "worker_item_id",
        "request_id",
        "execution_id",
        "repository_ref",
        "input_commit",
        "input_tree",
        "result_commit",
        "result_tree",
        "consumed_result_ids",
        "artifact_ids",
        "capture_operation_id",
        "content_hash",
        "created_at",
    }
)


@dataclass(frozen=True, slots=True)
class CodeResultSnapshotV1:
    id: UUID
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    worker_item_id: UUID
    request_id: UUID
    execution_id: UUID
    repository_ref: str
    input_commit: str
    input_tree: str
    result_commit: str
    result_tree: str
    consumed_result_ids: tuple[UUID, ...]
    artifact_ids: tuple[UUID, ...]
    capture_operation_id: UUID
    content_hash: str
    created_at: datetime

    @classmethod
    def from_mapping(cls, value: object) -> CodeResultSnapshotV1:
        mapping = _mapping(value, "code result snapshot", _SNAPSHOT_FIELDS)
        schema_version = mapping["schema_version"]
        if isinstance(schema_version, bool) or schema_version != 1:
            raise ValidationError("unsupported code result snapshot schema_version")
        return cls(
            id=_uuid(mapping["id"], "snapshot id"),
            schema_version=1,
            workspace_id=_uuid(mapping["workspace_id"], "snapshot workspace_id"),
            root_task_id=_uuid(mapping["root_task_id"], "snapshot root_task_id"),
            worker_item_id=_uuid(mapping["worker_item_id"], "snapshot worker_item_id"),
            request_id=_uuid(mapping["request_id"], "snapshot request_id"),
            execution_id=_uuid(mapping["execution_id"], "snapshot execution_id"),
            repository_ref=_text(mapping["repository_ref"], "snapshot repository_ref"),
            input_commit=_oid(mapping["input_commit"], "snapshot input_commit"),
            input_tree=_oid(mapping["input_tree"], "snapshot input_tree"),
            result_commit=_oid(mapping["result_commit"], "snapshot result_commit"),
            result_tree=_oid(mapping["result_tree"], "snapshot result_tree"),
            consumed_result_ids=_unique_uuids(
                mapping["consumed_result_ids"], "snapshot consumed_result_ids"
            ),
            artifact_ids=_unique_uuids(mapping["artifact_ids"], "snapshot artifact_ids"),
            capture_operation_id=_uuid(
                mapping["capture_operation_id"], "snapshot capture_operation_id"
            ),
            content_hash=_hash(mapping["content_hash"], "snapshot content_hash"),
            created_at=_aware_datetime(mapping["created_at"], "snapshot created_at"),
        )

    def _content_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "worker_item_id": str(self.worker_item_id),
            "request_id": str(self.request_id),
            "execution_id": str(self.execution_id),
            "repository_ref": self.repository_ref,
            "input_commit": self.input_commit,
            "input_tree": self.input_tree,
            "result_commit": self.result_commit,
            "result_tree": self.result_tree,
            "consumed_result_ids": [str(item) for item in self.consumed_result_ids],
            "artifact_ids": [str(item) for item in self.artifact_ids],
            "capture_operation_id": str(self.capture_operation_id),
        }

    def compute_content_hash(self) -> str:
        return _canonical_hash(self._content_mapping())

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValidationError("unsupported code result snapshot schema_version")
        for value, label in (
            (self.id, "snapshot id"),
            (self.workspace_id, "snapshot workspace_id"),
            (self.root_task_id, "snapshot root_task_id"),
            (self.worker_item_id, "snapshot worker_item_id"),
            (self.request_id, "snapshot request_id"),
            (self.execution_id, "snapshot execution_id"),
            (self.capture_operation_id, "snapshot capture_operation_id"),
        ):
            _uuid(value, label)
        _text(self.repository_ref, "snapshot repository_ref")
        for value, label in (
            (self.input_commit, "snapshot input_commit"),
            (self.input_tree, "snapshot input_tree"),
            (self.result_commit, "snapshot result_commit"),
            (self.result_tree, "snapshot result_tree"),
        ):
            _oid(value, label)
        _hash(self.content_hash, "snapshot content_hash")
        _aware_datetime(self.created_at, "snapshot created_at")
        if not isinstance(self.consumed_result_ids, tuple) or not isinstance(
            self.artifact_ids, tuple
        ):
            raise ValidationError("snapshot identity arrays must be tuples")
        _verify_content_hash(self.compute_content_hash(), self.content_hash, "snapshot")

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "schema_version": self.schema_version,
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "worker_item_id": str(self.worker_item_id),
            "request_id": str(self.request_id),
            "execution_id": str(self.execution_id),
            "repository_ref": self.repository_ref,
            "input_commit": self.input_commit,
            "input_tree": self.input_tree,
            "result_commit": self.result_commit,
            "result_tree": self.result_tree,
            "consumed_result_ids": [str(item) for item in self.consumed_result_ids],
            "artifact_ids": [str(item) for item in self.artifact_ids],
            "capture_operation_id": str(self.capture_operation_id),
            "content_hash": self.content_hash,
            "created_at": self.created_at.isoformat(),
        }


_CANDIDATE_FIELDS = frozenset(
    {
        "id",
        "schema_version",
        "workspace_id",
        "root_task_id",
        "plan_snapshot_id",
        "plan_content_hash",
        "integration_execution_id",
        "base_commit",
        "source_result_ids",
        "input_selection_id",
        "input_selection_hash",
        "code_result_id",
        "previous_candidate_id",
        "content_hash",
        "created_at",
    }
)


@dataclass(frozen=True, slots=True)
class IntegrationCandidateV1:
    id: UUID
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    plan_snapshot_id: UUID
    plan_content_hash: str
    integration_execution_id: UUID
    base_commit: str
    source_result_ids: tuple[UUID, ...]
    input_selection_id: UUID
    input_selection_hash: str
    code_result_id: UUID
    previous_candidate_id: UUID | None
    content_hash: str
    created_at: datetime

    @classmethod
    def from_mapping(cls, value: object) -> IntegrationCandidateV1:
        mapping = _mapping(value, "integration candidate", _CANDIDATE_FIELDS)
        schema_version = mapping["schema_version"]
        if isinstance(schema_version, bool) or schema_version != 1:
            raise ValidationError("unsupported integration candidate schema_version")
        source_result_ids = _unique_uuids(
            mapping["source_result_ids"], "candidate source_result_ids"
        )
        if not source_result_ids:
            raise ValidationError("candidate source_result_ids must not be empty")
        return cls(
            id=_uuid(mapping["id"], "candidate id"),
            schema_version=1,
            workspace_id=_uuid(mapping["workspace_id"], "candidate workspace_id"),
            root_task_id=_uuid(mapping["root_task_id"], "candidate root_task_id"),
            plan_snapshot_id=_uuid(mapping["plan_snapshot_id"], "candidate plan_snapshot_id"),
            plan_content_hash=_hash(mapping["plan_content_hash"], "candidate plan_content_hash"),
            integration_execution_id=_uuid(
                mapping["integration_execution_id"], "candidate integration_execution_id"
            ),
            base_commit=_oid(mapping["base_commit"], "candidate base_commit"),
            source_result_ids=source_result_ids,
            input_selection_id=_uuid(mapping["input_selection_id"], "candidate input_selection_id"),
            input_selection_hash=_hash(
                mapping["input_selection_hash"], "candidate input_selection_hash"
            ),
            code_result_id=_uuid(mapping["code_result_id"], "candidate code_result_id"),
            previous_candidate_id=(
                None
                if mapping["previous_candidate_id"] is None
                else _uuid(mapping["previous_candidate_id"], "candidate previous_candidate_id")
            ),
            content_hash=_hash(mapping["content_hash"], "candidate content_hash"),
            created_at=_aware_datetime(mapping["created_at"], "candidate created_at"),
        )

    def _content_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "plan_snapshot_id": str(self.plan_snapshot_id),
            "plan_content_hash": self.plan_content_hash,
            "integration_execution_id": str(self.integration_execution_id),
            "base_commit": self.base_commit,
            "source_result_ids": [str(item) for item in self.source_result_ids],
            "input_selection_id": str(self.input_selection_id),
            "input_selection_hash": self.input_selection_hash,
            "code_result_id": str(self.code_result_id),
            "previous_candidate_id": (
                None if self.previous_candidate_id is None else str(self.previous_candidate_id)
            ),
        }

    def compute_content_hash(self) -> str:
        return _canonical_hash(self._content_mapping())

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValidationError("unsupported integration candidate schema_version")
        for value, label in (
            (self.id, "candidate id"),
            (self.workspace_id, "candidate workspace_id"),
            (self.root_task_id, "candidate root_task_id"),
            (self.plan_snapshot_id, "candidate plan_snapshot_id"),
            (self.integration_execution_id, "candidate integration_execution_id"),
            (self.input_selection_id, "candidate input_selection_id"),
            (self.code_result_id, "candidate code_result_id"),
        ):
            _uuid(value, label)
        if self.previous_candidate_id is not None:
            _uuid(self.previous_candidate_id, "candidate previous_candidate_id")
        _hash(self.plan_content_hash, "candidate plan_content_hash")
        _hash(self.input_selection_hash, "candidate input_selection_hash")
        _hash(self.content_hash, "candidate content_hash")
        _oid(self.base_commit, "candidate base_commit")
        if not self.source_result_ids:
            raise ValidationError("candidate source_result_ids must not be empty")
        _verify_content_hash(self.compute_content_hash(), self.content_hash, "candidate")

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "schema_version": self.schema_version,
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "plan_snapshot_id": str(self.plan_snapshot_id),
            "plan_content_hash": self.plan_content_hash,
            "integration_execution_id": str(self.integration_execution_id),
            "base_commit": self.base_commit,
            "source_result_ids": [str(item) for item in self.source_result_ids],
            "input_selection_id": str(self.input_selection_id),
            "input_selection_hash": self.input_selection_hash,
            "code_result_id": str(self.code_result_id),
            "previous_candidate_id": (
                None if self.previous_candidate_id is None else str(self.previous_candidate_id)
            ),
            "content_hash": self.content_hash,
            "created_at": self.created_at.isoformat(),
        }


_SOURCE_FIELDS = frozenset(
    {
        "result_id",
        "result_hash",
        "input_commit",
        "input_tree",
        "result_commit",
        "result_tree",
    }
)


@dataclass(frozen=True, slots=True)
class CodeInputSourceV1:
    result_id: UUID
    result_hash: str
    input_commit: str
    input_tree: str
    result_commit: str
    result_tree: str

    @classmethod
    def from_mapping(cls, value: object) -> CodeInputSourceV1:
        mapping = _mapping(value, "code input source", _SOURCE_FIELDS)
        return cls(
            result_id=_uuid(mapping["result_id"], "code input result_id"),
            result_hash=_hash(mapping["result_hash"], "code input result_hash"),
            input_commit=_oid(mapping["input_commit"], "code input input_commit"),
            input_tree=_oid(mapping["input_tree"], "code input input_tree"),
            result_commit=_oid(mapping["result_commit"], "code input result_commit"),
            result_tree=_oid(mapping["result_tree"], "code input result_tree"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "result_id": str(self.result_id),
            "result_hash": self.result_hash,
            "input_commit": self.input_commit,
            "input_tree": self.input_tree,
            "result_commit": self.result_commit,
            "result_tree": self.result_tree,
        }


_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "workspace_id",
        "root_task_id",
        "request_id",
        "execution_id",
        "selection_id",
        "selection_hash",
        "repository_ref",
        "base_commit",
        "sources",
        "input_hash",
    }
)


@dataclass(frozen=True, slots=True)
class CodeInputManifestV1:
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    request_id: UUID
    execution_id: UUID
    selection_id: UUID
    selection_hash: str
    repository_ref: str
    base_commit: str
    sources: tuple[CodeInputSourceV1, ...]
    input_hash: str

    @classmethod
    def from_mapping(cls, value: object) -> CodeInputManifestV1:
        mapping = _mapping(value, "code input manifest", _MANIFEST_FIELDS)
        schema_version = mapping["schema_version"]
        if isinstance(schema_version, bool) or schema_version != 1:
            raise ValidationError("unsupported code input manifest schema_version")
        sources = tuple(
            CodeInputSourceV1.from_mapping(item)
            for item in _sequence(mapping["sources"], "code input sources")
        )
        if not sources:
            raise ValidationError("code input sources must not be empty")
        if len({item.result_id for item in sources}) != len(sources):
            raise ValidationError("code input sources must not contain duplicates")
        return cls(
            schema_version=1,
            workspace_id=_uuid(mapping["workspace_id"], "manifest workspace_id"),
            root_task_id=_uuid(mapping["root_task_id"], "manifest root_task_id"),
            request_id=_uuid(mapping["request_id"], "manifest request_id"),
            execution_id=_uuid(mapping["execution_id"], "manifest execution_id"),
            selection_id=_uuid(mapping["selection_id"], "manifest selection_id"),
            selection_hash=_hash(mapping["selection_hash"], "manifest selection_hash"),
            repository_ref=_text(mapping["repository_ref"], "manifest repository_ref"),
            base_commit=_oid(mapping["base_commit"], "manifest base_commit"),
            sources=sources,
            input_hash=_hash(mapping["input_hash"], "manifest input_hash"),
        )

    def _content_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "request_id": str(self.request_id),
            "execution_id": str(self.execution_id),
            "selection_id": str(self.selection_id),
            "selection_hash": self.selection_hash,
            "repository_ref": self.repository_ref,
            "base_commit": self.base_commit,
            "sources": [item.to_mapping() for item in self.sources],
        }

    def compute_content_hash(self) -> str:
        return _canonical_hash(self._content_mapping())

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValidationError("unsupported code input manifest schema_version")
        for value, label in (
            (self.workspace_id, "manifest workspace_id"),
            (self.root_task_id, "manifest root_task_id"),
            (self.request_id, "manifest request_id"),
            (self.execution_id, "manifest execution_id"),
            (self.selection_id, "manifest selection_id"),
        ):
            _uuid(value, label)
        _hash(self.selection_hash, "manifest selection_hash")
        _hash(self.input_hash, "manifest input_hash")
        _text(self.repository_ref, "manifest repository_ref")
        _oid(self.base_commit, "manifest base_commit")
        if not self.sources:
            raise ValidationError("code input sources must not be empty")
        _verify_content_hash(self.compute_content_hash(), self.input_hash, "manifest input")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "request_id": str(self.request_id),
            "execution_id": str(self.execution_id),
            "selection_id": str(self.selection_id),
            "selection_hash": self.selection_hash,
            "repository_ref": self.repository_ref,
            "base_commit": self.base_commit,
            "sources": [item.to_mapping() for item in self.sources],
            "input_hash": self.input_hash,
        }


__all__ = [
    "CodeInputManifestV1",
    "CodeInputSourceV1",
    "CodeResultSnapshotV1",
    "IntegrationCandidateV1",
]
