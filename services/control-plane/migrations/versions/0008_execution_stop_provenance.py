"""Persist exact stop requests and continuation provenance.

Revision ID: 0008_execution_stop_provenance
Revises: 0007_execution_input_causation
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_execution_stop_provenance"
down_revision: str | None = "0007_execution_input_causation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column(
            "stop_requested",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "executions",
        sa.Column(
            "source_execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("executions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column(
        "executions",
        sa.Column(
            "source_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_sessions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("executions", "source_session_id")
    op.drop_column("executions", "source_execution_id")
    op.drop_column("executions", "stop_requested")
