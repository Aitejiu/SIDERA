"""Persist the R6.3.1 collaboration runtime ledger and request gate identity.

Revision ID: 0022_r6_3_1_collaboration_runtime
Revises: 0021_r6_2_retired_handoff_targets
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_r6_3_1_collaboration_runtime"
down_revision: str | None = "0021_r6_2_retired_handoff_targets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _add_collaboration_gate_mode()
    _create_collaboration_runs()
    _create_runtime_projection_snapshots()
    _create_collaboration_evaluations()
    _create_runtime_decisions()
    _create_runtime_intents()
    _create_runtime_releases()
    _create_runtime_intent_applications()
    _add_deferred_foreign_keys()
    op.create_index(
        "ix_projection_changes_collaboration_target_cursor",
        "projection_changes",
        ["workspace_id", "projection_type", "resource_id", "cursor"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_projection_changes_collaboration_target_cursor",
        table_name="projection_changes",
    )
    op.drop_constraint(
        "fk_runtime_intent_applications_release_workspace",
        "runtime_intent_applications",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_collaboration_runs_decision_workspace",
        "collaboration_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_collaboration_runs_projection_workspace",
        "collaboration_runs",
        type_="foreignkey",
    )
    op.drop_table("runtime_intent_applications")
    op.drop_table("runtime_releases")
    op.drop_table("runtime_intents")
    op.drop_table("runtime_decisions")
    op.drop_table("collaboration_evaluations")
    op.drop_table("runtime_projection_snapshots")
    op.drop_table("collaboration_runs")
    op.drop_constraint(
        "ck_execution_requests_collaboration_gate_mode",
        "execution_requests",
        type_="check",
    )
    op.drop_column("execution_requests", "collaboration_gate_mode")


def _add_collaboration_gate_mode() -> None:
    op.add_column(
        "execution_requests",
        sa.Column(
            "collaboration_gate_mode",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'unmanaged'"),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE execution_requests "
            "SET collaboration_gate_mode = 'unmanaged' "
            "WHERE collaboration_gate_mode IS NULL"
        )
    )
    op.create_check_constraint(
        "ck_execution_requests_collaboration_gate_mode",
        "execution_requests",
        "collaboration_gate_mode IN ('unmanaged', 'release_required')",
    )
    op.alter_column(
        "execution_requests",
        "collaboration_gate_mode",
        existing_type=sa.String(length=32),
        existing_nullable=False,
        server_default=None,
    )


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


def _uuid() -> object:
    return postgresql.UUID(as_uuid=True)


def _json() -> object:
    return postgresql.JSONB()


def _create_collaboration_runs() -> None:
    op.create_table(
        "collaboration_runs",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("root_task_id", _uuid(), nullable=False),
        sa.Column("plan_snapshot_id", _uuid(), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("plan_content_hash", sa.String(length=64), nullable=False),
        sa.Column("runtime_definition_id", _uuid(), nullable=False),
        sa.Column("runtime_instance_id", _uuid(), nullable=False),
        sa.Column("runtime_interface_version", sa.String(length=100), nullable=False),
        sa.Column("runtime_config_version", sa.Integer(), nullable=False),
        sa.Column("runtime_capability_snapshot_json", _json(), nullable=False),
        sa.Column("policy_snapshot_json", _json(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_projection_snapshot_id", _uuid(), nullable=True),
        sa.Column("last_decision_id", _uuid(), nullable=True),
        sa.Column("last_evaluated_change_cursor", sa.BigInteger(), nullable=False),
        sa.Column("next_evaluation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failure_count", sa.Integer(), nullable=False),
        sa.Column("blocked_reason", sa.String(length=16384), nullable=True),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "plan_generation >= 1", name="ck_collaboration_runs_generation"
        ),
        sa.CheckConstraint(
            "runtime_config_version >= 1",
            name="ck_collaboration_runs_runtime_config_version",
        ),
        sa.CheckConstraint(
            "last_evaluated_change_cursor >= 0",
            name="ck_collaboration_runs_change_cursor",
        ),
        sa.CheckConstraint(
            "consecutive_failure_count >= 0",
            name="ck_collaboration_runs_failure_count",
        ),
        sa.CheckConstraint(
            "resource_version >= 1", name="ck_collaboration_runs_resource_version"
        ),
        sa.CheckConstraint(
            "plan_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_collaboration_runs_plan_hash",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'blocked', 'completed', 'superseded')",
            name="ck_collaboration_runs_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(runtime_capability_snapshot_json) = 'object'",
            name="ck_collaboration_runs_capability_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(policy_snapshot_json) = 'object'",
            name="ck_collaboration_runs_policy_object",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_collaboration_runs_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_collaboration_runs_root_task_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_collaboration_runs_plan_snapshot_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_definition_id", "workspace_id"],
            ["module_definitions.id", "module_definitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_collaboration_runs_runtime_definition_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_collaboration_runs_runtime_instance_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_collaboration_runs_identity"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "root_task_id",
            "plan_snapshot_id",
            "plan_generation",
            name="uq_collaboration_runs_plan_identity",
        ),
        sa.Index(
            "ix_collaboration_runs_recovery",
            "workspace_id",
            "status",
            "next_evaluation_at",
        ),
    )
    op.create_index(
        "uq_collaboration_runs_root_active",
        "collaboration_runs",
        ["workspace_id", "root_task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'active', 'blocked')"),
    )


def _create_runtime_projection_snapshots() -> None:
    op.create_table(
        "runtime_projection_snapshots",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("run_id", _uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("plan_snapshot_id", _uuid(), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("plan_content_hash", sa.String(length=64), nullable=False),
        sa.Column("fact_cursor", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", _json(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "schema_version >= 1", name="ck_runtime_projection_schema_version"
        ),
        sa.CheckConstraint(
            "plan_generation >= 1", name="ck_runtime_projection_generation"
        ),
        sa.CheckConstraint(
            "fact_cursor >= 0", name="ck_runtime_projection_fact_cursor"
        ),
        sa.CheckConstraint("size_bytes >= 1", name="ck_runtime_projection_size"),
        sa.CheckConstraint(
            "plan_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_projection_plan_hash",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_projection_content_hash",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload_json) = 'object'",
            name="ck_runtime_projection_payload_object",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["collaboration_runs.id", "collaboration_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_projection_run_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_projection_plan_snapshot_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_runtime_projection_identity"
        ),
        sa.UniqueConstraint(
            "run_id",
            "schema_version",
            "content_hash",
            name="uq_runtime_projection_run_schema_hash",
        ),
        sa.Index(
            "ix_runtime_projection_run_created",
            "workspace_id",
            "run_id",
            "created_at",
        ),
    )


def _create_collaboration_evaluations() -> None:
    op.create_table(
        "collaboration_evaluations",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("run_id", _uuid(), nullable=False),
        sa.Column("projection_snapshot_id", _uuid(), nullable=False),
        sa.Column("operation_id", _uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("response_hash", sa.String(length=64), nullable=True),
        sa.Column("diagnostic_summary", sa.String(length=16384), nullable=True),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'evaluating', 'retry_wait', 'completed', 'blocked')",
            name="ck_collaboration_evaluations_status",
        ),
        sa.CheckConstraint(
            "attempt >= 1", name="ck_collaboration_evaluations_attempt"
        ),
        sa.CheckConstraint(
            "resource_version >= 1",
            name="ck_collaboration_evaluations_resource_version",
        ),
        sa.CheckConstraint(
            "response_hash IS NULL OR response_hash ~ '^[0-9a-f]{64}$'",
            name="ck_collaboration_evaluations_response_hash",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["collaboration_runs.id", "collaboration_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_collaboration_evaluations_run_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["projection_snapshot_id", "workspace_id"],
            [
                "runtime_projection_snapshots.id",
                "runtime_projection_snapshots.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_collaboration_evaluations_projection_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_collaboration_evaluations_identity"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "run_id",
            "projection_snapshot_id",
            name="uq_collaboration_evaluations_run_projection",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "operation_id",
            name="uq_collaboration_evaluations_operation",
        ),
        sa.Index(
            "ix_collaboration_evaluations_claimable",
            "workspace_id",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
    )


def _create_runtime_decisions() -> None:
    op.create_table(
        "runtime_decisions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("run_id", _uuid(), nullable=False),
        sa.Column("evaluation_id", _uuid(), nullable=False),
        sa.Column("projection_snapshot_id", _uuid(), nullable=False),
        sa.Column("projection_hash", sa.String(length=64), nullable=False),
        sa.Column("operation_id", _uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.String(length=16384), nullable=False),
        sa.Column("strategy_state_json", _json(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("runtime_duration_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "schema_version >= 1", name="ck_runtime_decisions_schema_version"
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_runtime_decisions_sequence"),
        sa.CheckConstraint(
            "runtime_duration_ms >= 0",
            name="ck_runtime_decisions_duration",
        ),
        sa.CheckConstraint(
            "outcome IN ('intents', 'no_change')",
            name="ck_runtime_decisions_outcome",
        ),
        sa.CheckConstraint(
            "projection_hash ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_decisions_projection_hash",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_decisions_content_hash",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(strategy_state_json) = 'object'",
            name="ck_runtime_decisions_strategy_state_object",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["collaboration_runs.id", "collaboration_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_decisions_run_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id", "workspace_id"],
            [
                "collaboration_evaluations.id",
                "collaboration_evaluations.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_runtime_decisions_evaluation_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["projection_snapshot_id", "workspace_id"],
            [
                "runtime_projection_snapshots.id",
                "runtime_projection_snapshots.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_runtime_decisions_projection_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_runtime_decisions_identity"
        ),
        sa.UniqueConstraint(
            "workspace_id", "run_id", "sequence", name="uq_runtime_decisions_sequence"
        ),
        sa.Index(
            "ix_runtime_decisions_run_sequence",
            "workspace_id",
            "run_id",
            "sequence",
        ),
    )


def _create_runtime_intents() -> None:
    op.create_table(
        "runtime_intents",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("run_id", _uuid(), nullable=False),
        sa.Column("decision_id", _uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("intent_type", sa.String(length=100), nullable=False),
        sa.Column("target_execution_request_id", _uuid(), nullable=False),
        sa.Column("target_request_version", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.String(length=16384), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload_json", _json(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_runtime_intents_ordinal"),
        sa.CheckConstraint(
            "target_request_version >= 1",
            name="ck_runtime_intents_request_version",
        ),
        sa.CheckConstraint(
            "intent_type = 'release_execution_request'",
            name="ck_runtime_intents_type",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload_json) = 'object'",
            name="ck_runtime_intents_payload_object",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["collaboration_runs.id", "collaboration_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_intents_run_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id", "workspace_id"],
            ["runtime_decisions.id", "runtime_decisions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_intents_decision_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_execution_request_id", "workspace_id"],
            ["execution_requests.id", "execution_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_intents_request_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_runtime_intents_identity"),
        sa.UniqueConstraint(
            "workspace_id",
            "decision_id",
            "ordinal",
            name="uq_runtime_intents_decision_ordinal",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_runtime_intents_idempotency",
        ),
        sa.Index(
            "ix_runtime_intents_decision_ordinal",
            "workspace_id",
            "decision_id",
            "ordinal",
        ),
    )


def _create_runtime_releases() -> None:
    op.create_table(
        "runtime_releases",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("run_id", _uuid(), nullable=False),
        sa.Column("plan_snapshot_id", _uuid(), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("plan_content_hash", sa.String(length=64), nullable=False),
        sa.Column("decision_id", _uuid(), nullable=False),
        sa.Column("intent_id", _uuid(), nullable=False),
        sa.Column("execution_request_id", _uuid(), nullable=False),
        sa.Column("request_version_at_release", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "plan_generation >= 1", name="ck_runtime_releases_generation"
        ),
        sa.CheckConstraint(
            "request_version_at_release >= 1",
            name="ck_runtime_releases_request_version",
        ),
        sa.CheckConstraint(
            "plan_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_releases_plan_hash",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["collaboration_runs.id", "collaboration_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_releases_run_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_releases_plan_snapshot_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id", "workspace_id"],
            ["runtime_decisions.id", "runtime_decisions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_releases_decision_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["intent_id", "workspace_id"],
            ["runtime_intents.id", "runtime_intents.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_releases_intent_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["execution_request_id", "workspace_id"],
            ["execution_requests.id", "execution_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_releases_request_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_runtime_releases_identity"),
        sa.UniqueConstraint(
            "workspace_id",
            "execution_request_id",
            name="uq_runtime_releases_workspace_request",
        ),
        sa.UniqueConstraint("intent_id", name="uq_runtime_releases_intent"),
        sa.Index(
            "ix_runtime_releases_run_issued",
            "workspace_id",
            "run_id",
            "issued_at",
        ),
    )


def _create_runtime_intent_applications() -> None:
    op.create_table(
        "runtime_intent_applications",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("intent_id", _uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expected_run_version", sa.Integer(), nullable=False),
        sa.Column("observed_run_version", sa.Integer(), nullable=False),
        sa.Column("expected_request_version", sa.Integer(), nullable=False),
        sa.Column("observed_request_version", sa.Integer(), nullable=True),
        sa.Column("release_id", _uuid(), nullable=True),
        sa.Column("result_code", sa.String(length=255), nullable=False),
        sa.Column("result_summary", sa.String(length=16384), nullable=False),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'rejected', 'stale', 'duplicate')",
            name="ck_runtime_intent_applications_status",
        ),
        sa.CheckConstraint(
            "expected_run_version >= 1 AND observed_run_version >= 1",
            name="ck_runtime_intent_applications_run_versions",
        ),
        sa.CheckConstraint(
            "expected_request_version >= 1 AND "
            "(observed_request_version IS NULL OR observed_request_version >= 1)",
            name="ck_runtime_intent_applications_request_versions",
        ),
        sa.CheckConstraint(
            "resource_version >= 1",
            name="ck_runtime_intent_applications_resource_version",
        ),
        sa.ForeignKeyConstraint(
            ["intent_id", "workspace_id"],
            ["runtime_intents.id", "runtime_intents.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_intent_applications_intent_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_runtime_intent_applications_identity"
        ),
        sa.UniqueConstraint(
            "intent_id", name="uq_runtime_intent_applications_intent"
        ),
        sa.Index(
            "ix_runtime_intent_applications_status",
            "workspace_id",
            "status",
            "created_at",
        ),
    )


def _add_deferred_foreign_keys() -> None:
    op.create_foreign_key(
        "fk_collaboration_runs_projection_workspace",
        "collaboration_runs",
        "runtime_projection_snapshots",
        ["last_projection_snapshot_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_collaboration_runs_decision_workspace",
        "collaboration_runs",
        "runtime_decisions",
        ["last_decision_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_runtime_intent_applications_release_workspace",
        "runtime_intent_applications",
        "runtime_releases",
        ["release_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )
