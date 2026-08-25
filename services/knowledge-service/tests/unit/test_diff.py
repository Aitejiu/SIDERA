from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sidera_knowledge.application.knowledge_queries import (
    QueryValidationError,
    RevisionDiffError,
    decode_cursor,
    diff_revisions,
)
from sidera_knowledge.domain.pages import (
    KnowledgeRelationSnapshot,
    KnowledgeRevision,
    KnowledgeSectionSnapshot,
)

WORKSPACE_ID = uuid4()
PAGE_ID = uuid4()


def _revision(
    *,
    page_id=PAGE_ID,
    workspace_id=WORKSPACE_ID,
    number=1,
    title="Guide",
    summary="Summary",
    tags=("sidera",),
    sections=None,
    relations=(),
    source_refs=(),
):
    if sections is None:
        sections = (
            KnowledgeSectionSnapshot("overview", "Overview", "", "old", 0),
        )
    return KnowledgeRevision(
        revision_id=uuid4(),
        page_id=page_id,
        workspace_id=workspace_id,
        revision_number=number,
        title=title,
        summary=summary,
        tags=tags,
        sections=sections,
        relations=relations,
        source_refs=source_refs,
        change_reason="test",
        created_by="test",
        created_at=datetime(2026, 1, number, tzinfo=UTC),
    )


def test_diff_reports_structure_and_body_changes() -> None:
    before = _revision(
        relations=(
            KnowledgeRelationSnapshot(
                "dependency", "depends_on", uuid4(), uuid4()
            ),
        ),
        source_refs=(uuid4(),),
    )
    after = _revision(
        number=2,
        title="Updated Guide",
        summary="New summary",
        tags=("sidera", "knowledge"),
        sections=(
            KnowledgeSectionSnapshot("overview", "Overview", "", "new", 0),
            KnowledgeSectionSnapshot("setup", "Setup", "", "added", 1),
        ),
        relations=(
            KnowledgeRelationSnapshot("dependency", "relates_to", uuid4()),
        ),
        source_refs=(uuid4(),),
    )

    result = diff_revisions(before, after)

    assert result.title_changed is True
    assert result.summary_changed is True
    assert result.tags_added == ("knowledge",)
    assert result.tags_removed == ()
    assert result.sections_added == ("setup",)
    assert result.sections_removed == ()
    assert result.sections_changed[0].section_key == "overview"
    assert result.relations_changed == ("dependency",)
    assert len(result.sources_added) == 1
    assert len(result.sources_removed) == 1
    assert "-old" in result.body_unified_diff
    assert "+new" in result.body_unified_diff
    assert result.truncated is False
    assert result.is_empty is False


def test_same_revision_diff_is_empty() -> None:
    revision = _revision()

    result = diff_revisions(revision, revision)

    assert result.is_empty is True
    assert result.body_unified_diff == ""
    assert result.sections_changed == ()


def test_diff_rejects_cross_page_or_workspace_inputs() -> None:
    before = _revision()

    with pytest.raises(RevisionDiffError, match="same page"):
        diff_revisions(before, _revision(page_id=uuid4()))
    with pytest.raises(RevisionDiffError, match="same workspace"):
        diff_revisions(before, _revision(workspace_id=uuid4()))


def test_diff_marks_bounded_unified_output_as_truncated() -> None:
    before = _revision(
        sections=(
            KnowledgeSectionSnapshot("overview", "Overview", "", "old\n" * 20, 0),
        )
    )
    after = _revision(
        number=2,
        sections=(
            KnowledgeSectionSnapshot("overview", "Overview", "", "new\n" * 20, 0),
        ),
    )

    result = diff_revisions(before, after, max_diff_lines=4, max_diff_chars=40)

    assert result.truncated is True
    assert len(result.body_unified_diff) <= 40


def test_invalid_query_cursor_is_a_stable_validation_error() -> None:
    with pytest.raises(QueryValidationError, match="invalid cursor"):
        decode_cursor("not-base64", 2)
