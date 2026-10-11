from alembic import op
from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, String, Uuid
from sqlalchemy.schema import CreateSchema

revision = "0001_agent_catalog"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(CreateSchema("agents", if_not_exists=True))
    op.create_table(
        "profile_heads",
        Column("workspace_id", Uuid(as_uuid=True), primary_key=True),
        Column("profile_id", Uuid(as_uuid=True), primary_key=True),
        Column("revision", Integer, nullable=False),
        Column("enabled", Boolean, nullable=False),
        Column("status_version", Integer, nullable=False),
        schema="agents",
    )
    op.create_table(
        "profile_revisions",
        Column("workspace_id", Uuid(as_uuid=True), primary_key=True),
        Column("profile_id", Uuid(as_uuid=True), primary_key=True),
        Column("revision", Integer, primary_key=True),
        Column("draft", JSON, nullable=False),
        Column("config_hash", String(71), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        schema="agents",
    )
    op.create_table(
        "resource_heads",
        Column("workspace_id", Uuid(as_uuid=True), primary_key=True),
        Column("resource_id", Uuid(as_uuid=True), primary_key=True),
        Column("revision", Integer, nullable=False),
        Column("enabled", Boolean, nullable=False),
        Column("status_version", Integer, nullable=False),
        schema="agents",
    )
    op.create_table(
        "resource_revisions",
        Column("workspace_id", Uuid(as_uuid=True), primary_key=True),
        Column("resource_id", Uuid(as_uuid=True), primary_key=True),
        Column("revision", Integer, primary_key=True),
        Column("draft", JSON, nullable=False),
        Column("content_hash", String(71), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        schema="agents",
    )


def downgrade() -> None:
    op.drop_table("resource_revisions", schema="agents")
    op.drop_table("resource_heads", schema="agents")
    op.drop_table("profile_revisions", schema="agents")
    op.drop_table("profile_heads", schema="agents")
