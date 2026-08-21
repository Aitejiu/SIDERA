"""Remove mutable timestamp metadata from immutable plan snapshots.

Revision ID: 0011_r4_drop_plan_updated_at
Revises: 0010_r4_attention_ack
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_r4_drop_plan_updated_at"
down_revision: str | None = "0010_r4_attention_ack"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("task_plan_snapshots", "updated_at")


def downgrade() -> None:
    op.add_column(
        "task_plan_snapshots",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
