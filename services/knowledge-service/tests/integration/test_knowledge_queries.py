from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from sidera_knowledge.application.command_service import KnowledgeCommandService
from sidera_knowledge.application.knowledge_commands import knowledge_command_handlers
from sidera_knowledge.application.knowledge_queries import KnowledgeQueryService
from sidera_knowledge.domain.commands import CommandEnvelope
from sidera_knowledge.domain.pages import (
    KnowledgePage,
    KnowledgeRevision,
    KnowledgeSectionSnapshot,
    KnowledgeSource,
)
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.models import KnowledgeRevisionModel
from sidera_knowledge.persistence.query_store import SqlAlchemyKnowledgeQueryStore
from sidera_knowledge.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


def _revision(page: KnowledgePage, number: int, source_id=None) -> KnowledgeRevision:
    return KnowledgeRevision(
        revision_id=uuid4(),
        page_id=page.page_id,
        workspace_id=page.workspace_id,
        revision_number=number,
        title=f"{page.canonical_title} {number}",
        summary=f"Summary {number}",
        tags=("knowledge", f"v{number}"),
        sections=(
            KnowledgeSectionSnapshot(
                "details", "Details", "", f"body {number}", 1
            ),
            KnowledgeSectionSnapshot(
                "overview", "Overview", "", f"overview {number}", 0
            ),
        ),
        relations=(),
        source_refs=(source_id,) if source_id else (),
        change_reason=f"revision {number}",
        created_by="test",
        created_at=datetime(2026, 1, number, tzinfo=UTC),
    )


def _seed_page(
    database: Database,
    workspace_id,
    *,
    slug: str,
    archived: bool = False,
) -> tuple[KnowledgePage, KnowledgeSource, KnowledgeRevision, KnowledgeRevision]:
    page = KnowledgePage(
        page_id=uuid4(),
        workspace_id=workspace_id,
        canonical_title=f"Page {slug}",
        slug=slug,
    )
    source = KnowledgeSource(
        source_id=uuid4(),
        workspace_id=workspace_id,
        source_type="markdown_file",
        label=f"{slug}.md",
        workspace_relative_path=f"docs/{slug}.md",
        git_revision="abc123",
        metadata={"task_id": str(uuid4())},
    )
    first = _revision(page, 1, source.source_id)
    second = _revision(page, 2, source.source_id)
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_page(page)
        uow.sources.add_source(source)
        uow.commit()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_revision_and_advance_page(first, expected_page_version=1)
        uow.commit()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_revision_and_advance_page(second, expected_page_version=2)
        uow.commit()
    if archived:
        with SqlAlchemyUnitOfWork(database) as uow:
            uow.pages.archive_page(page.page_id, workspace_id, expected_version=3)
            uow.commit()
    return page, source, first, second


def _candidate_payload(candidate_id, source_id, *, task_id, page=None, revision=None):
    payload = {
        "candidate_schema_version": 1,
        "candidate_id": str(candidate_id),
        "operation": "create_page" if page is None else "revise_page",
        "title": "Candidate Guide",
        "slug": f"candidate-{candidate_id.hex}",
        "summary": "Candidate summary",
        "tags": ["candidate"],
        "sections": [
            {
                "section_key": "overview",
                "heading": "Overview",
                "summary": "",
                "body_markdown": f"body marker {task_id}",
                "ordinal": 0,
            }
        ],
        "relations": [],
        "sources": [
            {
                "source_id": str(source_id),
                "source_type": "manual",
                "label": "Candidate note",
                "metadata": {
                    "task_id": str(task_id),
                    "artifact_id": str(uuid4()),
                },
            }
        ],
        "provenance": {
            "producer_type": "main_agent",
            "producer_id": "agent-1",
            "task_id": str(task_id),
        },
    }
    if page is not None and revision is not None:
        payload.update(
            {
                "target_page_id": str(page.page_id),
                "expected_revision_id": str(revision.revision_id),
                "expected_revision_hash": revision.content_hash,
            }
        )
    return payload


def _submit_candidate(database: Database, workspace_id, payload, candidate_id):
    command = CommandEnvelope(
        command_id=uuid4(),
        workspace_id=workspace_id,
        command_type="SubmitKnowledgeCandidate",
        actor_type="main_agent",
        actor_id="agent-1",
        target_type="knowledge_candidate",
        target_id=candidate_id,
        payload=payload,
        expected_version=None,
        idempotency_key=f"query-candidate-{candidate_id}",
        correlation_id=uuid4(),
    )
    return KnowledgeCommandService(
        lambda: SqlAlchemyUnitOfWork(database), knowledge_command_handlers()
    ).handle(command)


def test_page_queries_are_scoped_and_history_is_current_first(
    database: Database,
) -> None:
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    active, source, _, second = _seed_page(
        database, workspace_id, slug="active-page"
    )
    archived, _, _, _ = _seed_page(
        database, workspace_id, slug="archived-page", archived=True
    )
    _seed_page(database, other_workspace_id, slug="foreign-page")
    query = KnowledgeQueryService(SqlAlchemyKnowledgeQueryStore(database))

    current = query.list_pages(workspace_id)
    assert [item.page_id for item in current.items] == [active.page_id]
    all_pages = query.list_pages(workspace_id, include_archived=True)
    assert {item.page_id for item in all_pages.items} == {active.page_id, archived.page_id}

    detail = query.get_page(workspace_id, active.page_id)
    assert detail is not None
    assert detail.current_revision is not None
    assert detail.current_revision.revision_id == second.revision_id
    assert [item.revision_number for item in detail.revision_history] == [2, 1]
    assert [item.section_key for item in detail.current_revision.sections] == [
        "overview",
        "details",
    ]
    assert detail.current_revision.sources[0].source_id == source.source_id
    assert query.get_page(other_workspace_id, active.page_id) is None

    first_page = query.list_pages(workspace_id, limit=1, include_archived=True)
    assert len(first_page.items) == 1
    assert first_page.next_cursor is not None
    second_page = query.list_pages(
        workspace_id,
        limit=1,
        include_archived=True,
        cursor=first_page.next_cursor,
    )
    assert len(second_page.items) == 1
    assert second_page.items[0].page_id != first_page.items[0].page_id


def test_candidate_queries_filter_structured_source_provenance_and_show_target_diff(
    database: Database,
) -> None:
    workspace_id = uuid4()
    page, _, first, second = _seed_page(
        database, workspace_id, slug="target-page"
    )
    task_id = uuid4()
    candidate_id = uuid4()
    source_id = uuid4()
    result = _submit_candidate(
        database,
        workspace_id,
        _candidate_payload(
            candidate_id,
            source_id,
            task_id=task_id,
            page=page,
            revision=first,
        ),
        candidate_id,
    )
    assert result.status == "applied"
    query = KnowledgeQueryService(SqlAlchemyKnowledgeQueryStore(database))

    candidates = query.list_candidates(workspace_id, source_task_id=task_id)
    assert [item.candidate_id for item in candidates.items] == [candidate_id]
    assert candidates.items[0].proposed_title == "Candidate Guide"
    assert candidates.items[0].proposed_slug == f"candidate-{candidate_id.hex}"
    assert query.list_candidates(workspace_id, source_task_id=uuid4()).items == ()

    detail = query.get_candidate(workspace_id, candidate_id)
    assert detail is not None
    assert detail.provenance["task_id"] == str(task_id)
    assert detail.proposed_snapshot["sections"][0]["body_markdown"]
    assert detail.observed_target_revision is not None
    assert detail.observed_target_revision.revision_id == first.revision_id
    assert detail.current_target_revision is not None
    assert detail.current_target_revision.revision_id == second.revision_id
    assert detail.target_is_stale is True
    assert detail.diff is not None
    assert detail.diff.from_revision_id == first.revision_id
    assert detail.sources[0].availability == "available"

    source_list = query.list_sources(workspace_id)
    assert source_list.items[0].source_id == source_id

    with database.session() as session:
        revision_count = session.scalar(
            select(KnowledgeRevisionModel.id).where(
                KnowledgeRevisionModel.id == second.revision_id,
                KnowledgeRevisionModel.workspace_id == workspace_id,
            )
        )
    assert revision_count == second.revision_id
