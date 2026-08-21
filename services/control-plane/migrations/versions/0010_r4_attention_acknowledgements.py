"""Persist actor-local attention acknowledgements.

Revision ID: 0010_r4_attention_ack
Revises: 0009_r4_plan_projection
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_r4_attention_ack"
down_revision: str | None = "0009_r4_plan_projection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attention_acknowledgements",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_type", sa.String(length=50), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("attention_key", sa.String(length=500), nullable=False),
        sa.Column("root_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source_correlation_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("source_resource_version", sa.BigInteger(), nullable=False),
        sa.Column(
            "acknowledged_command_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_resource_version >= 1",
            name="ck_attention_ack_source_resource_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_attention_ack_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_attention_ack_root_task_workspace",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id", "actor_type", "actor_id", "attention_key"
        ),
        sa.Index(
            "ix_attention_ack_root_acknowledged",
            "workspace_id",
            "root_task_id",
            "acknowledged_at",
        ),
    )


def downgrade() -> None:
    op.drop_table("attention_acknowledgements")
