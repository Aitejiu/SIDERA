"""Allow complete worker instructions in execution purposes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_execution_purpose_text"
down_revision: str | None = "0032_r6_real_run_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "executions",
        "purpose",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "executions",
        "purpose",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
