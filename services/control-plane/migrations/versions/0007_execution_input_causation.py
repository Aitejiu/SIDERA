"""Persist the command that accepted an ExecutionInput.

Revision ID: 0007_execution_input_causation
Revises: 0006_r3_immutable_timestamps
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_execution_input_causation"
down_revision: str | None = "0006_r3_immutable_timestamps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "execution_inputs",
        sa.Column(
            "source_command_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("commands.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("execution_inputs", "source_command_id")
