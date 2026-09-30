from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_NAME = "sidera_configuration"


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA_NAME)


class ConnectionRow(Base):
    __tablename__ = "connections"
    __table_args__ = (
        CheckConstraint("head_revision >= 1", name="ck_connections_head_revision"),
        CheckConstraint("version >= 1", name="ck_connections_version"),
    )

    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    head_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CredentialRow(Base):
    __tablename__ = "credentials"
    __table_args__ = (
        CheckConstraint(
            "source IN ('macos_keychain', 'env')",
            name="ck_credentials_source",
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'revoked', 'failed')",
            name="ck_credentials_status",
        ),
        CheckConstraint(
            "(source = 'env' AND env_name IS NOT NULL) OR "
            "(source = 'macos_keychain' AND env_name IS NULL)",
            name="ck_credentials_env_name_source",
        ),
        CheckConstraint("version >= 1", name="ck_credentials_version"),
        ForeignKeyConstraint(
            ["workspace_id", "replaces_id"],
            [f"{SCHEMA_NAME}.credentials.workspace_id", f"{SCHEMA_NAME}.credentials.id"],
            name="fk_credentials_replaces",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    alias: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    env_name: Mapped[str | None] = mapped_column(String(128))
    replaces_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConnectionRevisionRow(Base):
    __tablename__ = "connection_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_connection_revisions_revision"),
        CheckConstraint(
            "cardinality(allowed_roles) > 0 AND "
            "allowed_roles <@ ARRAY['main_agent', 'worker', 'planner']::text[]",
            name="ck_connection_revisions_allowed_roles",
        ),
        CheckConstraint(
            "protocol = 'openai_compatible'",
            name="ck_connection_revisions_protocol",
        ),
        CheckConstraint(
            "network_scope IN ('public', 'loopback')",
            name="ck_connection_revisions_network_scope",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "connection_id"],
            [f"{SCHEMA_NAME}.connections.workspace_id", f"{SCHEMA_NAME}.connections.id"],
            name="fk_connection_revisions_connection",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "credential_id"],
            [f"{SCHEMA_NAME}.credentials.workspace_id", f"{SCHEMA_NAME}.credentials.id"],
            name="fk_connection_revisions_credential",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    connection_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    network_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    allowed_roles: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    credential_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CredentialWriteRow(Base):
    __tablename__ = "credential_writes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'completed', 'failed', 'unknown')",
            name="ck_credential_writes_status",
        ),
        UniqueConstraint(
            "workspace_id",
            "credential_id",
            name="uq_credential_writes_credential",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "credential_id"],
            [f"{SCHEMA_NAME}.credentials.workspace_id", f"{SCHEMA_NAME}.credentials.id"],
            name="fk_credential_writes_credential",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    credential_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CredentialEventRow(Base):
    __tablename__ = "credential_events"
    __table_args__ = (
        CheckConstraint(
            "type IN ('created', 'rotated', 'revoked', 'write_failed', 'write_unknown')",
            name="ck_credential_events_type",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "credential_id"],
            [f"{SCHEMA_NAME}.credentials.workspace_id", f"{SCHEMA_NAME}.credentials.id"],
            name="fk_credential_events_credential",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            [f"{SCHEMA_NAME}.credential_writes.workspace_id", f"{SCHEMA_NAME}.credential_writes.operation_id"],
            name="fk_credential_events_write",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    credential_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    operation_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
