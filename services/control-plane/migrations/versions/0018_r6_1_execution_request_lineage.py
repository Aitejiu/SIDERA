"""Add source lineage to execution requests.

Revision ID: 0018_r6_1_execution_request_lineage
Revises: 0017_r6_1_plan_revisions
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_r6_1_execution_request_lineage"
down_revision: str | None = "0017_r6_1_plan_revisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "execution_requests",
        sa.Column("source_execution_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "execution_requests",
        sa.Column("source_session_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_execution_requests_source_execution_workspace",
        "execution_requests",
        "executions",
        ["source_execution_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_execution_requests_source_session_workspace",
        "execution_requests",
        "execution_sessions",
        ["source_session_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_execution_requests_source_session_workspace",
        "execution_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_execution_requests_source_execution_workspace",
        "execution_requests",
        type_="foreignkey",
    )
    op.drop_column("execution_requests", "source_session_id")
    op.drop_column("execution_requests", "source_execution_id")
