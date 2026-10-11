from alembic import op
from sqlalchemy import Boolean, Column, Integer, JSON, String, UniqueConstraint, Uuid

revision = "0003_agent_runs_operations"
down_revision = "0002_runtime_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_operations",
        Column("workspace_id", Uuid(as_uuid=True), primary_key=True),
        Column("operation_id", Uuid(as_uuid=True), primary_key=True),
        Column("profile_id", Uuid(as_uuid=True), nullable=False),
        Column("profile_revision", Integer, nullable=False),
        Column("profile_config_hash", String(71), nullable=False),
        Column("fingerprint", String(71), nullable=False),
        Column("status", String(24), nullable=False),
        Column("run_id", Uuid(as_uuid=True), nullable=True),
        Column("reason_code", String(120), nullable=True),
        schema="agents",
    )
    op.create_table(
        "runs",
        Column("run_id", Uuid(as_uuid=True), primary_key=True),
        Column("workspace_id", Uuid(as_uuid=True), nullable=False),
        Column("operation_id", Uuid(as_uuid=True), nullable=False),
        Column("profile_id", Uuid(as_uuid=True), nullable=False),
        Column("profile_revision", Integer, nullable=False),
        Column("runtime_workspace_id", Uuid(as_uuid=True), nullable=False),
        Column("runtime_id", Uuid(as_uuid=True), nullable=False),
        Column("installation_key", String(71), nullable=False),
        Column("state", String(24), nullable=False),
        Column("snapshot", JSON, nullable=False),
        Column("native_handle", JSON, nullable=True),
        Column("terminal_state", String(24), nullable=True),
        Column("terminal_reason_code", String(120), nullable=True),
        UniqueConstraint("workspace_id", "operation_id"),
        schema="agents",
    )
    op.create_table(
        "run_reservations",
        Column("run_id", Uuid(as_uuid=True), primary_key=True),
        Column("workspace_id", Uuid(as_uuid=True), nullable=False),
        Column("profile_id", Uuid(as_uuid=True), nullable=False),
        Column("installation_key", String(71), nullable=False),
        Column("active", Boolean, nullable=False),
        Column("session", Boolean, nullable=False),
        schema="agents",
    )


def downgrade() -> None:
    op.drop_table("run_reservations", schema="agents")
    op.drop_table("runs", schema="agents")
    op.drop_table("run_operations", schema="agents")
