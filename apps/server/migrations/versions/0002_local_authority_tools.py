import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_local_authority_tools"
down_revision = "0001_product_defaults_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "browser_sessions",
        sa.Column("csrf_digest", sa.String(64), nullable=True),
        schema="product",
    )
    op.create_table(
        "local_bootstraps",
        sa.Column("bootstrap_id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(200), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        schema="product",
    )
    op.create_table(
        "agent_grants",
        sa.Column("grant_id", sa.Uuid(), primary_key=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(200), nullable=False),
        sa.Column("profile_ref", JSONB(), nullable=False),
        sa.Column("start_operation_id", sa.Uuid(), nullable=False),
        sa.Column("native_grant_id", sa.Uuid(), nullable=True),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("parent_session_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("root_task_id", sa.Uuid(), nullable=True),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        sa.Column("worker_item_id", sa.Uuid(), nullable=True),
        sa.Column("execution_version", sa.Integer(), nullable=True),
        sa.Column("binding_version", sa.Integer(), nullable=True),
        sa.Column("planning_request_id", sa.Uuid(), nullable=True),
        sa.Column("planning_run_id", sa.Uuid(), nullable=True),
        sa.Column("lease_id", sa.Uuid(), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=True),
        sa.Column("authority_refs", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bearer_digest", sa.String(64), nullable=True, unique=True),
        sa.CheckConstraint(
            "source IN ('product', 'control_plane')", name="agent_grant_source"
        ),
        sa.CheckConstraint(
            "role IN ('main', 'worker', 'planner')", name="agent_grant_role"
        ),
        sa.CheckConstraint(
            "(source = 'product' AND role = 'main' AND parent_session_id IS NOT NULL "
            "AND native_grant_id IS NULL) OR (source = 'control_plane' AND role IN "
            "('worker', 'planner') AND parent_session_id IS NULL AND native_grant_id "
            "IS NOT NULL)",
            name="agent_grant_role_source",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "start_operation_id",
            "connection_id",
            name="agent_grant_original_connection",
        ),
        schema="product",
    )
    op.create_table(
        "tool_receipts",
        sa.Column("receipt_id", sa.Uuid(), primary_key=True),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("response_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("logical_request_id", sa.Uuid(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("command_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("intent", JSONB(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "grant_id", "tool_name", "logical_request_id", name="tool_receipt_intent"
        ),
        sa.CheckConstraint(
            "outcome IN ('dispatching', 'applied', 'rejected', 'unknown')",
            name="tool_receipt_outcome",
        ),
        schema="product",
    )


def downgrade() -> None:
    raise RuntimeError("Product delivery migrations are forward-only")
