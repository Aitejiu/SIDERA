from alembic import op
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)

revision = "0002_runtime_catalog"
down_revision = "0001_agent_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_installations",
        Column("installation_key", String(71), primary_key=True),
        Column("harness_id", String(80), nullable=False),
        Column("executable", Text, nullable=False),
        Column("max_active", Integer, nullable=False),
        Column("max_sessions", Integer, nullable=False),
        Column("enabled", Boolean, nullable=False),
        Column("config_version", Integer, nullable=False),
        Column("active_count", Integer, nullable=False),
        Column("session_count", Integer, nullable=False),
        UniqueConstraint("harness_id", "executable"),
        schema="agents",
    )
    op.create_table(
        "runtime_references",
        Column("workspace_id", Uuid(as_uuid=True), primary_key=True),
        Column("runtime_id", Uuid(as_uuid=True), primary_key=True),
        Column("installation_key", String(71), nullable=False),
        Column("harness_id", String(80), nullable=False),
        Column("executable", Text, nullable=False),
        Column("actual_version", String(120), nullable=False),
        Column("installed", Boolean, nullable=False),
        Column("protocol_state", String(24), nullable=False),
        Column("auth_state", String(24), nullable=False),
        Column("capabilities", JSON, nullable=False),
        Column("checked_at", DateTime(timezone=True), nullable=False),
        Column("file_identity", String(160), nullable=True),
        UniqueConstraint("workspace_id", "harness_id", "executable"),
        schema="agents",
    )


def downgrade() -> None:
    op.drop_table("runtime_references", schema="agents")
    op.drop_table("runtime_installations", schema="agents")
