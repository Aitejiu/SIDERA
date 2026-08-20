"""Persist worker desired scheduling state.

Revision ID: 0004_worker_desired_state
Revises: 0003_workspace_foreign_keys
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_worker_desired_state"
down_revision: str | None = "0003_workspace_foreign_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workers",
        sa.Column(
            "desired_state",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
    )
    op.create_check_constraint(
        "ck_workers_desired_state",
        "workers",
        "desired_state IN ('active', 'draining')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workers_desired_state", "workers", type_="check")
    op.drop_column("workers", "desired_state")
