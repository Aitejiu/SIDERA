"""Persist stable WorkerItems and immutable plan placements.

Revision ID: 0012_r4_1_worker_items
Revises: 0011_r4_drop_plan_updated_at
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_r4_1_worker_items"
down_revision: str | None = "0011_r4_drop_plan_updated_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stable_key", sa.String(length=500), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("responsibility", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("context_version", sa.Integer(), nullable=False),
        sa.Column("acknowledged_context_version", sa.Integer(), nullable=False),
        sa.Column(
            "derived_from_worker_item_ids",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
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
            "status IN ('active', 'paused', 'retired')",
            name="ck_worker_items_status",
        ),
        sa.CheckConstraint(
            "context_version >= 1", name="ck_worker_items_context_version"
        ),
        sa.CheckConstraint(
            "acknowledged_context_version >= 0 AND "
            "acknowledged_context_version <= context_version",
            name="ck_worker_items_ack_context_version",
        ),
        sa.CheckConstraint("version >= 1", name="ck_worker_items_version"),
        sa.CheckConstraint(
            "jsonb_typeof(derived_from_worker_item_ids) = 'array'",
            name="ck_worker_items_derived_ids_array",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_worker_items_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_items_root_task_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_worker_items_id_workspace"),
        sa.UniqueConstraint(
            "root_task_id", "stable_key", name="uq_worker_items_root_stable_key"
        ),
        sa.Index(
            "ix_worker_items_workspace_root_status",
            "workspace_id",
            "root_task_id",
            "status",
        ),
    )

    op.create_table(
        "worker_item_placements",
        sa.Column("plan_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_item_key", sa.String(length=500), nullable=False),
        sa.Column("subtask_key", sa.String(length=500), nullable=False),
        sa.Column("subtask_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.String(length=255), nullable=False),
        sa.Column("capability_requirements", postgresql.JSONB(), nullable=False),
        sa.Column("worker_policy_ref", sa.String(length=500), nullable=True),
        sa.Column("configuration_ref", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "plan_generation >= 1",
            name="ck_worker_item_placements_generation",
        ),
        sa.ForeignKeyConstraint(
            ["plan_snapshot_id"],
            ["task_plan_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_worker_item_placements_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_worker_item_placements_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_item_placements_root_task",
        ),
        sa.ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_item_placements_worker_item_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["subtask_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_item_placements_subtask_task_workspace",
        ),
        sa.PrimaryKeyConstraint("plan_snapshot_id", "worker_item_id"),
        sa.UniqueConstraint(
            "plan_snapshot_id",
            "worker_item_key",
            name="uq_worker_item_placements_snapshot_key",
        ),
        sa.Index(
            "ix_worker_item_placements_workspace_root_generation",
            "workspace_id",
            "root_task_id",
            "plan_generation",
        ),
        sa.Index(
            "ix_worker_item_placements_workspace_worker_item_generation",
            "workspace_id",
            "worker_item_id",
            "plan_generation",
        ),
    )

    op.add_column(
        "executions",
        sa.Column("worker_item_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_executions_worker_item_workspace",
        "executions",
        "worker_items",
        ["worker_item_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_executions_worker_item_workspace",
        "executions",
        type_="foreignkey",
    )
    op.drop_column("executions", "worker_item_id")
    op.drop_table("worker_item_placements")
    op.drop_table("worker_items")
