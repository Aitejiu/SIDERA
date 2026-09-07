"""Allow verification-derived requests beside the original WorkerItem request."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_r6_verification_request_uniqueness"
down_revision: str | None = "0029_r6_result_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "uq_execution_requests_proposal_worker_item",
        table_name="execution_requests",
    )
    op.create_index(
        "uq_execution_requests_proposal_worker_item",
        "execution_requests",
        ["proposal_revision_id", "worker_item_key"],
        unique=True,
        postgresql_where=sa.text(
            "handoff_transition_id IS NULL AND result_derivation_kind IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_execution_requests_proposal_worker_item",
        table_name="execution_requests",
    )
    op.create_index(
        "uq_execution_requests_proposal_worker_item",
        "execution_requests",
        ["proposal_revision_id", "worker_item_key"],
        unique=True,
        postgresql_where=sa.text("handoff_transition_id IS NULL"),
    )
