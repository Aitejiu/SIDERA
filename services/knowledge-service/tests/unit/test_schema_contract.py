from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

from sidera_knowledge.persistence.models import Base

EXPECTED_TABLES = {
    "knowledge_pages",
    "knowledge_revisions",
    "knowledge_section_snapshots",
    "knowledge_relation_snapshots",
    "knowledge_sources",
    "knowledge_revision_sources",
    "knowledge_candidates",
    "knowledge_candidate_sources",
    "knowledge_candidate_reviews",
    "commands",
    "events",
    "outbox_messages",
}


def test_knowledge_metadata_has_exact_foundation_table_set() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_business_tables_are_workspace_scoped() -> None:
    for table in Base.metadata.tables.values():
        assert "workspace_id" in table.c, table.name


def test_snapshots_are_jsonb_and_revision_search_is_generated_fts() -> None:
    revisions = Base.metadata.tables["knowledge_revisions"]
    assert isinstance(revisions.c.snapshot.type, JSONB)
    assert isinstance(revisions.c.search_vector.type, TSVECTOR)
    assert revisions.c.search_vector.computed is not None
    assert revisions.c.search_vector.computed.persisted is True
    expression = str(revisions.c.search_vector.computed.sqltext).lower()
    assert "setweight" in expression
    assert "snapshot" in expression

    gin_indexes = [
        index
        for index in revisions.indexes
        if index.dialect_options["postgresql"].get("using") == "gin"
    ]
    assert {column.name for index in gin_indexes for column in index.columns} == {
        "search_vector"
    }


def test_commands_events_and_outbox_have_no_body_columns() -> None:
    forbidden_fragments = {"body", "content", "markdown"}
    for table_name in ("commands", "events", "outbox_messages"):
        columns = Base.metadata.tables[table_name].columns
        assert not any(
            any(fragment in column.name.lower() for fragment in forbidden_fragments)
            for column in columns
        ), table_name


def test_page_current_revision_is_nullable_and_revision_hash_is_fixed_width() -> None:
    pages = Base.metadata.tables["knowledge_pages"]
    revisions = Base.metadata.tables["knowledge_revisions"]

    assert pages.c.current_revision_id.nullable is True
    assert isinstance(revisions.c.content_hash.type, String)
    assert revisions.c.content_hash.type.length == 64
