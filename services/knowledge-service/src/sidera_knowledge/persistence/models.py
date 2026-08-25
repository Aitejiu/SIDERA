from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UpdatedTimestampMixin(TimestampMixin):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class KnowledgePageModel(UpdatedTimestampMixin, Base):
    __tablename__ = "knowledge_pages"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'archived')", name="ck_knowledge_pages_status"
        ),
        CheckConstraint("version >= 1", name="ck_knowledge_pages_version"),
        ForeignKeyConstraint(
            ["current_revision_id", "workspace_id"],
            ["knowledge_revisions.id", "knowledge_revisions.workspace_id"],
            name="fk_knowledge_pages_current_revision",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        UniqueConstraint("workspace_id", "slug", name="uq_knowledge_pages_workspace_slug"),
        UniqueConstraint("id", "workspace_id", name="uq_knowledge_pages_id_workspace"),
        Index("ix_knowledge_pages_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    canonical_title: Mapped[str] = mapped_column(String(500), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    current_revision_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class KnowledgeRevisionModel(TimestampMixin, Base):
    __tablename__ = "knowledge_revisions"
    __table_args__ = (
        CheckConstraint(
            "revision_number >= 1", name="ck_knowledge_revisions_number"
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_revisions_content_hash",
        ),
        ForeignKeyConstraint(
            ["page_id", "workspace_id"],
            ["knowledge_pages.id", "knowledge_pages.workspace_id"],
            name="fk_knowledge_revisions_page_workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "page_id",
            "revision_number",
            name="uq_knowledge_revisions_page_number",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_knowledge_revisions_id_workspace"),
        Index("ix_knowledge_revisions_workspace_page", "workspace_id", "page_id"),
        Index(
            "ix_knowledge_revisions_search_vector_gin",
            "search_vector",
            postgresql_using="gin",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    page_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_candidate_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    search_document: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('simple', "
            "coalesce(snapshot->>'title', '') || ' ' || "
            "coalesce(snapshot->>'tags', '')), 'A') || "
            "setweight(to_tsvector('simple', coalesce(snapshot->>'summary', '')), 'B') || "
            "setweight(to_tsvector('simple', coalesce(snapshot->>'sections', '')), 'C')",
            persisted=True,
        ),
        nullable=False,
    )


class KnowledgeSectionSnapshotModel(TimestampMixin, Base):
    __tablename__ = "knowledge_section_snapshots"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_knowledge_sections_ordinal"),
        CheckConstraint(
            "content_byte_count >= 0", name="ck_knowledge_sections_byte_count"
        ),
        CheckConstraint(
            "section_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_sections_hash",
        ),
        ForeignKeyConstraint(
            ["revision_id", "workspace_id"],
            ["knowledge_revisions.id", "knowledge_revisions.workspace_id"],
            name="fk_knowledge_sections_revision_workspace",
            ondelete="RESTRICT",
        ),
        PrimaryKeyConstraint(
            "revision_id", "section_key", name="pk_knowledge_section_snapshots"
        ),
        Index("ix_knowledge_sections_workspace_revision", "workspace_id", "revision_id"),
    )

    revision_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    section_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    heading: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    section_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_byte_count: Mapped[int] = mapped_column(BigInteger, nullable=False)


class KnowledgeRelationSnapshotModel(TimestampMixin, Base):
    __tablename__ = "knowledge_relation_snapshots"
    __table_args__ = (
        CheckConstraint(
            "relation_kind IN ('relates_to', 'depends_on', 'supersedes', "
            "'derived_from', 'implements')",
            name="ck_knowledge_relations_kind",
        ),
        ForeignKeyConstraint(
            ["revision_id", "workspace_id"],
            ["knowledge_revisions.id", "knowledge_revisions.workspace_id"],
            name="fk_knowledge_relations_revision_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_page_id", "workspace_id"],
            ["knowledge_pages.id", "knowledge_pages.workspace_id"],
            name="fk_knowledge_relations_target_page_workspace",
            ondelete="RESTRICT",
        ),
        PrimaryKeyConstraint(
            "revision_id", "relation_key", name="pk_knowledge_relation_snapshots"
        ),
    )

    revision_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    relation_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    relation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_page_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    target_revision_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )


class KnowledgeSourceModel(UpdatedTimestampMixin, Base):
    __tablename__ = "knowledge_sources"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('manual', 'markdown_file', 'task', 'artifact', "
            "'evidence', 'external_url')",
            name="ck_knowledge_sources_type",
        ),
        CheckConstraint(
            "availability IN ('available', 'unavailable')",
            name="ck_knowledge_sources_availability",
        ),
        CheckConstraint(
            "digest IS NULL OR digest ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_sources_digest",
        ),
        CheckConstraint(
            "workspace_relative_path IS NULL OR "
            "(left(workspace_relative_path, 1) <> '/' AND "
            "left(workspace_relative_path, 1) <> '~' AND "
            "position('..' in workspace_relative_path) = 0 AND "
            "position('\\\\' in workspace_relative_path) = 0)",
            name="ck_knowledge_sources_relative_path",
        ),
        CheckConstraint(
            "availability = 'available' OR safe_reason_code IS NOT NULL",
            name="ck_knowledge_sources_unavailable_reason",
        ),
        CheckConstraint("version >= 1", name="ck_knowledge_sources_version"),
        UniqueConstraint("id", "workspace_id", name="uq_knowledge_sources_id_workspace"),
        Index("ix_knowledge_sources_workspace_availability", "workspace_id", "availability"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    uri: Mapped[str | None] = mapped_column(Text)
    workspace_relative_path: Mapped[str | None] = mapped_column(String(1_024))
    git_revision: Mapped[str | None] = mapped_column(String(255))
    digest: Mapped[str | None] = mapped_column(String(64))
    source_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    availability: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="available"
    )
    safe_reason_code: Mapped[str | None] = mapped_column(String(64))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class KnowledgeRevisionSourceModel(Base):
    __tablename__ = "knowledge_revision_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "workspace_id"],
            ["knowledge_revisions.id", "knowledge_revisions.workspace_id"],
            name="fk_knowledge_revision_sources_revision_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_id", "workspace_id"],
            ["knowledge_sources.id", "knowledge_sources.workspace_id"],
            name="fk_knowledge_revision_sources_source_workspace",
            ondelete="RESTRICT",
        ),
        PrimaryKeyConstraint(
            "revision_id", "source_id", name="pk_knowledge_revision_sources"
        ),
        UniqueConstraint(
            "revision_id",
            "ordinal",
            name="uq_knowledge_revision_sources_ordinal",
        ),
    )

    revision_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    source_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    workspace_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    provenance_role: Mapped[str] = mapped_column(String(64), nullable=False)


class KnowledgeCandidateModel(UpdatedTimestampMixin, Base):
    __tablename__ = "knowledge_candidates"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('create_page', 'revise_page')",
            name="ck_knowledge_candidates_operation",
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'changes_requested', "
            "'stale', 'withdrawn')",
            name="ck_knowledge_candidates_status",
        ),
        CheckConstraint(
            "candidate_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_candidates_hash",
        ),
        CheckConstraint(
            "version >= 1", name="ck_knowledge_candidates_version"
        ),
        CheckConstraint(
            "(operation = 'create_page' AND target_page_id IS NULL AND "
            "expected_revision_id IS NULL AND expected_revision_hash IS NULL) OR "
            "(operation = 'revise_page' AND target_page_id IS NOT NULL AND "
            "expected_revision_id IS NOT NULL AND expected_revision_hash IS NOT NULL)",
            name="ck_knowledge_candidates_target_contract",
        ),
        ForeignKeyConstraint(
            ["target_page_id", "workspace_id"],
            ["knowledge_pages.id", "knowledge_pages.workspace_id"],
            name="fk_knowledge_candidates_target_page_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["expected_revision_id", "workspace_id"],
            ["knowledge_revisions.id", "knowledge_revisions.workspace_id"],
            name="fk_knowledge_candidates_expected_revision_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["supersedes_candidate_id", "workspace_id"],
            ["knowledge_candidates.id", "knowledge_candidates.workspace_id"],
            name="fk_knowledge_candidates_supersedes_workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_knowledge_candidates_id_workspace"),
        Index("ix_knowledge_candidates_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    target_page_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    expected_revision_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    expected_revision_hash: Mapped[str | None] = mapped_column(String(64))
    proposed_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    producer_type: Mapped[str] = mapped_column(String(64), nullable=False)
    producer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    subtask_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    worker_item_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    execution_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    supersedes_candidate_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class KnowledgeCandidateSourceModel(Base):
    __tablename__ = "knowledge_candidate_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["candidate_id", "workspace_id"],
            ["knowledge_candidates.id", "knowledge_candidates.workspace_id"],
            name="fk_knowledge_candidate_sources_candidate_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_id", "workspace_id"],
            ["knowledge_sources.id", "knowledge_sources.workspace_id"],
            name="fk_knowledge_candidate_sources_source_workspace",
            ondelete="RESTRICT",
        ),
        PrimaryKeyConstraint(
            "candidate_id", "source_id", name="pk_knowledge_candidate_sources"
        ),
        UniqueConstraint(
            "candidate_id",
            "ordinal",
            name="uq_knowledge_candidate_sources_ordinal",
        ),
    )

    candidate_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    source_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    workspace_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    provenance_role: Mapped[str] = mapped_column(String(64), nullable=False)


class KnowledgeCandidateReviewModel(TimestampMixin, Base):
    __tablename__ = "knowledge_candidate_reviews"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('accept', 'reject', 'request_changes')",
            name="ck_knowledge_candidate_reviews_decision",
        ),
        CheckConstraint(
            "observed_candidate_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_candidate_reviews_hash",
        ),
        CheckConstraint(
            "(decision = 'accept' AND created_revision_id IS NOT NULL) OR "
            "(decision <> 'accept' AND created_revision_id IS NULL)",
            name="ck_knowledge_candidate_reviews_created_revision",
        ),
        ForeignKeyConstraint(
            ["candidate_id", "workspace_id"],
            ["knowledge_candidates.id", "knowledge_candidates.workspace_id"],
            name="fk_knowledge_candidate_reviews_candidate_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_revision_id", "workspace_id"],
            ["knowledge_revisions.id", "knowledge_revisions.workspace_id"],
            name="fk_knowledge_candidate_reviews_created_revision_workspace",
            ondelete="RESTRICT",
        ),
        Index("ix_knowledge_candidate_reviews_workspace_candidate", "workspace_id", "candidate_id"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    candidate_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    reviewer_actor: Mapped[str] = mapped_column(String(255), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    observed_candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_target_revision_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    created_revision_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )


class CommandModel(TimestampMixin, Base):
    __tablename__ = "commands"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'applied', 'rejected')",
            name="ck_knowledge_commands_status",
        ),
        CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_commands_payload_hash",
        ),
        UniqueConstraint(
            "workspace_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_knowledge_commands_idempotency",
        ),
        Index("ix_knowledge_commands_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    command_type: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_version: Mapped[int | None] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(128))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EventModel(TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            name="uq_knowledge_events_aggregate_version",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_knowledge_events_id_workspace"),
        Index("ix_knowledge_events_workspace_occurred", "workspace_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    causation_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))


class OutboxMessageModel(TimestampMixin, Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        CheckConstraint(
            "publish_attempts >= 0", name="ck_knowledge_outbox_publish_attempts"
        ),
        CheckConstraint(
            "published_at IS NULL OR dead_lettered_at IS NULL",
            name="ck_knowledge_outbox_terminal_state",
        ),
        UniqueConstraint("event_id", name="uq_knowledge_outbox_event"),
        ForeignKeyConstraint(
            ["event_id", "workspace_id"],
            ["events.id", "events.workspace_id"],
            name="fk_knowledge_outbox_event_workspace",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_knowledge_outbox_available",
            "workspace_id",
            "available_at",
            "published_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    event_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
