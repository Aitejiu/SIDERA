from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_knowledge.domain.pages import (
    KnowledgePage,
    KnowledgeRevision,
    KnowledgeSectionSnapshot,
)
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.models import (
    KnowledgeRevisionModel,
    OutboxMessageModel,
)
from sidera_knowledge.persistence.repositories import PersistenceConflict
from sidera_knowledge.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


def _page(workspace_id: UUID, slug: str | None = None) -> KnowledgePage:
    return KnowledgePage(
        page_id=uuid4(),
        workspace_id=workspace_id,
        canonical_title="Repository page",
        slug=slug or f"repository-{uuid4().hex}",
    )


def _revision(page: KnowledgePage, number: int = 1) -> KnowledgeRevision:
    return KnowledgeRevision(
        revision_id=uuid4(),
        page_id=page.page_id,
        workspace_id=page.workspace_id,
        revision_number=number,
        title="Repository page",
        summary="Stored revision",
        tags=("repository",),
        sections=(
            KnowledgeSectionSnapshot("details", "Details", "Second", "two", 1),
            KnowledgeSectionSnapshot("overview", "Overview", "First", "one", 0),
        ),
        relations=(),
        source_refs=(),
        change_reason="test",
        created_by="test",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_page_reads_are_workspace_scoped(database: Database) -> None:
    workspace_a = uuid4()
    workspace_b = uuid4()
    page = _page(workspace_a)
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_page(page)
        uow.commit()

    with SqlAlchemyUnitOfWork(database) as uow:
        assert uow.pages.get_page(page.page_id, workspace_a) == page
        assert uow.pages.get_page(page.page_id, workspace_b) is None


def test_revision_reads_return_ordered_complete_snapshot(database: Database) -> None:
    workspace_id = uuid4()
    page = _page(workspace_id)
    revision = _revision(page)
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_page(page)
        uow.commit()

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_revision_and_advance_page(revision, expected_page_version=1)
        uow.commit()

    with SqlAlchemyUnitOfWork(database) as uow:
        loaded = uow.pages.get_revision(revision.revision_id, workspace_id)
        assert loaded is not None
        assert [section.section_key for section in loaded.sections] == [
            "overview",
            "details",
        ]
        assert loaded.content_hash == revision.content_hash
        page_after = uow.pages.get_page(page.page_id, workspace_id)
        assert page_after is not None
        assert page_after.current_revision_id == revision.revision_id
        assert page_after.version == 2


def test_duplicate_slug_and_stale_page_version_are_explicit_conflicts(
    database: Database,
) -> None:
    workspace_id = uuid4()
    first = _page(workspace_id, slug="same-slug")
    second = _page(workspace_id, slug="same-slug")
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_page(first)
        uow.commit()

    with pytest.raises(PersistenceConflict, match="unique"), SqlAlchemyUnitOfWork(
        database
    ) as uow:
        uow.pages.add_page(second)
        uow.commit()

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_revision_and_advance_page(
            _revision(first), expected_page_version=1
        )
        uow.commit()

    with pytest.raises(PersistenceConflict, match="version"), SqlAlchemyUnitOfWork(
        database
    ) as uow:
        uow.pages.add_revision_and_advance_page(
            _revision(first, number=2), expected_page_version=1
        )
        uow.commit()


def test_rollback_leaves_page_revision_and_outbox_unchanged(database: Database) -> None:
    workspace_id = uuid4()
    page = _page(workspace_id)
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_page(page)
        uow.commit()

    with database.session() as session:
        before_revisions = session.scalar(
            select(func.count()).select_from(KnowledgeRevisionModel)
        )
        before_outbox = session.scalar(select(func.count()).select_from(OutboxMessageModel))

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_revision_and_advance_page(_revision(page), expected_page_version=1)
        uow.rollback()

    with database.session() as session:
        after_revisions = session.scalar(
            select(func.count()).select_from(KnowledgeRevisionModel)
        )
        after_outbox = session.scalar(select(func.count()).select_from(OutboxMessageModel))
        assert after_revisions == before_revisions
        assert after_outbox == before_outbox

    with SqlAlchemyUnitOfWork(database) as uow:
        assert uow.pages.get_page(page.page_id, workspace_id) == page
