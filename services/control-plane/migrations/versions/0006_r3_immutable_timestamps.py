"""Align immutable R3 records with their ORM timestamp contract.

Revision ID: 0006_r3_immutable_timestamps
Revises: 0005_r3_gateway_state
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_r3_immutable_timestamps"
down_revision: str | None = "0005_r3_gateway_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = (
    "process_descriptors",
    "execution_events",
    "artifacts",
    "evidence_claims",
)


def upgrade() -> None:
    for table_name in _TABLES:
        op.drop_column(table_name, "updated_at")


def downgrade() -> None:
    for table_name in reversed(_TABLES):
        op.add_column(
            table_name,
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
