from datetime import datetime
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    MetaData,
    String,
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
