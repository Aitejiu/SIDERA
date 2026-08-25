from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast
from uuid import UUID, uuid4

from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.events import EventData

if TYPE_CHECKING:
    from sidera_control_plane.application.ports import ControlPlaneUnitOfWork

logger = logging.getLogger(__name__)

MAX_CANDIDATE_PROPOSAL_BYTES = 512 * 1024
_FILENAME = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27}-[0-9a-f]{64}\.json$")
_FORBIDDEN_SCOPE_KEYS = frozenset(
    {"workspace_id", "task_id", "subtask_id", "worker_item_id", "actor_id", "review_decision"}
)


class CandidateProposalSpoolError(RuntimeError):
    """A safe, local failure while reading or writing candidate content."""


def _text(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{field_name} is required")
    return normalized


def _hash(value: object, field_name: str) -> str:
    normalized = _text(value, field_name)
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash")
    return normalized


def _uuid(value: object, field_name: str, *, required: bool = True) -> UUID | None:
    if value is None and not required:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        for key in value:
            if str(key) in _FORBIDDEN_SCOPE_KEYS:
                raise ValueError(f"proposal metadata cannot contain {key}")
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("proposal metadata must be JSON compatible")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class KnowledgeCandidateSectionV1:
    section_key: str
    heading: str
    summary: str
    body_markdown: str
    ordinal: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "section_key", _text(self.section_key, "section_key"))
        object.__setattr__(self, "heading", _text(self.heading, "heading"))
        object.__setattr__(self, "summary", _text(self.summary, "summary", allow_empty=True))
        object.__setattr__(self, "body_markdown", _text(self.body_markdown, "body_markdown"))
        if isinstance(self.ordinal, bool) or self.ordinal < 0:
            raise ValueError("section ordinal must be non-negative")

    def to_mapping(self) -> dict[str, object]:
        return {
            "section_key": self.section_key,
            "heading": self.heading,
            "summary": self.summary,
            "body_markdown": self.body_markdown,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeCandidateRelationV1:
    relation_key: str
    relation_kind: str
    target_page_id: UUID
    target_revision_id: UUID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "relation_key", _text(self.relation_key, "relation_key"))
        object.__setattr__(self, "relation_kind", _text(self.relation_kind, "relation_kind"))
        if not isinstance(self.target_page_id, UUID):
            raise TypeError("relation target_page_id must be a UUID")

    def to_mapping(self) -> dict[str, object]:
        return {
            "relation_key": self.relation_key,
            "relation_kind": self.relation_kind,
            "target_page_id": str(self.target_page_id),
            "target_revision_id": (
                str(self.target_revision_id) if self.target_revision_id is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class KnowledgeCandidateSourceSuggestionV1:
    source_type: str
    label: str
    source_id: UUID | None = None
    uri: str | None = None
    workspace_relative_path: str | None = None
    git_revision: str | None = None
    digest: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_type", _text(self.source_type, "source_type"))
        object.__setattr__(self, "label", _text(self.label, "source label"))
        if self.source_id is not None and not isinstance(self.source_id, UUID):
            raise ValueError("source_id must be a UUID")
        if self.digest is not None:
            object.__setattr__(self, "digest", _hash(self.digest, "source digest"))
        for field_name in (
            "uri",
            "workspace_relative_path",
            "git_revision",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"source {field_name} must be text")
        object.__setattr__(self, "metadata", cast(Mapping[str, object], _json_value(self.metadata)))

    def to_mapping(self) -> dict[str, object]:
        return {
            "source_type": self.source_type,
            "label": self.label,
            "source_id": str(self.source_id) if self.source_id is not None else None,
            "uri": self.uri,
            "workspace_relative_path": self.workspace_relative_path,
            "git_revision": self.git_revision,
            "digest": self.digest,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class KnowledgeCandidateProposalV1:
    """Provider-neutral candidate content; no workspace or review authority."""

    operation: Literal["create_page", "revise_page"]
    target_page_id: UUID | None
    expected_revision_id: UUID | None
    expected_revision_hash: str | None
    title: str
    slug: str
    summary: str
    tags: tuple[str, ...]
    sections: tuple[KnowledgeCandidateSectionV1, ...]
    source_suggestions: tuple[KnowledgeCandidateSourceSuggestionV1, ...]
    artifact_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    relations: tuple[KnowledgeCandidateRelationV1, ...] = ()
    supersedes_candidate_id: UUID | None = None
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise ValueError("unsupported candidate proposal schema_version")
        if self.operation not in {"create_page", "revise_page"}:
            raise ValueError("unsupported candidate operation")
        for field_name in (
            "target_page_id",
            "expected_revision_id",
            "supersedes_candidate_id",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, UUID):
                raise TypeError(f"{field_name} must be a UUID")
        if self.operation == "create_page":
            if any(
                value is not None
                for value in (self.target_page_id, self.expected_revision_id, self.expected_revision_hash)
            ):
                raise ValueError("create_page cannot contain target_page_id or target revision")
        else:
            if self.target_page_id is None:
                raise ValueError("revise_page requires target_page_id")
            if self.expected_revision_id is None:
                raise ValueError("revise_page requires expected_revision_id")
            if self.expected_revision_hash is None:
                raise ValueError("revise_page requires expected_revision_hash")
            object.__setattr__(
                self,
                "expected_revision_hash",
                _hash(self.expected_revision_hash, "expected_revision_hash"),
            )
        object.__setattr__(self, "title", _text(self.title, "title"))
        object.__setattr__(self, "slug", _text(self.slug, "slug"))
        if any(char.isspace() for char in self.slug) or "/" in self.slug:
            raise ValueError("slug must be a path segment")
        object.__setattr__(self, "summary", _text(self.summary, "summary", allow_empty=True))
        object.__setattr__(
            self,
            "tags",
            tuple(dict.fromkeys(_text(item, "tag") for item in self.tags)),
        )
        sections = tuple(self.sections)
        if not sections:
            raise ValueError("candidate proposal requires sections")
        if any(not isinstance(item, KnowledgeCandidateSectionV1) for item in sections):
            raise TypeError("candidate sections must be typed")
        if len({item.section_key for item in sections}) != len(sections):
            raise ValueError("candidate section keys must be unique")
        if len({item.ordinal for item in sections}) != len(sections):
            raise ValueError("candidate section ordinals must be unique")
        object.__setattr__(self, "sections", tuple(sorted(sections, key=lambda item: item.ordinal)))
        relations = tuple(self.relations)
        if any(not isinstance(item, KnowledgeCandidateRelationV1) for item in relations):
            raise TypeError("candidate relations must be typed")
        if len({item.relation_key for item in relations}) != len(relations):
            raise ValueError("candidate relation keys must be unique")
        object.__setattr__(self, "relations", relations)
        sources = tuple(self.source_suggestions)
        if any(not isinstance(item, KnowledgeCandidateSourceSuggestionV1) for item in sources):
            raise TypeError("candidate source suggestions must be typed")
        object.__setattr__(self, "source_suggestions", sources)
        for field_name in ("artifact_refs", "evidence_refs"):
            values = tuple(_text(item, field_name) for item in getattr(self, field_name))
            if len(set(values)) != len(values):
                raise ValueError(f"{field_name} must be unique")
            object.__setattr__(self, field_name, values)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operation": self.operation,
            "target_page_id": str(self.target_page_id) if self.target_page_id else None,
            "expected_revision_id": (
                str(self.expected_revision_id) if self.expected_revision_id else None
            ),
            "expected_revision_hash": self.expected_revision_hash,
            "title": self.title,
            "slug": self.slug,
            "summary": self.summary,
            "tags": list(self.tags),
            "sections": [item.to_mapping() for item in self.sections],
            "relations": [item.to_mapping() for item in self.relations],
            "source_suggestions": [item.to_mapping() for item in self.source_suggestions],
            "artifact_refs": list(self.artifact_refs),
            "evidence_refs": list(self.evidence_refs),
            "supersedes_candidate_id": (
                str(self.supersedes_candidate_id)
                if self.supersedes_candidate_id is not None
                else None
            ),
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_mapping())

    @property
    def proposal_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> KnowledgeCandidateProposalV1:
        allowed = {
            "schema_version",
            "operation",
            "target_page_id",
            "expected_revision_id",
            "expected_revision_hash",
            "title",
            "slug",
            "summary",
            "tags",
            "sections",
            "relations",
            "source_suggestions",
            "artifact_refs",
            "evidence_refs",
            "supersedes_candidate_id",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError("candidate proposal contains unknown fields")
        raw_sections = value.get("sections")
        if not isinstance(raw_sections, list):
            raise TypeError("candidate proposal sections must be a list")
        section_items = _mapping_list(raw_sections, "sections")
        sections_list: list[KnowledgeCandidateSectionV1] = []
        for index, item in enumerate(section_items):
            raw_ordinal = item.get("ordinal", index)
            if not isinstance(raw_ordinal, int) or isinstance(raw_ordinal, bool):
                raise TypeError("section ordinal must be an integer")
            sections_list.append(
                KnowledgeCandidateSectionV1(
                    section_key=_text(item.get("section_key"), "section_key"),
                    heading=_text(item.get("heading"), "heading"),
                    summary=_text(item.get("summary", ""), "summary", allow_empty=True),
                    body_markdown=_text(item.get("body_markdown"), "body_markdown"),
                    ordinal=raw_ordinal,
                )
            )
        sections = tuple(sections_list)
        relations = tuple(
            KnowledgeCandidateRelationV1(
                relation_key=_text(item.get("relation_key"), "relation_key"),
                relation_kind=_text(item.get("relation_kind"), "relation_kind"),
                target_page_id=cast(
                    UUID, _uuid(item.get("target_page_id"), "target_page_id")
                ),
                target_revision_id=_uuid(
                    item.get("target_revision_id"), "target_revision_id", required=False
                ),
            )
            for item in _mapping_list(value.get("relations", []), "relations")
        )
        sources = tuple(
            KnowledgeCandidateSourceSuggestionV1(
                source_type=_text(item.get("source_type"), "source_type"),
                label=_text(item.get("label"), "source label"),
                source_id=_uuid(item.get("source_id"), "source_id", required=False),
                uri=cast(str | None, item.get("uri")),
                workspace_relative_path=cast(str | None, item.get("workspace_relative_path")),
                git_revision=cast(str | None, item.get("git_revision")),
                digest=cast(str | None, item.get("digest")),
                metadata=cast(Mapping[str, object], item.get("metadata", {})),
            )
            for item in _mapping_list(value.get("source_suggestions", []), "source_suggestions")
        )
        tags = value.get("tags", [])
        artifact_refs = value.get("artifact_refs", [])
        evidence_refs = value.get("evidence_refs", [])
        if not isinstance(tags, list) or not isinstance(artifact_refs, list) or not isinstance(evidence_refs, list):
            raise TypeError("candidate proposal lists are invalid")
        return cls(
            operation=cast(Literal["create_page", "revise_page"], value.get("operation")),
            target_page_id=_uuid(value.get("target_page_id"), "target_page_id", required=False),
            expected_revision_id=_uuid(
                value.get("expected_revision_id"), "expected_revision_id", required=False
            ),
            expected_revision_hash=cast(str | None, value.get("expected_revision_hash")),
            title=_text(value.get("title"), "title"),
            slug=_text(value.get("slug"), "slug"),
            summary=_text(value.get("summary", ""), "summary", allow_empty=True),
            tags=tuple(cast(str, item) for item in tags),
            sections=sections,
            relations=relations,
            source_suggestions=sources,
            artifact_refs=tuple(cast(str, item) for item in artifact_refs),
            evidence_refs=tuple(cast(str, item) for item in evidence_refs),
            supersedes_candidate_id=_uuid(
                value.get("supersedes_candidate_id"),
                "supersedes_candidate_id",
                required=False,
            ),
            schema_version=cast(Literal[1], value.get("schema_version", 1)),
        )


def _mapping_list(value: object, field_name: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list")
    result: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError(f"{field_name} items must be objects")
        result.append(item)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CandidateSpoolEntry:
    content_ref: str
    proposal_hash: str
    content_bytes: int


class CandidateProposalSpool(Protocol):
    def put(self, proposal: KnowledgeCandidateProposalV1) -> CandidateSpoolEntry:
        raise NotImplementedError

    def read(
        self, content_ref: str, *, expected_hash: str
    ) -> KnowledgeCandidateProposalV1:
        raise NotImplementedError

    def delete(self, content_ref: str) -> None:
        raise NotImplementedError

    def quarantine(self, content_ref: str, *, now: datetime) -> None:
        raise NotImplementedError

    def gc_quarantine(self, *, now: datetime, retention: timedelta) -> int:
        raise NotImplementedError


class FileCandidateProposalSpool:
    def __init__(
        self,
        root: Path,
        *,
        quota_bytes: int,
        max_proposal_bytes: int = MAX_CANDIDATE_PROPOSAL_BYTES,
    ) -> None:
        if quota_bytes <= 0 or max_proposal_bytes <= 0:
            raise ValueError("candidate spool limits must be positive")
        self._root = root.expanduser()
        self._quota_bytes = quota_bytes
        self._max_proposal_bytes = min(max_proposal_bytes, MAX_CANDIDATE_PROPOSAL_BYTES)
        self._quarantine_root = self._root / "quarantine"
        self._lock = threading.RLock()
        self._ensure_root()

    @property
    def root(self) -> Path:
        return self._root

    def put(self, proposal: KnowledgeCandidateProposalV1) -> CandidateSpoolEntry:
        content = proposal.canonical_bytes()
        if len(content) > self._max_proposal_bytes:
            raise CandidateProposalSpoolError("candidate proposal exceeds size limit")
        with self._lock:
            self._ensure_root()
            if self._usage_bytes() + len(content) > self._quota_bytes:
                raise CandidateProposalSpoolError("candidate proposal spool quota exceeded")
            filename = f"{uuid4()}-{proposal.proposal_hash}.json"
            destination = self._root / filename
            fd, temporary_name = tempfile.mkstemp(prefix=".candidate-", dir=self._root)
            temporary = Path(temporary_name)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                os.chmod(destination, 0o600)
                return CandidateSpoolEntry(filename, proposal.proposal_hash, len(content))
            except Exception as exc:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise CandidateProposalSpoolError("candidate proposal spool write failed") from exc

    def read(
        self, content_ref: str, *, expected_hash: str
    ) -> KnowledgeCandidateProposalV1:
        path = self.path_for(content_ref)
        if not path.exists() or path.is_symlink():
            raise CandidateProposalSpoolError("candidate proposal spool file not found")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise CandidateProposalSpoolError("candidate proposal spool read failed") from exc
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise CandidateProposalSpoolError("candidate proposal spool hash mismatch")
        if len(content) > self._max_proposal_bytes:
            raise CandidateProposalSpoolError("candidate proposal exceeds size limit")
        try:
            value = json.loads(content.decode("utf-8"))
            if not isinstance(value, Mapping):
                raise TypeError("proposal root must be an object")
            proposal = KnowledgeCandidateProposalV1.from_mapping(value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CandidateProposalSpoolError("candidate proposal spool content is invalid") from exc
        if proposal.proposal_hash != expected_hash:
            raise CandidateProposalSpoolError("candidate proposal spool hash mismatch")
        return proposal

    def delete(self, content_ref: str) -> None:
        path = self.path_for(content_ref)
        if path.is_symlink():
            raise CandidateProposalSpoolError("candidate proposal spool path is a symlink")
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise CandidateProposalSpoolError("candidate proposal spool delete failed") from exc

    def quarantine(self, content_ref: str, *, now: datetime) -> None:
        source = self.path_for(content_ref)
        with self._lock:
            self._ensure_root()
            if source.is_symlink():
                raise CandidateProposalSpoolError("candidate proposal spool path is a symlink")
            if not source.exists():
                return
            destination = self._quarantine_root / source.name
            try:
                os.replace(source, destination)
                os.chmod(destination, 0o600)
                timestamp = now.timestamp()
                os.utime(destination, (timestamp, timestamp))
            except OSError as exc:
                raise CandidateProposalSpoolError(
                    "candidate proposal quarantine move failed"
                ) from exc

    def gc_quarantine(self, *, now: datetime, retention: timedelta) -> int:
        if retention.total_seconds() <= 0:
            raise ValueError("candidate quarantine retention must be positive")
        with self._lock:
            self._ensure_root()
            cutoff = now.timestamp() - retention.total_seconds()
            removed = 0
            try:
                for path in self._quarantine_root.iterdir():
                    if path.is_symlink() or not path.is_file():
                        continue
                    if path.stat().st_mtime <= cutoff:
                        path.unlink()
                        removed += 1
            except OSError as exc:
                raise CandidateProposalSpoolError(
                    "candidate quarantine scan failed"
                ) from exc
            return removed

    def path_for(self, content_ref: str) -> Path:
        if not isinstance(content_ref, str) or not _FILENAME.fullmatch(content_ref):
            raise CandidateProposalSpoolError("candidate proposal spool path is invalid")
        path = self._root / content_ref
        if path.parent != self._root:
            raise CandidateProposalSpoolError("candidate proposal spool path escapes root")
        return path

    def usage_bytes(self) -> int:
        with self._lock:
            return self._usage_bytes()

    def _usage_bytes(self) -> int:
        try:
            return sum(
                path.stat().st_size
                for path in self._root.iterdir()
                if path.is_file() and not path.is_symlink() and path.suffix == ".json"
            )
        except OSError as exc:
            raise CandidateProposalSpoolError("candidate proposal spool scan failed") from exc

    def _ensure_root(self) -> None:
        if self._root.exists() and self._root.is_symlink():
            raise CandidateProposalSpoolError("candidate proposal spool root is a symlink")
        try:
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self._root, 0o700)
            if self._quarantine_root.exists() and self._quarantine_root.is_symlink():
                raise CandidateProposalSpoolError("candidate quarantine root is a symlink")
            self._quarantine_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self._quarantine_root, 0o700)
        except OSError as exc:
            raise CandidateProposalSpoolError("candidate proposal spool root is unavailable") from exc


class KnowledgeCandidateSubmissionStatus(StrEnum):
    WAITING_FOR_TASK = "waiting_for_task"
    QUEUED = "queued"
    SUBMITTING = "submitting"
    RETRY_WAIT = "retry_wait"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    DISCARDED = "discarded"


@dataclass(slots=True)
class KnowledgeCandidateSubmission:
    id: UUID
    workspace_id: UUID
    producer_type: str
    producer_id: UUID
    planning_run_id: UUID | None
    execution_id: UUID | None
    worker_item_id: UUID | None
    task_id: UUID | None
    subtask_task_id: UUID | None
    artifact_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    proposal_hash: str
    content_ref: str
    content_bytes: int
    knowledge_command_id: UUID | None
    status: KnowledgeCandidateSubmissionStatus
    attempt: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    next_attempt_at: datetime | None
    terminal_candidate_id: UUID | None
    error_code: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        submission_id: UUID,
        workspace_id: UUID,
        producer_type: str,
        producer_id: UUID,
        proposal: KnowledgeCandidateProposalV1,
        content_ref: str,
        content_bytes: int,
        status: KnowledgeCandidateSubmissionStatus,
        planning_run_id: UUID | None = None,
        execution_id: UUID | None = None,
        worker_item_id: UUID | None = None,
        task_id: UUID | None = None,
        subtask_task_id: UUID | None = None,
        now: datetime,
    ) -> KnowledgeCandidateSubmission:
        return cls(
            id=submission_id,
            workspace_id=workspace_id,
            producer_type=_text(producer_type, "producer_type"),
            producer_id=producer_id,
            planning_run_id=planning_run_id,
            execution_id=execution_id,
            worker_item_id=worker_item_id,
            task_id=task_id,
            subtask_task_id=subtask_task_id,
            artifact_refs=tuple(proposal.artifact_refs),
            evidence_refs=tuple(proposal.evidence_refs),
            proposal_hash=proposal.proposal_hash,
            content_ref=_text(content_ref, "content_ref"),
            content_bytes=content_bytes,
            knowledge_command_id=None,
            status=status,
            attempt=1,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=None,
            terminal_candidate_id=None,
            error_code=None,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or not isinstance(self.workspace_id, UUID):
            raise TypeError("submission IDs must be UUIDs")
        if not isinstance(self.producer_id, UUID):
            raise TypeError("submission producer_id must be a UUID")
        object.__setattr__(self, "producer_type", _text(self.producer_type, "producer_type"))
        object.__setattr__(self, "proposal_hash", _hash(self.proposal_hash, "proposal_hash"))
        object.__setattr__(self, "content_ref", _text(self.content_ref, "content_ref"))
        if self.content_bytes < 0:
            raise ValueError("submission content_bytes must be non-negative")
        if self.attempt < 1 or self.version < 1:
            raise ValueError("submission attempt and version must be positive")
        try:
            object.__setattr__(self, "status", KnowledgeCandidateSubmissionStatus(self.status))
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported knowledge candidate submission status") from exc

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            KnowledgeCandidateSubmissionStatus.SUBMITTED,
            KnowledgeCandidateSubmissionStatus.REJECTED,
            KnowledgeCandidateSubmissionStatus.DISCARDED,
        }

    def queue(self, *, now: datetime) -> None:
        if self.is_terminal:
            raise ValueError("terminal submission cannot be queued")
        self.status = KnowledgeCandidateSubmissionStatus.QUEUED
        self.lease_owner = None
        self.lease_expires_at = None
        self.next_attempt_at = now
        self.error_code = None
        self.version += 1
        self.updated_at = now

    def retry(
        self,
        *,
        now: datetime,
        next_attempt_at: datetime,
        error_code: str,
        command_id: UUID | None = None,
    ) -> None:
        if self.is_terminal:
            raise ValueError("terminal submission cannot be retried")
        self.status = KnowledgeCandidateSubmissionStatus.RETRY_WAIT
        self.lease_owner = None
        self.lease_expires_at = None
        self.next_attempt_at = next_attempt_at
        if command_id is not None:
            self.knowledge_command_id = command_id
        self.error_code = _text(error_code, "error_code")
        self.version += 1
        self.updated_at = now

    def mark_submitted(
        self, *, now: datetime, command_id: UUID, candidate_id: UUID
    ) -> None:
        if self.is_terminal and self.status is not KnowledgeCandidateSubmissionStatus.SUBMITTED:
            raise ValueError("rejected or discarded submission cannot be submitted")
        self.status = KnowledgeCandidateSubmissionStatus.SUBMITTED
        self.knowledge_command_id = command_id
        self.terminal_candidate_id = candidate_id
        self.lease_owner = None
        self.lease_expires_at = None
        self.next_attempt_at = None
        self.error_code = None
        self.version += 1
        self.updated_at = now

    def mark_rejected(
        self, *, now: datetime, command_id: UUID, error_code: str
    ) -> None:
        if self.status is KnowledgeCandidateSubmissionStatus.SUBMITTED:
            raise ValueError("submitted submission cannot be rejected")
        self.status = KnowledgeCandidateSubmissionStatus.REJECTED
        self.knowledge_command_id = command_id
        self.lease_owner = None
        self.lease_expires_at = None
        self.next_attempt_at = None
        self.error_code = _text(error_code, "error_code")
        self.version += 1
        self.updated_at = now

    def discard(self, *, now: datetime, error_code: str | None = None) -> None:
        if self.status is KnowledgeCandidateSubmissionStatus.SUBMITTED:
            raise ValueError("submitted submission cannot be discarded")
        self.status = KnowledgeCandidateSubmissionStatus.DISCARDED
        self.lease_owner = None
        self.lease_expires_at = None
        self.next_attempt_at = None
        self.error_code = error_code
        self.version += 1
        self.updated_at = now


class KnowledgeCandidateSubmissionService:
    """Queue candidate content without making it part of a task outcome."""

    def __init__(self, spool: CandidateProposalSpool) -> None:
        self._spool = spool

    def queue_in_uow(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        workspace_id: UUID,
        producer_type: str,
        producer_id: UUID,
        proposal: KnowledgeCandidateProposalV1,
        status: KnowledgeCandidateSubmissionStatus,
        now: datetime,
        correlation_id: UUID,
        planning_run_id: UUID | None = None,
        execution_id: UUID | None = None,
        worker_item_id: UUID | None = None,
        task_id: UUID | None = None,
        subtask_task_id: UUID | None = None,
        actor_id: str = "candidate-submission",
        causation_id: UUID | None = None,
        actor_type: str = "system",
    ) -> KnowledgeCandidateSubmission:
        entry = self._spool.put(proposal)
        existing = uow.knowledge_candidate_submissions.find_by_producer_hash(
            workspace_id,
            producer_type,
            producer_id,
            proposal.proposal_hash,
            for_update=True,
        )
        if existing is not None:
            self._spool.delete(entry.content_ref)
            return existing
        submission = KnowledgeCandidateSubmission.create(
            submission_id=uuid4(),
            workspace_id=workspace_id,
            producer_type=producer_type,
            producer_id=producer_id,
            proposal=proposal,
            content_ref=entry.content_ref,
            content_bytes=entry.content_bytes,
            status=status,
            planning_run_id=planning_run_id,
            execution_id=execution_id,
            worker_item_id=worker_item_id,
            task_id=task_id,
            subtask_task_id=subtask_task_id,
            now=now,
        )
        uow.knowledge_candidate_submissions.add(submission)
        self.record_event(
            uow,
            submission=submission,
            event_type="KnowledgeCandidateSubmissionQueued",
            actor_id=actor_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            actor_type=actor_type,
        )
        return submission

    def activate_planner_submissions(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        workspace_id: UUID,
        planning_run_id: UUID,
        root_task_id: UUID,
        now: datetime,
        correlation_id: UUID,
        actor_id: str = "candidate-submission",
        causation_id: UUID | None = None,
        actor_type: str = "system",
    ) -> tuple[KnowledgeCandidateSubmission, ...]:
        activated: list[KnowledgeCandidateSubmission] = []
        for submission in uow.knowledge_candidate_submissions.list_for_producer(
            workspace_id, "planner", planning_run_id
        ):
            if submission.status is not KnowledgeCandidateSubmissionStatus.WAITING_FOR_TASK:
                continue
            previous_version = submission.version
            submission.task_id = root_task_id
            submission.queue(now=now)
            uow.knowledge_candidate_submissions.save(submission, previous_version)
            self.record_event(
                uow,
                submission=submission,
                event_type="KnowledgeCandidateSubmissionUpdated",
                actor_id=actor_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                actor_type=actor_type,
            )
            activated.append(submission)
        return tuple(activated)

    def discard_planner_submissions(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        workspace_id: UUID,
        planning_run_id: UUID,
        now: datetime,
        correlation_id: UUID,
        error_code: str = "planning_proposal_discarded",
        actor_id: str = "candidate-submission",
        causation_id: UUID | None = None,
        actor_type: str = "system",
    ) -> tuple[KnowledgeCandidateSubmission, ...]:
        discarded: list[KnowledgeCandidateSubmission] = []
        for submission in uow.knowledge_candidate_submissions.list_for_producer(
            workspace_id, "planner", planning_run_id
        ):
            if submission.is_terminal:
                continue
            previous_version = submission.version
            submission.discard(now=now, error_code=error_code)
            uow.knowledge_candidate_submissions.save(submission, previous_version)
            self.record_event(
                uow,
                submission=submission,
                event_type="KnowledgeCandidateSubmissionUpdated",
                actor_id=actor_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                actor_type=actor_type,
            )
            try:
                self._spool.delete(submission.content_ref)
            except CandidateProposalSpoolError:
                logger.warning(
                    "planner candidate spool cleanup failed",
                    extra={"submission_id": str(submission.id)},
                )
            discarded.append(submission)
        return tuple(discarded)

    @staticmethod
    def record_event(
        uow: ControlPlaneUnitOfWork,
        *,
        submission: KnowledgeCandidateSubmission,
        event_type: str,
        actor_id: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
        actor_type: str = "system",
    ) -> None:
        # Keep the event helper lazy: command_service imports the port bundle,
        # which imports this candidate module.
        from sidera_control_plane.application.command_service import hash_request

        payload = {
            "submission_id": str(submission.id),
            "producer_type": submission.producer_type,
            "producer_id": str(submission.producer_id),
            "planning_run_id": (
                str(submission.planning_run_id)
                if submission.planning_run_id is not None
                else None
            ),
            "execution_id": (
                str(submission.execution_id) if submission.execution_id is not None else None
            ),
            "worker_item_id": (
                str(submission.worker_item_id)
                if submission.worker_item_id is not None
                else None
            ),
            "task_id": str(submission.task_id) if submission.task_id is not None else None,
            "subtask_task_id": (
                str(submission.subtask_task_id)
                if submission.subtask_task_id is not None
                else None
            ),
            "proposal_hash": submission.proposal_hash,
            "status": submission.status.value,
            "attempt": submission.attempt,
            "terminal_candidate_id": (
                str(submission.terminal_candidate_id)
                if submission.terminal_candidate_id is not None
                else None
            ),
            "error_code": submission.error_code,
        }
        command: CommandEnvelope | None = None
        if causation_id is None:
            command_id = uuid4()
            command = CommandEnvelope(
                id=command_id,
                workspace_id=submission.workspace_id,
                command_type="KnowledgeCandidateSubmissionUpdate",
                target_type="knowledge_candidate_submission",
                actor_type=actor_type,
                actor_id=actor_id,
                target_id=submission.id,
                payload=payload,
                expected_version=None,
                idempotency_key=f"candidate-submission-event:{submission.id}:{submission.version}:{event_type}",
                correlation_id=correlation_id,
            )
        else:
            command_id = causation_id
        event = EventData(
            id=uuid4(),
            workspace_id=submission.workspace_id,
            aggregate_type="knowledge_candidate_submission",
            aggregate_id=submission.id,
            aggregate_version=submission.version,
            event_type=event_type,
            payload=payload,
            actor_type=actor_type,
            actor_id=actor_id,
            causation_id=command_id,
            correlation_id=correlation_id,
        )
        uow.events.append_with_outbox(event)
        if command is not None:
            uow.commands.record_applied(
                command,
                hash_request(command),
                CommandResult(
                    command_id=command.id,
                    aggregate_id=submission.id,
                    aggregate_version=submission.version,
                    event_ids=(event.id,),
                    data={"submission_id": str(submission.id)},
                ),
            )
