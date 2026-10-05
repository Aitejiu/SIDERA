from datetime import datetime
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(schema="product")


class ProductDefaultsRow(Base):
    __tablename__ = "product_defaults"
    __table_args__ = (CheckConstraint("version > 0", name="defaults_version_positive"),)

    workspace_id: Mapped[UUID] = mapped_column(primary_key=True)
    main_profile_id: Mapped[UUID | None]
    planner_profile_id: Mapped[UUID | None]
    worker_profile_id: Mapped[UUID | None]
    version: Mapped[int]


class BrowserSessionRow(Base):
    __tablename__ = "browser_sessions"

    session_id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID]
    principal_id: Mapped[str] = mapped_column(String(200))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RequestStateRow(Base):
    __tablename__ = "request_state"

    request_id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID]
    principal_id: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LocalBootstrapRow(Base):
    __tablename__ = "local_bootstraps"

    bootstrap_id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID]
    principal_id: Mapped[str] = mapped_column(String(200))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentGrantRow(Base):
    __tablename__ = "agent_grants"
    __table_args__ = (
        CheckConstraint(
            "source IN ('product', 'control_plane')", name="agent_grant_source"
        ),
        CheckConstraint(
            "role IN ('main', 'worker', 'planner')", name="agent_grant_role"
        ),
        CheckConstraint(
            "(source = 'product' AND role = 'main' AND parent_session_id IS NOT NULL "
            "AND native_grant_id IS NULL) OR (source = 'control_plane' AND role IN "
            "('worker', 'planner') AND parent_session_id IS NULL AND native_grant_id "
            "IS NOT NULL)",
            name="agent_grant_role_source",
        ),
        UniqueConstraint(
            "workspace_id",
            "start_operation_id",
            "connection_id",
            name="agent_grant_original_connection",
        ),
    )

    grant_id: Mapped[UUID] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    role: Mapped[str] = mapped_column(String(16))
    workspace_id: Mapped[UUID]
    principal_id: Mapped[str] = mapped_column(String(200))
    profile_ref: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    start_operation_id: Mapped[UUID]
    native_grant_id: Mapped[UUID | None]
    connection_id: Mapped[UUID]
    parent_session_id: Mapped[UUID | None]
    run_id: Mapped[UUID | None]
    root_task_id: Mapped[UUID | None]
    execution_id: Mapped[UUID | None]
    worker_item_id: Mapped[UUID | None]
    execution_version: Mapped[int | None]
    binding_version: Mapped[int | None]
    planning_request_id: Mapped[UUID | None]
    planning_run_id: Mapped[UUID | None]
    lease_id: Mapped[UUID | None]
    fencing_token: Mapped[int | None] = mapped_column(BigInteger)
    authority_refs: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bearer_digest: Mapped[str | None] = mapped_column(String(64), unique=True)


class ToolReceiptRow(Base):
    __tablename__ = "tool_receipts"
    __table_args__ = (
        UniqueConstraint(
            "grant_id", "tool_name", "logical_request_id", name="tool_receipt_intent"
        ),
        CheckConstraint(
            "outcome IN ('dispatching', 'applied', 'rejected', 'unknown')",
            name="tool_receipt_outcome",
        ),
    )

    receipt_id: Mapped[UUID] = mapped_column(primary_key=True)
    grant_id: Mapped[UUID]
    run_id: Mapped[UUID]
    response_id: Mapped[UUID]
    tool_name: Mapped[str] = mapped_column(String(100))
    logical_request_id: Mapped[UUID]
    payload_hash: Mapped[str] = mapped_column(String(64))
    command_id: Mapped[UUID] = mapped_column(unique=True)
    intent: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    outcome: Mapped[str] = mapped_column(String(16))
    result: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConversationRow(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("version > 0", name="conversation_version_positive"),
        CheckConstraint(
            "stop_state IS NULL OR stop_state IN "
            "('prepared', 'dispatching', 'accepted', 'unknown', 'stopped', 'failed')",
            name="conversation_stop_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID]
    principal_id: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(120))
    context_refs: Mapped[list[str]] = mapped_column(JSONB)
    profile_ref: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int]
    active_turn_id: Mapped[UUID | None]
    grant_id: Mapped[UUID | None]
    start_operation_id: Mapped[UUID | None]
    run_id: Mapped[UUID | None]
    execution_workspace: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    stop_operation_id: Mapped[UUID | None]
    stop_state: Mapped[str | None] = mapped_column(String(16))


class ChatTurnRow(Base):
    __tablename__ = "chat_turns"
    __table_args__ = (
        CheckConstraint("version > 0", name="chat_turn_version_positive"),
        CheckConstraint("dispatch_attempt >= 0", name="chat_turn_attempt_nonnegative"),
        UniqueConstraint(
            "workspace_id",
            "conversation_id",
            "operation_id",
            name="chat_turn_original_operation",
        ),
        UniqueConstraint(
            "workspace_id",
            "conversation_id",
            "native_operation_id",
            name="chat_turn_native_operation",
        ),
        CheckConstraint("kind IN ('start', 'input')", name="chat_turn_kind"),
        CheckConstraint(
            "request_state IN ('claimed', 'prepared', 'dispatching', 'accepted', "
            "'delivered', 'busy', 'rejected', 'unknown')",
            name="chat_turn_request_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    principal_id: Mapped[str] = mapped_column(String(200))
    operation_id: Mapped[str] = mapped_column(String(128))
    native_operation_id: Mapped[UUID]
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    input_text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(8))
    request_state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int]
    dispatch_attempt: Mapped[int]
    native_request: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    response_id: Mapped[UUID | None]
    reason_code: Mapped[str | None] = mapped_column(String(100))
    cached_view: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    interrupt_operation_id: Mapped[UUID | None]
    interrupt_state: Mapped[str | None] = mapped_column(String(16))
