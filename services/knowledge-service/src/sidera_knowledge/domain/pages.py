from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from sidera_knowledge.domain.common import (
    DomainValidationError,
    canonical_json_bytes,
    normalize_markdown,
    normalize_tags,
    require_text,
    sha256_hex,
    validate_sha256,
    validate_workspace_relative_posix_path,
)

PageStatus = Literal["active", "archived"]
SourceType = Literal[
    "manual",
    "markdown_file",
    "task",
    "artifact",
    "evidence",
    "external_url",
]
SourceAvailability = Literal["available", "unavailable"]
RelationKind = Literal[
    "relates_to",
    "depends_on",
    "supersedes",
    "derived_from",
    "implements",
]


def _aware_datetime(value: datetime | None, field_name: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise DomainValidationError(f"{field_name} must be timezone aware")
    return value


@dataclass(frozen=True)
class KnowledgeSectionSnapshot:
    section_key: str
    heading: str
    summary: str
    body_markdown: str
    ordinal: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "section_key", require_text(self.section_key, "section_key"))
        object.__setattr__(self, "heading", require_text(self.heading, "heading"))
        object.__setattr__(self, "summary", self.summary.strip())
        object.__setattr__(self, "body_markdown", normalize_markdown(self.body_markdown))
        if self.ordinal < 0:
            raise DomainValidationError("section ordinal must be nonnegative")

    @property
    def utf8_byte_count(self) -> int:
        return len(self.body_markdown.encode("utf-8"))

    @property
    def snapshot(self) -> dict[str, Any]:
        return {
            "section_key": self.section_key,
            "heading": self.heading,
            "summary": self.summary,
            "body_markdown": self.body_markdown,
            "ordinal": self.ordinal,
        }

    @property
    def section_hash(self) -> str:
        return sha256_hex(canonical_json_bytes(self.snapshot))


@dataclass(frozen=True)
class KnowledgeRelationSnapshot:
    relation_key: str
    relation_kind: RelationKind
    target_page_id: UUID
    target_revision_id: UUID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "relation_key", require_text(self.relation_key, "relation_key"))
        if self.relation_kind not in {
            "relates_to",
            "depends_on",
            "supersedes",
            "derived_from",
            "implements",
        }:
            raise DomainValidationError("unsupported relation kind")

    @property
    def snapshot(self) -> dict[str, Any]:
        return {
            "relation_key": self.relation_key,
            "relation_kind": self.relation_kind,
            "target_page_id": str(self.target_page_id),
            "target_revision_id": (
                str(self.target_revision_id) if self.target_revision_id else None
            ),
        }


@dataclass(frozen=True)
class KnowledgeSource:
    source_id: UUID
    workspace_id: UUID
    source_type: SourceType
    label: str
    uri: str | None = None
    workspace_relative_path: str | None = None
    git_revision: str | None = None
    digest: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    availability: SourceAvailability = "available"
    safe_reason_code: str | None = None
    last_checked_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", require_text(self.label, "label"))
        if self.source_type not in {
            "manual",
            "markdown_file",
            "task",
            "artifact",
            "evidence",
            "external_url",
        }:
            raise DomainValidationError("unsupported source type")
        if self.workspace_relative_path is not None:
            object.__setattr__(
                self,
                "workspace_relative_path",
                validate_workspace_relative_posix_path(self.workspace_relative_path),
            )
        if self.source_type == "external_url":
            if not self.uri:
                raise DomainValidationError("external_url source requires uri")
            parsed = urlparse(self.uri)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise DomainValidationError("external_url source requires http(s) uri")
        if self.digest is not None:
            object.__setattr__(self, "digest", validate_sha256(self.digest, "digest"))
        if self.availability not in {"available", "unavailable"}:
            raise DomainValidationError("unsupported source availability")
        if self.availability == "unavailable" and not self.safe_reason_code:
            raise DomainValidationError("unavailable source requires safe reason code")
        _aware_datetime(self.last_checked_at, "last_checked_at")
        if self.version < 1:
            raise DomainValidationError("source version must be positive")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def mark_unavailable(
        self, reason_code: str, checked_at: datetime | None = None
    ) -> KnowledgeSource:
        return KnowledgeSource(
            source_id=self.source_id,
            workspace_id=self.workspace_id,
            source_type=self.source_type,
            label=self.label,
            uri=self.uri,
            workspace_relative_path=self.workspace_relative_path,
            git_revision=self.git_revision,
            digest=self.digest,
            metadata=self.metadata,
            availability="unavailable",
            safe_reason_code=require_text(reason_code, "safe_reason_code"),
            last_checked_at=checked_at,
            version=self.version + 1,
        )


@dataclass(frozen=True)
class KnowledgeRevision:
    revision_id: UUID
    page_id: UUID
    workspace_id: UUID
    revision_number: int
    title: str
    summary: str
    tags: tuple[str, ...]
    sections: tuple[KnowledgeSectionSnapshot, ...]
    relations: tuple[KnowledgeRelationSnapshot, ...]
    source_refs: tuple[UUID, ...]
    change_reason: str
    created_by: str
    created_at: datetime
    accepted_candidate_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.revision_number < 1:
            raise DomainValidationError("revision number must start at one")
        object.__setattr__(self, "title", require_text(self.title, "title"))
        object.__setattr__(self, "summary", self.summary.strip())
        object.__setattr__(self, "tags", normalize_tags(self.tags))
        sections = tuple(sorted(self.sections, key=lambda section: section.ordinal))
        keys = [section.section_key for section in sections]
        if len(keys) != len(set(keys)):
            raise DomainValidationError("section_key must be unique in a revision")
        object.__setattr__(self, "sections", sections)
        relations = tuple(sorted(self.relations, key=lambda relation: relation.relation_key))
        relation_keys = [relation.relation_key for relation in relations]
        if len(relation_keys) != len(set(relation_keys)):
            raise DomainValidationError("relation_key must be unique in a revision")
        object.__setattr__(self, "relations", relations)
        source_refs = tuple(sorted(self.source_refs, key=str))
        if len(source_refs) != len(set(source_refs)):
            raise DomainValidationError("source refs must be unique in a revision")
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "change_reason", require_text(self.change_reason, "change_reason"))
        object.__setattr__(self, "created_by", require_text(self.created_by, "created_by"))
        _aware_datetime(self.created_at, "created_at")

    @property
    def snapshot(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "tags": list(self.tags),
            "sections": [section.snapshot for section in self.sections],
            "relations": [relation.snapshot for relation in self.relations],
            "source_refs": [str(source_id) for source_id in self.source_refs],
        }

    @property
    def content_hash(self) -> str:
        return sha256_hex(canonical_json_bytes(self.snapshot))


@dataclass(frozen=True)
class KnowledgePage:
    page_id: UUID
    workspace_id: UUID
    canonical_title: str
    slug: str
    status: PageStatus = "active"
    current_revision_id: UUID | None = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "canonical_title", require_text(self.canonical_title, "canonical_title")
        )
        slug = require_text(self.slug, "slug")
        if any(char.isspace() for char in slug) or "/" in slug:
            raise DomainValidationError("slug must be a workspace-local path segment")
        object.__setattr__(self, "slug", slug)
        if self.status not in {"active", "archived"}:
            raise DomainValidationError("unsupported page status")
        if self.version < 1:
            raise DomainValidationError("page version must be positive")
