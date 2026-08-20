"""Persist workers, executions, sessions, and assignment leases.

Revision ID: 0002_worker_execution_lease
Revises: 0001_control_plane_foundation
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_worker_execution_lease"
down_revision: str | None = "0001_control_plane_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
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
    )


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_tasks_id_workspace", "tasks", ["id", "workspace_id"]
    )

    op.create_table(
        "workers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("adapter_type", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("current_load", sa.Integer(), nullable=False),
        sa.Column("cost_per_hour_microunits", sa.BigInteger(), nullable=False),
        sa.Column("startup_latency_ms", sa.Integer(), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("capacity >= 1", name="ck_workers_capacity"),
        sa.CheckConstraint(
            "current_load >= 0 AND current_load <= capacity",
            name="ck_workers_current_load",
        ),
        sa.CheckConstraint(
            "cost_per_hour_microunits >= 0",
            name="ck_workers_cost_per_hour_microunits",
        ),
        sa.CheckConstraint(
            "startup_latency_ms >= 0", name="ck_workers_startup_latency_ms"
        ),
        sa.CheckConstraint("version >= 1", name="ck_workers_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_workers_id_workspace"),
        *_timestamps(),
    )

    op.create_table(
        "executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lineage_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("capability_requirements", postgresql.JSONB(), nullable=False),
        sa.Column("pinned_worker_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_worker_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("active_lease_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("attempt >= 1", name="ck_executions_attempt"),
        sa.CheckConstraint(
            "max_attempts >= attempt", name="ck_executions_max_attempts"
        ),
        sa.CheckConstraint(
            "last_fencing_token >= 0", name="ck_executions_last_fencing_token"
        ),
        sa.CheckConstraint("version >= 1", name="ck_executions_version"),
        sa.ForeignKeyConstraint(
            ["task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_executions_task_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["lineage_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_executions_lineage_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["pinned_worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_executions_pinned_worker_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_executions_assigned_worker_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_executions_id_workspace"),
        sa.UniqueConstraint(
            "lineage_id", "attempt", name="uq_executions_lineage_attempt"
        ),
        *_timestamps(),
    )

    op.create_table(
        "assignment_leases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("assignment_reason", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "fencing_token >= 1", name="ck_assignment_leases_fencing_token"
        ),
        sa.CheckConstraint("version >= 1", name="ck_assignment_leases_version"),
        sa.ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_assignment_leases_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_assignment_leases_worker_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id", "fencing_token", name="uq_assignment_leases_execution_token"
        ),
        sa.UniqueConstraint(
            "id", "execution_id", "workspace_id", name="uq_assignment_leases_identity"
        ),
        *_timestamps(),
    )

    op.create_table(
        "execution_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("adapter_session_id", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_mode", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "last_sequence >= 0", name="ck_execution_sessions_last_sequence"
        ),
        sa.CheckConstraint("version >= 1", name="ck_execution_sessions_version"),
        sa.ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_sessions_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_sessions_worker_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "worker_id", "adapter_session_id", name="uq_execution_sessions_adapter"
        ),
        *_timestamps(),
    )

    op.create_foreign_key(
        "fk_executions_active_lease",
        "executions",
        "assignment_leases",
        ["active_lease_id", "id", "workspace_id"],
        ["id", "execution_id", "workspace_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_index(
        "uq_assignment_leases_active_execution",
        "assignment_leases",
        ["execution_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_execution_sessions_active_execution",
        "execution_sessions",
        ["execution_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_executions_schedulable",
        "executions",
        ["status", "not_before", "created_at"],
    )
    op.create_index(
        "ix_workers_scheduler_candidates",
        "workers",
        ["workspace_id", "status", "last_heartbeat_at"],
    )
    op.create_index(
        "ix_assignment_leases_expiry",
        "assignment_leases",
        ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_executions_active_lease", "executions", type_="foreignkey"
    )
    op.drop_index("ix_assignment_leases_expiry", table_name="assignment_leases")
    op.drop_index("ix_workers_scheduler_candidates", table_name="workers")
    op.drop_index("ix_executions_schedulable", table_name="executions")
    op.drop_index(
        "uq_execution_sessions_active_execution", table_name="execution_sessions"
    )
    op.drop_index(
        "uq_assignment_leases_active_execution", table_name="assignment_leases"
    )
    op.drop_table("execution_sessions")
    op.drop_table("assignment_leases")
    op.drop_table("executions")
    op.drop_table("workers")
    op.drop_constraint("uq_tasks_id_workspace", "tasks", type_="unique")
