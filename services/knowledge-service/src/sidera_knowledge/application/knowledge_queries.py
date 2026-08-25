from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from difflib import unified_diff
from typing import Any, Protocol
from uuid import UUID

from sidera_knowledge.domain.common import DomainValidationError

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


class QueryValidationError(DomainValidationError):
    """Raised when a read query contains an invalid cursor or limit."""


class RevisionDiffError(DomainValidationError):
    """Raised when two revisions cannot be compared safely."""


class KnowledgeMaterializationError(DomainValidationError):
    """Raised when an exact knowledge section reference cannot be materialized."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


MATERIALIZATION_MAX_SECTIONS = 32
MATERIALIZATION_MAX_SECTION_BYTES = 128 * 1024
MATERIALIZATION_MAX_TOTAL_BYTES = 256 * 1024


@dataclass(frozen=True)
class KnowledgeSectionRef:
    ordinal: int
    page_id: UUID
    revision_id: UUID
    revision_hash: str
    section_key: str
    section_hash: str
    source_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise KnowledgeMaterializationError(
                "source_invalid", "section ordinal must be nonnegative"
            )
        object.__setattr__(self, "source_ids", tuple(self.source_ids))


@dataclass(frozen=True)
class SectionView:
    section_key: str
    heading: str
    summary: str
    body_markdown: str
    ordinal: int
    section_hash: str
    content_byte_count: int


@dataclass(frozen=True)
class RelationView:
    relation_key: str
    relation_kind: str
    target_page_id: UUID
    target_revision_id: UUID | None

    @property
    def snapshot(self) -> dict[str, object]:
        return {
            "relation_key": self.relation_key,
            "relation_kind": self.relation_kind,
            "target_page_id": str(self.target_page_id),
            "target_revision_id": (
                str(self.target_revision_id) if self.target_revision_id else None
            ),
        }


@dataclass(frozen=True)
class SourceView:
    source_id: UUID
    workspace_id: UUID
    source_type: str
    label: str
    uri: str | None
    workspace_relative_path: str | None
    git_revision: str | None
    digest: str | None
    metadata: dict[str, Any]
    availability: str
    safe_reason_code: str | None
    last_checked_at: datetime | None
    version: int


@dataclass(frozen=True)
class MaterializedKnowledgeSection:
    ordinal: int
    page_id: UUID
    revision_id: UUID
    revision_hash: str
    section_key: str
    section_hash: str
    body_markdown: str
    sources: tuple[SourceView, ...]
    trust: str
    section_byte_count: int


@dataclass(frozen=True)
class KnowledgeMaterialization:
    sections: tuple[MaterializedKnowledgeSection, ...]
    total_byte_count: int


# Short aliases keep the exact-ref contract easy to discover for adapters.
ExactSectionRef = KnowledgeSectionRef
MaterializedSection = MaterializedKnowledgeSection
MaterializationResult = KnowledgeMaterialization


@dataclass(frozen=True)
class RevisionSummary:
    revision_id: UUID
    page_id: UUID
    workspace_id: UUID
    revision_number: int
    title: str
    summary: str
    tags: tuple[str, ...]
    content_hash: str
    change_reason: str
    created_by: str
    created_at: datetime
    accepted_candidate_id: UUID | None


@dataclass(frozen=True)
class RevisionDetail:
    revision_id: UUID
    page_id: UUID
    workspace_id: UUID
    revision_number: int
    title: str
    summary: str
    tags: tuple[str, ...]
    content_hash: str
    change_reason: str
    created_by: str
    created_at: datetime
    accepted_candidate_id: UUID | None
    sections: tuple[SectionView, ...]
    relations: tuple[RelationView, ...]
    source_refs: tuple[UUID, ...]
    sources: tuple[SourceView, ...]

    def to_summary(self) -> RevisionSummary:
        return RevisionSummary(
            revision_id=self.revision_id,
            page_id=self.page_id,
            workspace_id=self.workspace_id,
            revision_number=self.revision_number,
            title=self.title,
            summary=self.summary,
            tags=self.tags,
            content_hash=self.content_hash,
            change_reason=self.change_reason,
            created_by=self.created_by,
            created_at=self.created_at,
            accepted_candidate_id=self.accepted_candidate_id,
        )


@dataclass(frozen=True)
class PageSummary:
    page_id: UUID
    workspace_id: UUID
    canonical_title: str
    slug: str
    status: str
    version: int
    current_revision: RevisionSummary | None
    updated_at: datetime


@dataclass(frozen=True)
class PageDetail:
    summary: PageSummary
    current_revision: RevisionDetail | None
    revision_history: tuple[RevisionSummary, ...]


@dataclass(frozen=True)
class CandidateSummary:
    candidate_id: UUID
    workspace_id: UUID
    operation: str
    target_page_id: UUID | None
    expected_revision_id: UUID | None
    expected_revision_hash: str | None
    proposed_title: str
    proposed_slug: str
    candidate_hash: str
    status: str
    producer_type: str
    producer_id: str
    task_id: UUID | None
    subtask_id: UUID | None
    worker_item_id: UUID | None
    execution_id: UUID | None
    supersedes_candidate_id: UUID | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SectionDiff:
    section_key: str
    before_hash: str
    after_hash: str


@dataclass(frozen=True)
class RevisionDiff:
    from_revision_id: UUID
    to_revision_id: UUID
    title_changed: bool
    summary_changed: bool
    tags_added: tuple[str, ...]
    tags_removed: tuple[str, ...]
    sections_added: tuple[str, ...]
    sections_removed: tuple[str, ...]
    sections_changed: tuple[SectionDiff, ...]
    relations_added: tuple[str, ...]
    relations_removed: tuple[str, ...]
    relations_changed: tuple[str, ...]
    sources_added: tuple[UUID, ...]
    sources_removed: tuple[UUID, ...]
    body_unified_diff: str
    truncated: bool

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.title_changed,
                self.summary_changed,
                self.tags_added,
                self.tags_removed,
                self.sections_added,
                self.sections_removed,
                self.sections_changed,
                self.relations_added,
                self.relations_removed,
                self.relations_changed,
                self.sources_added,
                self.sources_removed,
                self.body_unified_diff,
            )
        )


@dataclass(frozen=True)
class CandidateDetail:
    summary: CandidateSummary
    proposed_snapshot: dict[str, Any]
    provenance: dict[str, object]
    sources: tuple[SourceView, ...]
    observed_target_revision: RevisionSummary | None
    current_target_revision: RevisionSummary | None
    target_is_stale: bool
    diff: RevisionDiff | None


@dataclass(frozen=True)
class PageList:
    items: tuple[PageSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class CandidateList:
    items: tuple[CandidateSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class SourceList:
    items: tuple[SourceView, ...]
    next_cursor: str | None


class KnowledgeQueryStore(Protocol):
    def list_pages(
        self,
        workspace_id: UUID,
        *,
        include_archived: bool,
        cursor: str | None,
        limit: int,
    ) -> PageList: ...

    def get_page(self, workspace_id: UUID, page_id: UUID) -> PageDetail | None: ...

    def get_revision(
        self, workspace_id: UUID, page_id: UUID, revision_id: UUID
    ) -> RevisionDetail | None: ...

    def materialize_sections(
        self,
        workspace_id: UUID,
        refs: tuple[KnowledgeSectionRef, ...],
    ) -> KnowledgeMaterialization: ...

    def list_candidates(
        self,
        workspace_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        status: str | None,
        source_task_id: UUID | None,
        source_subtask_id: UUID | None,
        source_worker_item_id: UUID | None,
        source_execution_id: UUID | None,
        source_artifact_id: UUID | None,
        source_evidence_id: UUID | None,
    ) -> CandidateList: ...

    def get_candidate(
        self, workspace_id: UUID, candidate_id: UUID
    ) -> CandidateDetail | None: ...

    def list_sources(
        self,
        workspace_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        availability: str | None,
    ) -> SourceList: ...


def _cursor(values: tuple[str, ...]) -> str:
    encoded = json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def decode_cursor(value: str | None, expected_parts: int) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not value or len(value) > 512:
        raise QueryValidationError("invalid cursor")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode((value + padding).encode())
        values = json.loads(decoded)
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
        raise QueryValidationError("invalid cursor") from exc
    if (
        not isinstance(values, list)
        or len(values) != expected_parts
        or not all(isinstance(item, str) and item for item in values)
    ):
        raise QueryValidationError("invalid cursor")
    return tuple(values)


def encode_page_cursor(slug: str, page_id: UUID) -> str:
    return _cursor((slug, str(page_id)))


def encode_candidate_cursor(created_at: datetime, candidate_id: UUID) -> str:
    return _cursor((created_at.isoformat(), str(candidate_id)))


def encode_source_cursor(label: str, source_id: UUID) -> str:
    return _cursor((label, str(source_id)))


def _section_body(value: Any) -> str:
    return value.body_markdown


def _body_text(value: Any) -> str:
    chunks: list[str] = []
    for section in sorted(value.sections, key=lambda item: item.ordinal):
        chunks.append(f"## {section.section_key}\n")
        chunks.append(_section_body(section))
        if not chunks[-1].endswith("\n"):
            chunks.append("\n")
    return "".join(chunks)


def _relation_snapshot(value: Any) -> dict[str, object]:
    if hasattr(value, "snapshot"):
        return value.snapshot
    return {
        "relation_key": value.relation_key,
        "relation_kind": value.relation_kind,
        "target_page_id": str(value.target_page_id),
        "target_revision_id": (
            str(value.target_revision_id) if value.target_revision_id else None
        ),
    }


def diff_revisions(
    before: Any,
    after: Any,
    *,
    max_diff_lines: int = 500,
    max_diff_chars: int = 32_000,
) -> RevisionDiff:
    if before.workspace_id != after.workspace_id:
        raise RevisionDiffError("revisions must belong to the same workspace")
    if before.page_id != after.page_id:
        raise RevisionDiffError("revisions must belong to the same page")
    if max_diff_lines < 1 or max_diff_chars < 1:
        raise RevisionDiffError("diff limits must be positive")

    before_sections = {item.section_key: item for item in before.sections}
    after_sections = {item.section_key: item for item in after.sections}
    shared_sections = sorted(before_sections.keys() & after_sections.keys())
    section_changes = tuple(
        SectionDiff(
            section_key=key,
            before_hash=before_sections[key].section_hash,
            after_hash=after_sections[key].section_hash,
        )
        for key in shared_sections
        if before_sections[key].section_hash != after_sections[key].section_hash
    )

    before_relations = {
        item.relation_key: _relation_snapshot(item) for item in before.relations
    }
    after_relations = {
        item.relation_key: _relation_snapshot(item) for item in after.relations
    }
    shared_relations = sorted(before_relations.keys() & after_relations.keys())
    sources_before = set(before.source_refs)
    sources_after = set(after.source_refs)
    diff_lines = list(
        unified_diff(
            _body_text(before).splitlines(keepends=True),
            _body_text(after).splitlines(keepends=True),
            fromfile=str(before.revision_id),
            tofile=str(after.revision_id),
        )
    )
    truncated = len(diff_lines) > max_diff_lines
    body_diff = "".join(diff_lines[:max_diff_lines])
    if len(body_diff) > max_diff_chars:
        body_diff = body_diff[:max_diff_chars]
        truncated = True

    return RevisionDiff(
        from_revision_id=before.revision_id,
        to_revision_id=after.revision_id,
        title_changed=before.title != after.title,
        summary_changed=before.summary != after.summary,
        tags_added=tuple(sorted(set(after.tags) - set(before.tags))),
        tags_removed=tuple(sorted(set(before.tags) - set(after.tags))),
        sections_added=tuple(sorted(after_sections.keys() - before_sections.keys())),
        sections_removed=tuple(sorted(before_sections.keys() - after_sections.keys())),
        sections_changed=section_changes,
        relations_added=tuple(sorted(after_relations.keys() - before_relations.keys())),
        relations_removed=tuple(sorted(before_relations.keys() - after_relations.keys())),
        relations_changed=tuple(
            key
            for key in shared_relations
            if before_relations[key] != after_relations[key]
        ),
        sources_added=tuple(sorted(sources_after - sources_before, key=str)),
        sources_removed=tuple(sorted(sources_before - sources_after, key=str)),
        body_unified_diff=body_diff,
        truncated=truncated,
    )


class KnowledgeQueryService:
    def __init__(self, store: KnowledgeQueryStore) -> None:
        self._store = store

    @staticmethod
    def _limit(value: int) -> int:
        if value < 1 or value > MAX_PAGE_SIZE:
            raise QueryValidationError("limit must be between 1 and 100")
        return value

    def list_pages(
        self,
        workspace_id: UUID,
        *,
        include_archived: bool = False,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> PageList:
        return self._store.list_pages(
            workspace_id,
            include_archived=include_archived,
            cursor=cursor,
            limit=self._limit(limit),
        )

    def get_page(self, workspace_id: UUID, page_id: UUID) -> PageDetail | None:
        return self._store.get_page(workspace_id, page_id)

    def get_revision(
        self, workspace_id: UUID, page_id: UUID, revision_id: UUID
    ) -> RevisionDetail | None:
        return self._store.get_revision(workspace_id, page_id, revision_id)

    def materialize_sections(
        self,
        workspace_id: UUID,
        refs: tuple[KnowledgeSectionRef, ...],
    ) -> KnowledgeMaterialization:
        return self._store.materialize_sections(workspace_id, refs)

    def list_candidates(
        self,
        workspace_id: UUID,
        *,
        status: str | None = None,
        source_task_id: UUID | None = None,
        source_subtask_id: UUID | None = None,
        source_worker_item_id: UUID | None = None,
        source_execution_id: UUID | None = None,
        source_artifact_id: UUID | None = None,
        source_evidence_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> CandidateList:
        return self._store.list_candidates(
            workspace_id,
            cursor=cursor,
            limit=self._limit(limit),
            status=status,
            source_task_id=source_task_id,
            source_subtask_id=source_subtask_id,
            source_worker_item_id=source_worker_item_id,
            source_execution_id=source_execution_id,
            source_artifact_id=source_artifact_id,
            source_evidence_id=source_evidence_id,
        )

    def get_candidate(
        self, workspace_id: UUID, candidate_id: UUID
    ) -> CandidateDetail | None:
        return self._store.get_candidate(workspace_id, candidate_id)

    def list_sources(
        self,
        workspace_id: UUID,
        *,
        availability: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> SourceList:
        return self._store.list_sources(
            workspace_id,
            cursor=cursor,
            limit=self._limit(limit),
            availability=availability,
        )
