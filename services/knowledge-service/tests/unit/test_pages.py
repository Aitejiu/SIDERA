from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_knowledge.domain.common import DomainValidationError
from sidera_knowledge.domain.pages import (
    KnowledgePage,
    KnowledgeRelationSnapshot,
    KnowledgeRevision,
    KnowledgeSectionSnapshot,
    KnowledgeSource,
)

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PAGE_ID = UUID("00000000-0000-0000-0000-000000000002")


def _section(key: str = "overview", body: str = "body") -> KnowledgeSectionSnapshot:
    return KnowledgeSectionSnapshot(
        section_key=key,
        heading="Overview",
        summary="A summary",
        body_markdown=body,
        ordinal=0,
    )


def _revision(
    *,
    section: KnowledgeSectionSnapshot | None = None,
    tags: tuple[str, ...] = ("sidera",),
    source_refs: tuple[UUID, ...] = (),
    relations: tuple[KnowledgeRelationSnapshot, ...] = (),
) -> KnowledgeRevision:
    return KnowledgeRevision(
        revision_id=uuid4(),
        page_id=PAGE_ID,
        workspace_id=WORKSPACE_ID,
        revision_number=1,
        title="Project guide",
        summary="A durable guide",
        tags=tags,
        sections=(section or _section(),),
        relations=relations,
        source_refs=source_refs,
        change_reason="initial publication",
        created_by="owner:aitejiu",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_revision_hash_covers_complete_consumer_snapshot() -> None:
    source_id = uuid4()
    relation = KnowledgeRelationSnapshot(
        relation_key="implementation",
        relation_kind="implements",
        target_page_id=uuid4(),
    )
    base = _revision(source_refs=(source_id,), relations=(relation,))

    assert base.revision_number == 1
    assert base.content_hash == base.content_hash
    assert base.snapshot["source_refs"] == [str(source_id)]
    assert base.snapshot["sections"][0]["body_markdown"] == "body\n"

    assert _revision(section=_section(body="changed"), source_refs=(source_id,)).content_hash != base.content_hash
    assert _revision(tags=("different",), source_refs=(source_id,), relations=(relation,)).content_hash != base.content_hash


def test_revision_rejects_duplicate_section_keys_and_invalid_order() -> None:
    with pytest.raises(DomainValidationError, match="section_key"):
        KnowledgeRevision(
            revision_id=uuid4(),
            page_id=PAGE_ID,
            workspace_id=WORKSPACE_ID,
            revision_number=1,
            title="Project guide",
            summary="A durable guide",
            tags=("sidera",),
            sections=(_section(), _section()),
            relations=(),
            source_refs=(),
            change_reason="initial publication",
            created_by="owner:aitejiu",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    with pytest.raises(DomainValidationError, match="ordinal"):
        KnowledgeSectionSnapshot(
            section_key="bad",
            heading="Bad",
            summary="",
            body_markdown="body",
            ordinal=-1,
        )


def test_page_has_stable_identity_and_valid_status() -> None:
    page = KnowledgePage(
        page_id=PAGE_ID,
        workspace_id=WORKSPACE_ID,
        canonical_title="Project guide",
        slug="project-guide",
        current_revision_id=uuid4(),
        version=1,
    )
    assert page.status == "active"
    assert page.page_id == PAGE_ID

    with pytest.raises(DomainValidationError):
        KnowledgePage(
            page_id=PAGE_ID,
            workspace_id=WORKSPACE_ID,
            canonical_title="",
            slug="project-guide",
            version=1,
        )


def test_source_rejects_absolute_path_and_can_become_unavailable_without_revision_change() -> None:
    with pytest.raises(DomainValidationError):
        KnowledgeSource(
            source_id=uuid4(),
            workspace_id=WORKSPACE_ID,
            source_type="markdown_file",
            label="bad",
            workspace_relative_path="/tmp/source.md",
        )

    source = KnowledgeSource(
        source_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        source_type="markdown_file",
        label="design",
        workspace_relative_path="docs/design.md",
        digest="a" * 64,
        last_checked_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    unavailable = source.mark_unavailable("file_missing")
    assert source.availability == "available"
    assert unavailable.availability == "unavailable"
    assert unavailable.safe_reason_code == "file_missing"
    assert unavailable.version == source.version + 1
