"""Align the persisted R6.2 handoff lineage constraint with the ORM model.

Revision ID: 0023_r6_3_1_schema_alignment
Revises: 0022_r6_3_1_collaboration_runtime
Create Date: 2026-09-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023_r6_3_1_schema_alignment"
down_revision: str | None = "0022_r6_3_1_collaboration_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_execution_requests_handoff_lineage",
        "execution_requests",
        "handoff_transition_id IS NULL OR "
        "(source_execution_id IS NOT NULL AND source_session_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_execution_requests_handoff_lineage",
        "execution_requests",
        type_="check",
    )
