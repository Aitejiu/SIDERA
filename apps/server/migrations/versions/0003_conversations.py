import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_conversations"
down_revision = "0002_local_authority_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(200), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("context_refs", JSONB(), nullable=False),
        sa.Column("profile_ref", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active_turn_id", sa.Uuid(), nullable=True),
        sa.Column("grant_id", sa.Uuid(), nullable=True),
        sa.Column("start_operation_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("execution_workspace", JSONB(), nullable=True),
        sa.Column("stop_operation_id", sa.Uuid(), nullable=True),
        sa.Column("stop_state", sa.String(16), nullable=True),
        sa.CheckConstraint("version > 0", name="conversation_version_positive"),
        sa.CheckConstraint(
            "stop_state IS NULL OR stop_state IN "
            "('prepared', 'dispatching', 'accepted', 'unknown', 'stopped', 'failed')",
            name="conversation_stop_state",
        ),
        schema="product",
    )
    op.create_table(
        "chat_turns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(200), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("native_operation_id", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("request_state", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("dispatch_attempt", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("native_request", JSONB(), nullable=True),
        sa.Column("response_id", sa.Uuid(), nullable=True),
        sa.Column("reason_code", sa.String(100), nullable=True),
        sa.Column("cached_view", JSONB(), nullable=True),
        sa.Column("interrupt_operation_id", sa.Uuid(), nullable=True),
        sa.Column("interrupt_state", sa.String(16), nullable=True),
        sa.UniqueConstraint(
            "workspace_id",
            "conversation_id",
            "operation_id",
            name="chat_turn_original_operation",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "conversation_id",
            "native_operation_id",
            name="chat_turn_native_operation",
        ),
        sa.CheckConstraint("kind IN ('start', 'input')", name="chat_turn_kind"),
        sa.CheckConstraint("version > 0", name="chat_turn_version_positive"),
        sa.CheckConstraint(
            "dispatch_attempt >= 0", name="chat_turn_attempt_nonnegative"
        ),
        sa.CheckConstraint(
            "request_state IN ('claimed', 'prepared', 'dispatching', 'accepted', "
            "'delivered', 'busy', 'rejected', 'unknown')",
            name="chat_turn_request_state",
        ),
        schema="product",
    )


def downgrade() -> None:
    raise RuntimeError("Product delivery migrations are forward-only")
