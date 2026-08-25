from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import JSONB

pytestmark = pytest.mark.integration

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


def test_foundation_migration_creates_only_knowledge_tables(database) -> None:
    inspector = inspect(database.engine)
    tables = set(inspector.get_table_names())
    assert EXPECTED_TABLES <= tables
    assert "tasks" not in tables
    assert "workspaces" not in tables


def test_database_constraints_and_indexes_are_installed(database) -> None:
    inspector = inspect(database.engine)
    revisions = {item["name"] for item in inspector.get_indexes("knowledge_revisions")}
    assert "ix_knowledge_revisions_search_vector_gin" in revisions

    page_columns = {
        item["name"]: item for item in inspector.get_columns("knowledge_pages")
    }
    assert page_columns["current_revision_id"]["nullable"] is True

    revision_columns = {
        item["name"]: item
        for item in inspector.get_columns("knowledge_revisions")
    }
    assert isinstance(revision_columns["snapshot"]["type"], JSONB)


def test_search_vector_is_stored_generated_and_uses_weighted_snapshot_fields(
    database,
) -> None:
    query = text(
        """
        select pg_get_expr(adbin, adrelid)
        from pg_attrdef
        join pg_class on pg_class.oid = adrelid
        join pg_attribute on pg_attribute.attrelid = adrelid
          and pg_attribute.attnum = pg_attrdef.adnum
        where pg_class.relname = 'knowledge_revisions'
          and pg_attribute.attname = 'search_vector'
        """
    )
    with database.engine.connect() as connection:
        expression = connection.execute(query).scalar_one()

    lowered = expression.lower()
    assert "setweight" in lowered
    assert "snapshot" in lowered
    assert "'a'" in lowered
    assert "'b'" in lowered
    assert "'c'" in lowered
    assert "stored" not in lowered
