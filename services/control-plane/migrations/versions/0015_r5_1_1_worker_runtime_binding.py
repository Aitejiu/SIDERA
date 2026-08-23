"""Add worker runtime bindings and scheduling block episodes.

Revision ID: 0015_r5_1_1_worker_runtime_binding
Revises: 0014_r5_1_execution_requests
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_r5_1_1_worker_runtime_binding"
down_revision: str | None = "0014_r5_1_execution_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.add_column(
        "executions",
        sa.Column(
            "adapter_preferences",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "executions",
        sa.Column(
            "adapter_forbidden",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_executions_adapter_preferences_array",
        "executions",
        "jsonb_typeof(adapter_preferences) = 'array'",
    )
    op.create_check_constraint(
        "ck_executions_adapter_forbidden_array",
        "executions",
        "jsonb_typeof(adapter_forbidden) = 'array'",
    )

    op.add_column(
        "execution_requests",
        sa.Column(
            "adapter_forbidden",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_execution_requests_adapter_forbidden_array",
        "execution_requests",
        "jsonb_typeof(adapter_forbidden) = 'array'",
    )

    op.add_column(
        "module_definitions",
        sa.Column(
            "resource_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_check_constraint(
        "ck_module_definitions_resource_version",
        "module_definitions",
        "resource_version >= 1",
    )
    op.add_column(
        "module_instances",
        sa.Column(
            "resource_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_check_constraint(
        "ck_module_instances_resource_version",
        "module_instances",
        "resource_version >= 1",
    )

    op.create_table(
        "worker_runtime_configurations",
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "adapter_module_instance_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("adapter_module_config_version", sa.Integer(), nullable=False),
        sa.Column(
            "workspace_module_instance_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("workspace_module_config_version", sa.Integer(), nullable=False),
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
            "adapter_module_config_version >= 1",
            name="ck_worker_runtime_configurations_adapter_config_version",
        ),
        sa.CheckConstraint(
            "workspace_module_config_version >= 1",
            name="ck_worker_runtime_configurations_workspace_config_version",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_worker_runtime_configurations_version"
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_runtime_configurations_worker_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_runtime_configurations_adapter_module_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_runtime_configurations_workspace_module_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_worker_runtime_configurations_workspace",
        ),
        sa.PrimaryKeyConstraint("worker_id"),
    )

    op.create_table(
        "scheduling_blocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("reason_details", postgresql.JSONB(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "requires_operator",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('active', 'resolved')",
            name="ck_scheduling_blocks_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reason_details) = 'object'",
            name="ck_scheduling_blocks_reason_details_object",
        ),
        sa.CheckConstraint(
            "occurrence_count >= 1",
            name="ck_scheduling_blocks_occurrence_count",
        ),
        sa.CheckConstraint("version >= 1", name="ck_scheduling_blocks_version"),
        sa.ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_scheduling_blocks_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_scheduling_blocks_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_scheduling_blocks_identity"),
        sa.Index(
            "uq_scheduling_blocks_active_execution",
            "execution_id",
            "workspace_id",
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
        ),
        sa.Index(
            "ix_scheduling_blocks_workspace_status",
            "workspace_id",
            "status",
        ),
        sa.Index(
            "ix_scheduling_blocks_workspace_execution",
            "workspace_id",
            "execution_id",
        ),
        sa.Index(
            "ix_scheduling_blocks_workspace_last_seen",
            "workspace_id",
            "last_seen_at",
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scheduling_blocks_workspace_last_seen", table_name="scheduling_blocks"
    )
    op.drop_index(
        "ix_scheduling_blocks_workspace_execution", table_name="scheduling_blocks"
    )
    op.drop_index(
        "ix_scheduling_blocks_workspace_status", table_name="scheduling_blocks"
    )
    op.drop_index(
        "uq_scheduling_blocks_active_execution", table_name="scheduling_blocks"
    )
    op.drop_table("scheduling_blocks")
    op.drop_table("worker_runtime_configurations")

    op.drop_constraint(
        "ck_module_instances_resource_version",
        "module_instances",
        type_="check",
    )
    op.drop_column("module_instances", "resource_version")
    op.drop_constraint(
        "ck_module_definitions_resource_version",
        "module_definitions",
        type_="check",
    )
    op.drop_column("module_definitions", "resource_version")

    op.drop_constraint(
        "ck_execution_requests_adapter_forbidden_array",
        "execution_requests",
        type_="check",
    )
    op.drop_column("execution_requests", "adapter_forbidden")

    op.drop_constraint(
        "ck_executions_adapter_forbidden_array", "executions", type_="check"
    )
    op.drop_constraint(
        "ck_executions_adapter_preferences_array", "executions", type_="check"
    )
    op.drop_column("executions", "adapter_forbidden")
    op.drop_column("executions", "adapter_preferences")
