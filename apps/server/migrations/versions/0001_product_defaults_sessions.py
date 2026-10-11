import sqlalchemy as sa
from alembic import op

revision = "0001_product_defaults_sessions"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_defaults",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("main_profile_id", sa.Uuid(), nullable=True),
        sa.Column("planner_profile_id", sa.Uuid(), nullable=True),
        sa.Column("worker_profile_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("version > 0", name="defaults_version_positive"),
        schema="product",
    )
    op.create_table(
        "browser_sessions",
        sa.Column("session_id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(200), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        schema="product",
    )
    op.create_table(
        "request_state",
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(200), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="product",
    )


def downgrade() -> None:
    raise RuntimeError("Product delivery migrations are forward-only")
