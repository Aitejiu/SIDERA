"""Add R3 modules, gateway state, artifacts, evidence, and inputs.

Revision ID: 0005_r3_gateway_state
Revises: 0004_worker_desired_state
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_r3_gateway_state"
down_revision: str | None = "0004_worker_desired_state"
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


def _workspace_fk(
    *,
    local: list[str],
    remote: list[str],
    name: str,
    ondelete: str = "RESTRICT",
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        local,
        remote,
        ondelete=ondelete,
        name=name,
    )


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_execution_sessions_identity",
        "execution_sessions",
        ["id", "workspace_id"],
    )
    op.add_column(
        "execution_sessions",
        sa.Column("adapter_module_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "execution_sessions",
        sa.Column("adapter_config_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "execution_sessions",
        sa.Column("workspace_module_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "execution_sessions",
        sa.Column("workspace_config_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "execution_sessions",
        sa.Column("capability_snapshot", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "execution_sessions",
        sa.Column("process_ref", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "execution_sessions",
        sa.Column("workspace_ref", sa.String(length=500), nullable=True),
    )

    op.create_table(
        "module_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("module_type", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("interface_version", sa.String(length=100), nullable=False),
        sa.Column("config_schema_version", sa.Integer(), nullable=False),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "config_schema_version >= 1", name="ck_module_definitions_schema_version"
        ),
        sa.CheckConstraint(
            "lifecycle IN ('active', 'draining', 'disabled')",
            name="ck_module_definitions_lifecycle",
        ),
        _workspace_fk(
            local=["workspace_id"],
            remote=["workspaces.id"],
            name="fk_module_definitions_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_module_definitions_identity"),
        *_timestamps(),
    )
    op.create_index(
        "ix_module_definitions_workspace_type",
        "module_definitions",
        ["workspace_id", "module_type"],
    )

    op.create_table(
        "module_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_name", sa.String(length=255), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("config_namespace", sa.String(length=100), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("secret_refs", postgresql.JSONB(), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "config_version >= 1", name="ck_module_instances_config_version"
        ),
        sa.CheckConstraint(
            "lifecycle IN ('active', 'draining', 'disabled')",
            name="ck_module_instances_lifecycle",
        ),
        _workspace_fk(
            local=["workspace_id"],
            remote=["workspaces.id"],
            name="fk_module_instances_workspace",
        ),
        _workspace_fk(
            local=["definition_id", "workspace_id"],
            remote=["module_definitions.id", "module_definitions.workspace_id"],
            name="fk_module_instances_definition_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_module_instances_identity"),
        sa.UniqueConstraint(
            "workspace_id",
            "instance_name",
            "config_version",
            name="uq_module_instances_name_version",
        ),
        *_timestamps(),
    )

    op.create_table(
        "execution_workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("checkouts", postgresql.JSONB(), nullable=False),
        sa.Column("location_ref", sa.String(length=500), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("retention_seconds", sa.Integer(), nullable=False),
        sa.Column("retain_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "lifecycle IN ('requested', 'preparing', 'ready', 'active', 'frozen', "
            "'retained', 'cleaning', 'released', 'quarantined')",
            name="ck_execution_workspaces_lifecycle",
        ),
        sa.CheckConstraint(
            "retention_seconds > 0", name="ck_execution_workspaces_retention"
        ),
        sa.CheckConstraint(
            "fencing_token >= 1", name="ck_execution_workspaces_fencing"
        ),
        sa.CheckConstraint("version >= 1", name="ck_execution_workspaces_version"),
        _workspace_fk(
            local=["workspace_id"],
            remote=["workspaces.id"],
            name="fk_execution_workspaces_workspace",
        ),
        _workspace_fk(
            local=["task_id", "workspace_id"],
            remote=["tasks.id", "tasks.workspace_id"],
            name="fk_execution_workspaces_task_workspace",
        ),
        _workspace_fk(
            local=["execution_id", "workspace_id"],
            remote=["executions.id", "executions.workspace_id"],
            name="fk_execution_workspaces_execution_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_execution_workspaces_identity"),
        *_timestamps(),
    )
    op.create_index(
        "uq_execution_workspaces_active_execution",
        "execution_workspaces",
        ["execution_id"],
        unique=True,
        postgresql_where=sa.text(
            "lifecycle IN ('requested', 'preparing', 'ready', 'active')"
        ),
    )

    op.create_table(
        "execution_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("adapter_module_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_module_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("adapter_config_version", sa.Integer(), nullable=False),
        sa.Column("workspace_config_version", sa.Integer(), nullable=False),
        sa.Column("capability_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "adapter_config_version >= 1", name="ck_bindings_adapter_config"
        ),
        sa.CheckConstraint(
            "workspace_config_version >= 1", name="ck_bindings_workspace_config"
        ),
        sa.CheckConstraint("fencing_token >= 1", name="ck_bindings_fencing_token"),
        sa.CheckConstraint("version >= 1", name="ck_bindings_version"),
        _workspace_fk(
            local=["workspace_id"],
            remote=["workspaces.id"],
            name="fk_bindings_workspace",
        ),
        _workspace_fk(
            local=["execution_id", "workspace_id"],
            remote=["executions.id", "executions.workspace_id"],
            name="fk_bindings_execution_workspace",
        ),
        _workspace_fk(
            local=["worker_id", "workspace_id"],
            remote=["workers.id", "workers.workspace_id"],
            name="fk_bindings_worker_workspace",
        ),
        _workspace_fk(
            local=["adapter_module_id", "workspace_id"],
            remote=["module_instances.id", "module_instances.workspace_id"],
            name="fk_bindings_adapter_module_workspace",
        ),
        _workspace_fk(
            local=["workspace_module_id", "workspace_id"],
            remote=["module_instances.id", "module_instances.workspace_id"],
            name="fk_bindings_workspace_module_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["lease_id", "execution_id", "workspace_id"],
            [
                "assignment_leases.id",
                "assignment_leases.execution_id",
                "assignment_leases.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_bindings_lease_execution_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_bindings_identity"),
        sa.UniqueConstraint(
            "execution_id", "workspace_id", name="uq_bindings_execution"
        ),
        *_timestamps(),
    )

    op.create_table(
        "process_descriptors",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("nonce", sa.String(length=255), nullable=False),
        sa.Column("process_group_id", sa.Integer(), nullable=False),
        sa.CheckConstraint("pid >= 1", name="ck_process_descriptors_pid"),
        sa.CheckConstraint(
            "process_group_id >= 1", name="ck_process_descriptors_group"
        ),
        _workspace_fk(
            local=["workspace_id"],
            remote=["workspaces.id"],
            name="fk_process_descriptors_workspace",
        ),
        _workspace_fk(
            local=["execution_id", "workspace_id"],
            remote=["executions.id", "executions.workspace_id"],
            name="fk_process_descriptors_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_process_descriptors_session_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "nonce", name="uq_process_descriptors_nonce"),
        *_timestamps(),
    )

    op.create_table(
        "gateway_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("module_instance_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=True),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_ref", sa.String(length=500), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('requested', 'running', 'succeeded', 'failed', 'unknown')",
            name="ck_gateway_operations_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_gateway_operations_attempts"
        ),
        sa.CheckConstraint("fencing_token >= 1", name="ck_gateway_operations_fencing"),
        sa.CheckConstraint("version >= 1", name="ck_gateway_operations_version"),
        _workspace_fk(
            local=["workspace_id"],
            remote=["workspaces.id"],
            name="fk_gateway_operations_workspace",
        ),
        _workspace_fk(
            local=["task_id", "workspace_id"],
            remote=["tasks.id", "tasks.workspace_id"],
            name="fk_gateway_operations_task_workspace",
        ),
        _workspace_fk(
            local=["execution_id", "workspace_id"],
            remote=["executions.id", "executions.workspace_id"],
            name="fk_gateway_operations_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_gateway_operations_session_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_gateway_operations_module_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["lease_id", "execution_id", "workspace_id"],
            [
                "assignment_leases.id",
                "assignment_leases.execution_id",
                "assignment_leases.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_gateway_operations_lease_execution_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "operation_type", "operation_id",
            name="uq_gateway_operations_idempotency",
        ),
        *_timestamps(),
    )
    op.create_index(
        "ix_gateway_operations_recovery",
        "gateway_operations",
        ["status", "next_attempt_at"],
    )

    op.create_table(
        "execution_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_ref", sa.String(length=500), nullable=True),
        sa.CheckConstraint("sequence >= 1", name="ck_execution_events_sequence"),
        _workspace_fk(
            local=["workspace_id"],
            remote=["workspaces.id"],
            name="fk_execution_events_workspace",
        ),
        _workspace_fk(
            local=["execution_id", "workspace_id"],
            remote=["executions.id", "executions.workspace_id"],
            name="fk_execution_events_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_events_session_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "sequence", name="uq_execution_events_session_sequence"
        ),
        *_timestamps(),
    )
    op.create_index(
        "ix_execution_events_execution_sequence",
        "execution_events",
        ["execution_id", "sequence"],
    )

    op.create_table(
        "artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("storage_ref", sa.String(length=500), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifacts_size"),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-fA-F]{64}$'", name="ck_artifacts_content_hash"
        ),
        _workspace_fk(
            local=["workspace_id"],
            remote=["workspaces.id"],
            name="fk_artifacts_workspace",
        ),
        _workspace_fk(
            local=["task_id", "workspace_id"],
            remote=["tasks.id", "tasks.workspace_id"],
            name="fk_artifacts_task_workspace",
        ),
        _workspace_fk(
            local=["execution_id", "workspace_id"],
            remote=["executions.id", "executions.workspace_id"],
            name="fk_artifacts_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_artifacts_session_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "storage_ref", "content_hash",
            name="uq_artifacts_storage_hash",
        ),
        *_timestamps(),
    )

    op.create_table(
        "evidence_claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("acceptance_criterion_ref", sa.String(length=255), nullable=False),
        sa.Column("conclusion", sa.Text(), nullable=False),
        sa.Column("environment_ref", sa.String(length=500), nullable=False),
        sa.Column("artifact_ids", postgresql.JSONB(), nullable=False),
        sa.Column("producer", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "status = 'candidate'", name="ck_evidence_claims_candidate_only"
        ),
        _workspace_fk(
            local=["workspace_id"],
            remote=["workspaces.id"],
            name="fk_evidence_claims_workspace",
        ),
        _workspace_fk(
            local=["task_id", "workspace_id"],
            remote=["tasks.id", "tasks.workspace_id"],
            name="fk_evidence_claims_task_workspace",
        ),
        _workspace_fk(
            local=["execution_id", "workspace_id"],
            remote=["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_evidence_claims_execution_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        *_timestamps(),
    )

    op.create_table(
        "execution_inputs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("attachment_refs", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('accepted', 'queued', 'dispatching', 'delivered', 'blocked', 'failed', 'cancelled')",
            name="ck_execution_inputs_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_execution_inputs_version"),
        _workspace_fk(
            local=["workspace_id"],
            remote=["workspaces.id"],
            name="fk_execution_inputs_workspace",
        ),
        _workspace_fk(
            local=["execution_id", "workspace_id"],
            remote=["executions.id", "executions.workspace_id"],
            name="fk_execution_inputs_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_inputs_session_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "session_id", "client_idempotency_key",
            name="uq_execution_inputs_idempotency",
        ),
        *_timestamps(),
    )
    op.create_index(
        "uq_execution_inputs_one_pending_session",
        "execution_inputs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('accepted', 'queued', 'dispatching', 'blocked')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_execution_inputs_one_pending_session", table_name="execution_inputs"
    )
    op.drop_table("execution_inputs")
    op.drop_table("evidence_claims")
    op.drop_table("artifacts")
    op.drop_index(
        "ix_execution_events_execution_sequence", table_name="execution_events"
    )
    op.drop_table("execution_events")
    op.drop_index("ix_gateway_operations_recovery", table_name="gateway_operations")
    op.drop_table("gateway_operations")
    op.drop_table("process_descriptors")
    op.drop_index(
        "uq_execution_workspaces_active_execution", table_name="execution_workspaces"
    )
    op.drop_table("execution_workspaces")
    op.drop_table("execution_bindings")
    op.drop_index(
        "ix_module_definitions_workspace_type", table_name="module_definitions"
    )
    op.drop_table("module_instances")
    op.drop_table("module_definitions")
    for column in (
        "workspace_ref",
        "process_ref",
        "capability_snapshot",
        "workspace_config_version",
        "workspace_module_id",
        "adapter_config_version",
        "adapter_module_id",
    ):
        op.drop_column("execution_sessions", column)
    op.drop_constraint(
        "uq_execution_sessions_identity", "execution_sessions", type_="unique"
    )
