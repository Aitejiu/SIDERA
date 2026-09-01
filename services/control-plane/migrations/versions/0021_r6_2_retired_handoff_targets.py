"""Allow retired handoff target requests to retain audit lineage.

Revision ID: 0021_r6_2_retired_handoff_targets
Revises: 0020_r6_2_workspace_transfer_outcome
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_r6_2_retired_handoff_targets"
down_revision: str | None = "0020_r6_2_workspace_transfer_outcome"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_MATERIALIZED_LINEAGE_CHECK = (
    "(status = 'materialized' AND execution_lineage_id IS NOT NULL "
    "AND first_execution_id IS NOT NULL) OR "
    "(status = 'obsolete' AND handoff_transition_id IS NOT NULL "
    "AND execution_lineage_id IS NOT NULL "
    "AND first_execution_id IS NOT NULL) OR "
    "(status IN ('blocked', 'ready', 'cancelled', 'obsolete') "
    "AND execution_lineage_id IS NULL AND first_execution_id IS NULL)"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_execution_requests_materialized_lineage",
        "execution_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_execution_requests_materialized_lineage",
        "execution_requests",
        _MATERIALIZED_LINEAGE_CHECK,
    )
    op.drop_index(
        "uq_execution_requests_handoff_transition",
        table_name="execution_requests",
    )
    op.create_index(
        "uq_execution_requests_handoff_transition",
        "execution_requests",
        ["workspace_id", "handoff_transition_id"],
        unique=True,
        postgresql_where=sa.text(
            "handoff_transition_id IS NOT NULL AND status = 'materialized'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_execution_requests_handoff_transition",
        table_name="execution_requests",
    )
    op.create_index(
        "uq_execution_requests_handoff_transition",
        "execution_requests",
        ["workspace_id", "handoff_transition_id"],
        unique=True,
        postgresql_where=sa.text("handoff_transition_id IS NOT NULL"),
    )
    op.drop_constraint(
        "ck_execution_requests_materialized_lineage",
        "execution_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_execution_requests_materialized_lineage",
        "execution_requests",
        "(status = 'materialized' AND execution_lineage_id IS NOT NULL "
        "AND first_execution_id IS NOT NULL) OR "
        "(status <> 'materialized' AND execution_lineage_id IS NULL "
        "AND first_execution_id IS NULL)",
    )
