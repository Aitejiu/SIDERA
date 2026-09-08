"""Persist bounded real result-integration execution reservations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_r6_real_result_budget"
down_revision: str | None = "0030_r6_verification_request_uniqueness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid() -> sa.types.TypeEngine:
    return sa.dialects.postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "real_result_run_budgets",
        sa.Column("run_id", _uuid(), nullable=False),
        sa.Column("max_executions", sa.Integer(), nullable=False),
        sa.Column(
            "reserved_executions",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "max_executions >= 1 AND reserved_executions >= 0 "
            "AND reserved_executions <= max_executions",
            name="ck_real_result_run_budgets_counts",
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_real_result_run_budgets"),
    )
    op.create_table(
        "real_result_execution_reservations",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("run_id", _uuid(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("step_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sequence_no >= 1",
            name="ck_real_result_execution_reservations_sequence",
        ),
        sa.CheckConstraint(
            "status IN ('started', 'succeeded', 'unknown')",
            name="ck_real_result_execution_reservations_status",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["real_result_run_budgets.run_id"],
            ondelete="RESTRICT",
            name="fk_real_result_execution_reservations_run",
        ),
        sa.PrimaryKeyConstraint(
            "id", name="pk_real_result_execution_reservations"
        ),
        sa.UniqueConstraint(
            "run_id",
            "sequence_no",
            name="uq_real_result_execution_reservations_sequence",
        ),
        sa.UniqueConstraint(
            "run_id",
            "step_key",
            name="uq_real_result_execution_reservations_step",
        ),
    )
    op.create_index(
        "ix_real_result_execution_reservations_run",
        "real_result_execution_reservations",
        ["run_id", "sequence_no"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_real_result_execution_reservations_run",
        table_name="real_result_execution_reservations",
    )
    op.drop_table("real_result_execution_reservations")
    op.drop_table("real_result_run_budgets")
