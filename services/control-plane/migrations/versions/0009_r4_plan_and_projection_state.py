"""Add immutable R4 plan snapshots and durable projection state.

Revision ID: 0009_r4_plan_projection
Revises: 0008_execution_stop_provenance
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_r4_plan_projection"
down_revision: str | None = "0008_execution_stop_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_plan_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("parent_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_type", sa.String(length=100), nullable=False),
        sa.Column("source_ref", sa.String(length=500), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("goal_summary", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("published_by", sa.String(length=255), nullable=False),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.CheckConstraint(
            "generation >= 1", name="ck_plan_snapshots_generation"
        ),
        sa.CheckConstraint(
            "schema_version >= 1", name="ck_plan_snapshots_schema_version"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_plan_snapshots_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_snapshots_root_task_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["parent_snapshot_id"],
            ["task_plan_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_plan_snapshots_parent",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "root_task_id", "generation", name="uq_plan_snapshots_root_generation"
        ),
        sa.Index(
            "ix_plan_snapshots_workspace_root_generation",
            "workspace_id",
            "root_task_id",
            "generation",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "workspace_projection_cursors",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_cursor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_projection_cursors_workspace",
        ),
        sa.PrimaryKeyConstraint("workspace_id"),
    )
    op.create_table(
        "projection_changes",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cursor", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("projection_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("change_type", sa.String(length=50), nullable=False),
        sa.Column("resource_version", sa.BigInteger(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint("cursor >= 1", name="ck_projection_changes_cursor"),
        sa.CheckConstraint(
            "schema_version >= 1", name="ck_projection_changes_schema_version"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_projection_changes_workspace",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "cursor"),
        sa.Index(
            "ix_projection_changes_workspace_occurred",
            "workspace_id",
            "occurred_at",
        ),
    )


def downgrade() -> None:
    op.drop_table("projection_changes")
    op.drop_table("workspace_projection_cursors")
    op.drop_table("task_plan_snapshots")
