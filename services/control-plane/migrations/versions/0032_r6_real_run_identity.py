"""Persist real-run identity, deadline and execution/session bindings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_r6_real_run_identity"
down_revision: str | None = "0031_r6_real_result_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid() -> sa.types.TypeEngine:
    return sa.dialects.postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column(
        "real_result_run_budgets",
        sa.Column("profile_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "real_result_run_budgets",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "real_result_execution_reservations",
        sa.Column("execution_id", _uuid(), nullable=True),
    )
    op.add_column(
        "real_result_execution_reservations",
        sa.Column("session_id", _uuid(), nullable=True),
    )
    op.create_check_constraint(
        "ck_real_result_execution_reservations_binding_pair",
        "real_result_execution_reservations",
        "(execution_id IS NULL AND session_id IS NULL) OR "
        "(execution_id IS NOT NULL AND session_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_real_result_execution_reservations_execution",
        "real_result_execution_reservations",
        "executions",
        ["execution_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_real_result_execution_reservations_session",
        "real_result_execution_reservations",
        "execution_sessions",
        ["session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_real_result_execution_reservations_execution",
        "real_result_execution_reservations",
        ["execution_id"],
        unique=True,
        postgresql_where=sa.text("execution_id IS NOT NULL"),
    )
    op.create_index(
        "uq_real_result_execution_reservations_session",
        "real_result_execution_reservations",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_real_result_execution_reservations_session",
        table_name="real_result_execution_reservations",
    )
    op.drop_index(
        "uq_real_result_execution_reservations_execution",
        table_name="real_result_execution_reservations",
    )
    op.drop_constraint(
        "fk_real_result_execution_reservations_session",
        "real_result_execution_reservations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_real_result_execution_reservations_execution",
        "real_result_execution_reservations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_real_result_execution_reservations_binding_pair",
        "real_result_execution_reservations",
        type_="check",
    )
    op.drop_column("real_result_execution_reservations", "session_id")
    op.drop_column("real_result_execution_reservations", "execution_id")
    op.drop_column("real_result_run_budgets", "expires_at")
    op.drop_column("real_result_run_budgets", "profile_fingerprint")
