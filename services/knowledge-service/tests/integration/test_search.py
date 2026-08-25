from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sidera_knowledge.application.retrieval import SearchFilters
from sidera_knowledge.domain.pages import (
    KnowledgePage,
    KnowledgeRevision,
    KnowledgeSectionSnapshot,
    KnowledgeSource,
    SourceType,
)
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.query_store import PostgresRetrievalAdapter
from sidera_knowledge.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


def _seed_revision(
    database: Database,
    workspace_id,
    *,
    slug: str,
    title: str,
    summary: str,
    tags: tuple[str, ...],
    body: str,
    source_type: SourceType = "manual",
    archived: bool = False,
) -> tuple[KnowledgePage, KnowledgeSource, KnowledgeRevision]:
    page = KnowledgePage(
        page_id=uuid4(),
        workspace_id=workspace_id,
        canonical_title=title,
        slug=slug,
    )
    source = KnowledgeSource(
        source_id=uuid4(),
        workspace_id=workspace_id,
        source_type=source_type,
        label=f"{slug}.md",
        workspace_relative_path=f"docs/{slug}.md",
        metadata={"source": slug},
    )
    revision = KnowledgeRevision(
        revision_id=uuid4(),
        page_id=page.page_id,
        workspace_id=workspace_id,
        revision_number=1,
        title=title,
        summary=summary,
        tags=tags,
        sections=(KnowledgeSectionSnapshot("overview", "Overview", "", body, 0),),
        relations=(),
        source_refs=(source.source_id,),
        change_reason="seed",
        created_by="test",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_page(page)
        uow.sources.add_source(source)
        uow.commit()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_revision_and_advance_page(revision, expected_page_version=1)
        uow.commit()
    if archived:
        with SqlAlchemyUnitOfWork(database) as uow:
            uow.pages.archive_page(page.page_id, workspace_id, expected_version=2)
            uow.commit()
    return page, source, revision


def test_postgres_search_ranks_weighted_fields_and_scopes_workspace(
    database: Database,
) -> None:
    workspace_id = uuid4()
    foreign_workspace_id = uuid4()
    _seed_revision(
        database,
        workspace_id,
        slug="title-hit",
        title="signal title",
        summary="ordinary summary",
        tags=("ordinary",),
        body="ordinary body",
        source_type="task",
    )
    _seed_revision(
        database,
        workspace_id,
        slug="tag-hit",
        title="ordinary title",
        summary="ordinary summary",
        tags=("signal",),
        body="ordinary body",
        source_type="artifact",
    )
    _seed_revision(
        database,
        workspace_id,
        slug="summary-hit",
        title="ordinary title",
        summary="signal summary",
        tags=("ordinary",),
        body="ordinary body",
        source_type="evidence",
    )
    _seed_revision(
        database,
        workspace_id,
        slug="body-hit",
        title="ordinary title",
        summary="ordinary summary",
        tags=("ordinary",),
        body="signal body",
    )
    foreign_page, _, _ = _seed_revision(
        database,
        foreign_workspace_id,
        slug="foreign-hit",
        title="signal foreign",
        summary="ordinary summary",
        tags=(),
        body="ordinary body",
    )
    adapter = PostgresRetrievalAdapter(database)

    hits = adapter.search(workspace_id, "signal", SearchFilters(), limit=20)

    assert [hit.title for hit in hits][:4] == [
        "signal title",
        "ordinary title",
        "ordinary title",
        "ordinary title",
    ]
    assert hits[0].rank > hits[-1].rank
    assert all(hit.page_id != foreign_page.page_id for hit in hits)
    assert all(not hasattr(hit, "body_markdown") for hit in hits)
    assert hits[0].source_types == ("task",)
    assert hits[0].source_availability == ("available",)
    assert [hit.source_types[0] for hit in hits] == [
        "task",
        "artifact",
        "evidence",
        "manual",
    ]
    assert hits[0].rank_explanation.startswith("weighted")


def test_postgres_search_applies_current_status_and_source_filters(
    database: Database,
) -> None:
    workspace_id = uuid4()
    archived, _, _ = _seed_revision(
        database,
        workspace_id,
        slug="archived-hit",
        title="archived signal",
        summary="ordinary",
        tags=(),
        body="ordinary",
        archived=True,
    )
    _, _, _ = _seed_revision(
        database,
        workspace_id,
        slug="evidence-hit",
        title="evidence",
        summary="ordinary",
        tags=("signal",),
        body="ordinary",
        source_type="evidence",
    )
    adapter = PostgresRetrievalAdapter(database)

    default_hits = adapter.search(workspace_id, "signal", SearchFilters(), limit=20)
    archived_hits = adapter.search(
        workspace_id,
        "signal",
        SearchFilters(status="archived"),
        limit=20,
    )
    evidence_hits = adapter.search(
        workspace_id,
        "signal",
        SearchFilters(source_types=("evidence",)),
        limit=20,
    )

    assert all(hit.page_id != archived.page_id for hit in default_hits)
    assert [hit.page_id for hit in archived_hits] == [archived.page_id]
    assert all(hit.source_types == ("evidence",) for hit in evidence_hits)


def test_postgres_search_allows_empty_query_only_with_filters(
    database: Database,
) -> None:
    workspace_id = uuid4()
    page, _, _ = _seed_revision(
        database,
        workspace_id,
        slug="empty-filter-hit",
        title="filtered page",
        summary="ordinary",
        tags=("filtered",),
        body="ordinary",
    )
    adapter = PostgresRetrievalAdapter(database)

    hits = adapter.search(
        workspace_id,
        "",
        SearchFilters(tags=("filtered",)),
        limit=20,
    )

    assert [hit.page_id for hit in hits] == [page.page_id]
