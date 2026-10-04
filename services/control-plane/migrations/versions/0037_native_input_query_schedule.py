"""Rotate pending queries without changing input business versions."""

import sqlalchemy as sa
from alembic import op

revision = "0037_native_input_query_schedule"
down_revision = "0036_native_planning_and_submitted_goals"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "execution_inputs",
        sa.Column("last_query_selected_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    raise RuntimeError("native input history cannot be downgraded")
