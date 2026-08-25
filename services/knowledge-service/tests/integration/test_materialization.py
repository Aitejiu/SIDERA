from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sidera_knowledge.application.knowledge_queries import (
    KnowledgeMaterializationError,
    KnowledgeSectionRef,
)
from sidera_knowledge.domain.pages import (
    KnowledgePage,
    KnowledgeRevision,
    KnowledgeSectionSnapshot,
    KnowledgeSource,
)
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.query_store import SqlAlchemyKnowledgeQueryStore
from sidera_knowledge.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


def _seed_history(database: Database) -> tuple[KnowledgePage, KnowledgeRevision]:
    workspace_id = uuid4()
    page = KnowledgePage(
        page_id=uuid4(),
        workspace_id=workspace_id,
        canonical_title="Materialized page",
        slug=f"materialized-{uuid4().hex}",
    )
    sources = (
        KnowledgeSource(
            source_id=uuid4(),
            workspace_id=workspace_id,
            source_type="manual",
            label="design.md",
            metadata={"origin": "test"},
        ),
        KnowledgeSource(
            source_id=uuid4(),
            workspace_id=workspace_id,
            source_type="evidence",
            label="evidence.json",
            metadata={"origin": "test"},
        ),
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_page(page)
        for source in sources:
            uow.sources.add_source(source)
        uow.commit()

    first = KnowledgeRevision(
        revision_id=uuid4(),
        page_id=page.page_id,
        workspace_id=workspace_id,
        revision_number=1,
        title="Materialized page",
        summary="Historical revision",
        tags=("materialization",),
        sections=(
            KnowledgeSectionSnapshot(
                "overview", "Overview", "Old summary", "old body\n", 0
            ),
            KnowledgeSectionSnapshot(
                "details", "Details", "Old details", "old details\n", 1
            ),
        ),
        relations=(),
        source_refs=tuple(source.source_id for source in sources),
        change_reason="initial",
        created_by="test",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = KnowledgeRevision(
        revision_id=uuid4(),
        page_id=page.page_id,
        workspace_id=workspace_id,
        revision_number=2,
        title="Materialized page",
        summary="Current revision",
        tags=("materialization",),
        sections=(
            KnowledgeSectionSnapshot(
                "overview", "Overview", "New summary", "new body\n", 0
            ),
            KnowledgeSectionSnapshot(
                "details", "Details", "New details", "new details\n", 1
            ),
        ),
        relations=(),
        source_refs=tuple(source.source_id for source in sources),
        change_reason="update",
        created_by="test",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_revision_and_advance_page(first, expected_page_version=1)
        uow.commit()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_revision_and_advance_page(second, expected_page_version=2)
        uow.commit()
        uow.pages.archive_page(page.page_id, workspace_id, expected_version=3)
        uow.commit()
    return page, first


def _ref(
    page: KnowledgePage,
    revision: KnowledgeRevision,
    section_key: str,
    ordinal: int,
) -> KnowledgeSectionRef:
    section = next(item for item in revision.sections if item.section_key == section_key)
    return KnowledgeSectionRef(
        ordinal=ordinal,
        page_id=page.page_id,
        revision_id=revision.revision_id,
        revision_hash=revision.content_hash,
        section_key=section.section_key,
        section_hash=section.section_hash,
        source_ids=revision.source_refs,
    )


def test_materialize_exact_historical_sections_from_archived_page(
    database: Database,
) -> None:
    page, revision = _seed_history(database)
    store = SqlAlchemyKnowledgeQueryStore(database)

    result = store.materialize_sections(
        page.workspace_id,
        (
            _ref(page, revision, "details", ordinal=9),
            _ref(page, revision, "overview", ordinal=3),
        ),
    )

    assert [item.ordinal for item in result.sections] == [3, 9]
    assert [item.section_key for item in result.sections] == ["overview", "details"]
    assert result.sections[0].body_markdown == "old body\n"
    assert result.sections[0].sources[0].metadata == {"origin": "test"}
    assert result.sections[0].trust == "workspace_confirmed"
    assert result.total_byte_count == len(b"old body\nold details\n")


def test_materialize_rejects_any_hash_or_source_mismatch_as_one_batch(
    database: Database,
) -> None:
    page, revision = _seed_history(database)
    valid = _ref(page, revision, "overview", ordinal=0)
    store = SqlAlchemyKnowledgeQueryStore(database)

    with pytest.raises(KnowledgeMaterializationError) as hash_error:
        store.materialize_sections(
            page.workspace_id,
            (
                valid,
                replace(
                    _ref(page, revision, "details", ordinal=1),
                    section_hash="a" * 64,
                ),
            ),
        )
    assert hash_error.value.code == "hash_mismatch"

    with pytest.raises(KnowledgeMaterializationError) as source_error:
        store.materialize_sections(
            page.workspace_id,
            (replace(valid, source_ids=(uuid4(),)),),
        )
    assert source_error.value.code == "source_invalid"


def test_materialize_enforces_bounded_section_count_before_loading(
    database: Database,
) -> None:
    page, revision = _seed_history(database)
    refs = tuple(
        _ref(page, revision, "overview", ordinal=ordinal) for ordinal in range(33)
    )

    with pytest.raises(KnowledgeMaterializationError, match="sections") as error:
        SqlAlchemyKnowledgeQueryStore(database).materialize_sections(
            page.workspace_id, refs
        )
    assert error.value.code == "content_limit_exceeded"
