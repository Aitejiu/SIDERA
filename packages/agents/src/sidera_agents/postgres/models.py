from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ProfileHead(Base):
    __tablename__ = "profile_heads"
    __table_args__ = ({"schema": "agents"},)

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_version: Mapped[int] = mapped_column(Integer, nullable=False)


class ProfileRevisionRow(Base):
    __tablename__ = "profile_revisions"
    __table_args__ = ({"schema": "agents"},)

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ResourceHead(Base):
    __tablename__ = "resource_heads"
    __table_args__ = ({"schema": "agents"},)

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_version: Mapped[int] = mapped_column(Integer, nullable=False)


class ResourceRevisionRow(Base):
    __tablename__ = "resource_revisions"
    __table_args__ = ({"schema": "agents"},)

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RuntimeInstallationRow(Base):
    __tablename__ = "runtime_installations"
    __table_args__ = (
        UniqueConstraint("harness_id", "executable"),
        {"schema": "agents"},
    )

    installation_key: Mapped[str] = mapped_column(String(71), primary_key=True)
    harness_id: Mapped[str] = mapped_column(String(80), nullable=False)
    executable: Mapped[str] = mapped_column(Text, nullable=False)
    max_active: Mapped[int] = mapped_column(Integer, nullable=False)
    max_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    active_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    session_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RuntimeReferenceRow(Base):
    __tablename__ = "runtime_references"
    __table_args__ = (
        UniqueConstraint("workspace_id", "harness_id", "executable"),
        {"schema": "agents"},
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    runtime_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    installation_key: Mapped[str] = mapped_column(String(71), nullable=False)
    harness_id: Mapped[str] = mapped_column(String(80), nullable=False)
    executable: Mapped[str] = mapped_column(Text, nullable=False)
    actual_version: Mapped[str] = mapped_column(String(120), nullable=False)
    installed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    protocol_state: Mapped[str] = mapped_column(String(24), nullable=False)
    auth_state: Mapped[str] = mapped_column(String(24), nullable=False)
    capabilities: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    file_identity: Mapped[str | None] = mapped_column(String(160), nullable=True)


class RunOperationRow(Base):
    __tablename__ = "run_operations"
    __table_args__ = ({"schema": "agents"},)

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    profile_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_config_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(120), nullable=True)


class AgentRunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "operation_id"),
        {"schema": "agents"},
    )

    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    profile_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    profile_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    runtime_workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    runtime_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    installation_key: Mapped[str] = mapped_column(String(71), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    native_handle: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    terminal_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    terminal_reason_code: Mapped[str | None] = mapped_column(String(120), nullable=True)


class RunReservationRow(Base):
    __tablename__ = "run_reservations"
    __table_args__ = ({"schema": "agents"},)

    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    profile_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    installation_key: Mapped[str] = mapped_column(String(71), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    session: Mapped[bool] = mapped_column(Boolean, nullable=False)
