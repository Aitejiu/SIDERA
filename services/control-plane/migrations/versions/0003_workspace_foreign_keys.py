"""Add workspace ownership foreign keys to R2 tables.

Revision ID: 0003_workspace_foreign_keys
Revises: 0002_worker_execution_lease
Create Date: 2026-08-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_workspace_foreign_keys"
down_revision: str | None = "0002_worker_execution_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_executions_workspace",
        "executions",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_assignment_leases_workspace",
        "assignment_leases",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_execution_sessions_workspace",
        "execution_sessions",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_execution_sessions_workspace",
        "execution_sessions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_assignment_leases_workspace",
        "assignment_leases",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_executions_workspace",
        "executions",
        type_="foreignkey",
    )
