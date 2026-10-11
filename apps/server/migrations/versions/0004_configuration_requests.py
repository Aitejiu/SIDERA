import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_configuration_requests"
down_revision = "0003_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "configuration_requests",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("principal_id", sa.String(200), primary_key=True),
        sa.Column("operation_id", sa.String(128), primary_key=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("draft_fingerprint", sa.String(64), nullable=False),
        sa.Column("target_ref", JSONB(), nullable=False),
        sa.Column("expected_ref", JSONB(none_as_null=True), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason_code", sa.String(100), nullable=True),
        sa.Column("result_ref", JSONB(none_as_null=True), nullable=True),
        sa.CheckConstraint(
            "action IN ('profile.create', 'profile.revise', 'resource.create', 'resource.revise')",
            name="configuration_action",
        ),
        sa.CheckConstraint(
            "state IN ('claimed', 'dispatching', 'succeeded', 'reconciled', 'rejected', 'unknown')",
            name="configuration_state",
        ),
        sa.CheckConstraint("version > 0", name="configuration_version_positive"),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$' AND draft_fingerprint ~ '^[0-9a-f]{64}$'",
            name="configuration_fingerprints",
        ),
        schema="product",
    )


def downgrade() -> None:
    raise RuntimeError("Product delivery migrations are forward-only")
