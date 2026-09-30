"""Create the model connection catalog and credential metadata tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_connections_credentials"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "sidera_configuration"


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("alias", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("env_name", sa.String(length=128), nullable=True),
        sa.Column("replaces_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source IN ('macos_keychain', 'env')",
            name="ck_credentials_source",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'revoked', 'failed')",
            name="ck_credentials_status",
        ),
        sa.CheckConstraint(
            "(source = 'env' AND env_name IS NOT NULL) OR "
            "(source = 'macos_keychain' AND env_name IS NULL)",
            name="ck_credentials_env_name_source",
        ),
        sa.CheckConstraint("version >= 1", name="ck_credentials_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "replaces_id"],
            [f"{SCHEMA}.credentials.workspace_id", f"{SCHEMA}.credentials.id"],
            name="fk_credentials_replaces",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        schema=SCHEMA,
    )
    op.create_table(
        "connections",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("head_revision", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("head_revision >= 1", name="ck_connections_head_revision"),
        sa.CheckConstraint("version >= 1", name="ck_connections_version"),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        schema=SCHEMA,
    )
    op.create_table(
        "connection_revisions",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("protocol", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("network_scope", sa.String(length=16), nullable=False),
        sa.Column("allowed_roles", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_connection_revisions_revision"),
        sa.CheckConstraint(
            "cardinality(allowed_roles) > 0 AND "
            "allowed_roles <@ ARRAY['main_agent', 'worker', 'planner']::text[]",
            name="ck_connection_revisions_allowed_roles",
        ),
        sa.CheckConstraint(
            "protocol = 'openai_compatible'",
            name="ck_connection_revisions_protocol",
        ),
        sa.CheckConstraint(
            "network_scope IN ('public', 'loopback')",
            name="ck_connection_revisions_network_scope",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_id"],
            [f"{SCHEMA}.connections.workspace_id", f"{SCHEMA}.connections.id"],
            name="fk_connection_revisions_connection",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "credential_id"],
            [f"{SCHEMA}.credentials.workspace_id", f"{SCHEMA}.credentials.id"],
            name="fk_connection_revisions_credential",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "connection_id", "revision"),
        schema=SCHEMA,
    )
    op.create_table(
        "credential_writes",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'failed', 'unknown')",
            name="ck_credential_writes_status",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "credential_id"],
            [f"{SCHEMA}.credentials.workspace_id", f"{SCHEMA}.credentials.id"],
            name="fk_credential_writes_credential",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "operation_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "credential_id",
            name="uq_credential_writes_credential",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "credential_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "type IN ('created', 'rotated', 'revoked', 'write_failed', 'write_unknown')",
            name="ck_credential_events_type",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "credential_id"],
            [f"{SCHEMA}.credentials.workspace_id", f"{SCHEMA}.credentials.id"],
            name="fk_credential_events_credential",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            [f"{SCHEMA}.credential_writes.workspace_id", f"{SCHEMA}.credential_writes.operation_id"],
            name="fk_credential_events_write",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("credential_events", schema=SCHEMA)
    op.drop_table("credential_writes", schema=SCHEMA)
    op.drop_table("connection_revisions", schema=SCHEMA)
    op.drop_table("connections", schema=SCHEMA)
    op.drop_table("credentials", schema=SCHEMA)
