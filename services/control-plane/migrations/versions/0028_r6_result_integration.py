"""Persist immutable result integration, verification, rework, and apply facts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_r6_result_integration"
down_revision: str | None = "0027_r6_3_4_result_input_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid() -> sa.types.TypeEngine:
    return sa.dialects.postgresql.UUID(as_uuid=True)


def _jsonb() -> sa.types.TypeEngine:
    return sa.dialects.postgresql.JSONB


def _workspace_fk(
    local_table: str,
    remote_table: str,
    columns: list[str],
    remote_columns: list[str],
    name: str,
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        columns,
        [f"{remote_table}.{column}" for column in remote_columns],
        ondelete="RESTRICT",
        name=name,
    )


def upgrade() -> None:
    op.create_table(
        "result_operation_attempts",
        sa.Column("operation_id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("root_task_id", _uuid(), nullable=False),
        sa.Column("operation_kind", sa.String(64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", _jsonb(), nullable=False),
        sa.Column("lease_owner", sa.String(255)),
        sa.Column("lease_token", sa.String(255)),
        sa.Column("lease_expiry", sa.DateTime(timezone=True)),
        sa.Column("receipt_ref", sa.String(1024)),
        sa.Column("failure_code", sa.String(128)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt >= 1 AND version >= 1",
            name="ck_result_operation_attempts_versions",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'blocked', 'unknown', 'failed')",
            name="ck_result_operation_attempts_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_result_operation_attempts_payload_object",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_expiry IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expiry IS NOT NULL)",
            name="ck_result_operation_attempts_lease_pair",
        ),
        _workspace_fk(
            "result_operation_attempts",
            "tasks",
            ["root_task_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_result_operation_attempts_root_task_workspace",
        ),
        sa.PrimaryKeyConstraint("operation_id", name="pk_result_operation_attempts"),
        sa.UniqueConstraint(
            "operation_id",
            "workspace_id",
            name="uq_result_operation_attempts_identity",
        ),
        sa.Index(
            "ix_result_operation_attempts_claims",
            "workspace_id",
            "status",
            "lease_expiry",
            "created_at",
        ),
    )

    op.create_table(
        "code_result_snapshots",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("root_task_id", _uuid(), nullable=False),
        sa.Column("worker_item_id", _uuid(), nullable=False),
        sa.Column("request_id", _uuid(), nullable=False),
        sa.Column("execution_id", _uuid(), nullable=False),
        sa.Column("repository_ref", sa.String(500), nullable=False),
        sa.Column("input_commit", sa.String(64), nullable=False),
        sa.Column("input_tree", sa.String(64), nullable=False),
        sa.Column("result_commit", sa.String(64), nullable=False),
        sa.Column("result_tree", sa.String(64), nullable=False),
        sa.Column("consumed_result_ids", _jsonb(), nullable=False),
        sa.Column("artifact_ids", _jsonb(), nullable=False),
        sa.Column("capture_operation_id", _uuid(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload", _jsonb(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 1 AND content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_code_result_snapshots_protocol",
        ),
        sa.CheckConstraint(
            "input_commit ~ '^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$' AND "
            "input_tree ~ '^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$' AND "
            "result_commit ~ '^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$' AND "
            "result_tree ~ '^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$'",
            name="ck_code_result_snapshots_git_oids",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(consumed_result_ids) = 'array' AND "
            "jsonb_typeof(artifact_ids) = 'array' AND jsonb_typeof(payload) = 'object'",
            name="ck_code_result_snapshots_json_arrays",
        ),
        _workspace_fk(
            "code_result_snapshots",
            "tasks",
            ["root_task_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_code_result_snapshots_root_task_workspace",
        ),
        _workspace_fk(
            "code_result_snapshots",
            "worker_items",
            ["worker_item_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_code_result_snapshots_worker_item_workspace",
        ),
        _workspace_fk(
            "code_result_snapshots",
            "execution_requests",
            ["request_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_code_result_snapshots_request_workspace",
        ),
        _workspace_fk(
            "code_result_snapshots",
            "executions",
            ["execution_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_code_result_snapshots_execution_workspace",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_code_result_snapshots"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_code_result_snapshots_identity"),
        sa.UniqueConstraint(
            "workspace_id",
            "capture_operation_id",
            name="uq_code_result_snapshots_capture_operation",
        ),
        sa.Index(
            "ix_code_result_snapshots_worker_history",
            "workspace_id",
            "worker_item_id",
            "created_at",
        ),
        sa.Index(
            "ix_code_result_snapshots_root_history",
            "workspace_id",
            "root_task_id",
            "created_at",
        ),
    )

    op.create_table(
        "integration_candidates",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("root_task_id", _uuid(), nullable=False),
        sa.Column("plan_snapshot_id", _uuid(), nullable=False),
        sa.Column("plan_content_hash", sa.String(64), nullable=False),
        sa.Column("integration_execution_id", _uuid(), nullable=False),
        sa.Column("base_commit", sa.String(64), nullable=False),
        sa.Column("source_result_ids", _jsonb(), nullable=False),
        sa.Column("input_selection_id", _uuid(), nullable=False),
        sa.Column("input_selection_hash", sa.String(64), nullable=False),
        sa.Column("code_result_id", _uuid(), nullable=False),
        sa.Column("previous_candidate_id", _uuid()),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload", _jsonb(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 1 AND plan_content_hash ~ '^[0-9a-f]{64}$' AND "
            "input_selection_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_integration_candidates_protocol",
        ),
        sa.CheckConstraint(
            "base_commit ~ '^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$'",
            name="ck_integration_candidates_base_oid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_result_ids) = 'array' AND jsonb_array_length(source_result_ids) >= 1 "
            "AND jsonb_typeof(payload) = 'object'",
            name="ck_integration_candidates_sources_array",
        ),
        _workspace_fk(
            "integration_candidates",
            "tasks",
            ["root_task_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_integration_candidates_root_task_workspace",
        ),
        _workspace_fk(
            "integration_candidates",
            "task_plan_snapshots",
            ["plan_snapshot_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_integration_candidates_plan_workspace",
        ),
        _workspace_fk(
            "integration_candidates",
            "executions",
            ["integration_execution_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_integration_candidates_execution_workspace",
        ),
        _workspace_fk(
            "integration_candidates",
            "collaboration_input_selections",
            ["input_selection_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_integration_candidates_selection_workspace",
        ),
        _workspace_fk(
            "integration_candidates",
            "code_result_snapshots",
            ["code_result_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_integration_candidates_code_result_workspace",
        ),
        _workspace_fk(
            "integration_candidates",
            "integration_candidates",
            ["previous_candidate_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_integration_candidates_previous_workspace",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integration_candidates"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_integration_candidates_identity"),
        sa.Index(
            "ix_integration_candidates_root_history",
            "workspace_id",
            "root_task_id",
            "created_at",
        ),
    )

    op.create_table(
        "result_verification_results",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("root_task_id", _uuid(), nullable=False),
        sa.Column("candidate_id", _uuid(), nullable=False),
        sa.Column("execution_id", _uuid(), nullable=False),
        sa.Column("check_key", sa.String(255), nullable=False),
        sa.Column("check_version", sa.Integer(), nullable=False),
        sa.Column("check_hash", sa.String(64), nullable=False),
        sa.Column("environment_ref", sa.String(500), nullable=False),
        sa.Column("environment_hash", sa.String(64), nullable=False),
        sa.Column("actual_commit", sa.String(64), nullable=False),
        sa.Column("actual_tree", sa.String(64), nullable=False),
        sa.Column("operation_id", _uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("artifact_ids", _jsonb(), nullable=False),
        sa.Column("failure_code", sa.String(128)),
        sa.Column("payload", _jsonb(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 1 AND check_version >= 1 AND attempt >= 1 AND "
            "check_hash ~ '^[0-9a-f]{64}$' AND environment_hash ~ '^[0-9a-f]{64}$'",
            name="ck_result_verification_results_protocol",
        ),
        sa.CheckConstraint(
            "status IN ('passed', 'failed', 'not_run', 'unknown', 'interrupted', 'blocked')",
            name="ck_result_verification_results_status",
        ),
        sa.CheckConstraint(
            "actual_commit ~ '^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$' AND "
            "actual_tree ~ '^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$' AND "
            "jsonb_typeof(artifact_ids) = 'array' AND jsonb_typeof(payload) = 'object'",
            name="ck_result_verification_results_evidence",
        ),
        _workspace_fk(
            "result_verification_results",
            "tasks",
            ["root_task_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_result_verification_results_root_workspace",
        ),
        _workspace_fk(
            "result_verification_results",
            "integration_candidates",
            ["candidate_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_result_verification_results_candidate_workspace",
        ),
        _workspace_fk(
            "result_verification_results",
            "executions",
            ["execution_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_result_verification_results_execution_workspace",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_result_verification_results"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_result_verification_results_identity"),
        sa.UniqueConstraint(
            "workspace_id",
            "candidate_id",
            "check_key",
            "attempt",
            name="uq_result_verification_results_attempt",
        ),
        sa.Index(
            "ix_result_verification_results_candidate",
            "workspace_id",
            "candidate_id",
            "check_key",
            "created_at",
        ),
    )

    op.create_table(
        "task_acceptance_decisions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("root_task_id", _uuid(), nullable=False),
        sa.Column("candidate_id", _uuid(), nullable=False),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("plan_content_hash", sa.String(64), nullable=False),
        sa.Column("acceptance_basis_hash", sa.String(64), nullable=False),
        sa.Column("result_version", sa.Integer(), nullable=False),
        sa.Column("criteria_json", _jsonb(), nullable=False),
        sa.Column("actor_type", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("command_id", _uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "candidate_hash ~ '^[0-9a-f]{64}$' AND plan_content_hash ~ '^[0-9a-f]{64}$' "
            "AND acceptance_basis_hash ~ '^[0-9a-f]{64}$' AND result_version >= 1",
            name="ck_task_acceptance_decisions_protocol",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(criteria_json) = 'array' AND jsonb_array_length(criteria_json) >= 1",
            name="ck_task_acceptance_decisions_criteria_array",
        ),
        _workspace_fk(
            "task_acceptance_decisions",
            "tasks",
            ["root_task_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_task_acceptance_decisions_root_workspace",
        ),
        _workspace_fk(
            "task_acceptance_decisions",
            "integration_candidates",
            ["candidate_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_task_acceptance_decisions_candidate_workspace",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_acceptance_decisions"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_task_acceptance_decisions_identity"),
        sa.Index(
            "ix_task_acceptance_decisions_root_history",
            "workspace_id",
            "root_task_id",
            "created_at",
        ),
    )

    op.create_table(
        "result_rework_records",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("root_task_id", _uuid(), nullable=False),
        sa.Column("candidate_id", _uuid(), nullable=False),
        sa.Column("source_issue_keys_json", _jsonb(), nullable=False),
        sa.Column("target_worker_item_ids_json", _jsonb(), nullable=False),
        sa.Column("impact_worker_item_ids_json", _jsonb(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("round_number", sa.Integer()),
        sa.Column("source_command_id", _uuid(), nullable=False),
        sa.Column("request_ids_json", _jsonb(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('automatic', 'user') AND (round_number IS NULL OR round_number >= 1)",
            name="ck_result_rework_records_mode",
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'running', 'succeeded', 'blocked', 'failed')",
            name="ck_result_rework_records_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_issue_keys_json) = 'array' AND "
            "jsonb_typeof(target_worker_item_ids_json) = 'array' AND "
            "jsonb_typeof(impact_worker_item_ids_json) = 'array' AND "
            "jsonb_typeof(request_ids_json) = 'array'",
            name="ck_result_rework_records_json_arrays",
        ),
        _workspace_fk(
            "result_rework_records",
            "tasks",
            ["root_task_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_result_rework_records_root_workspace",
        ),
        _workspace_fk(
            "result_rework_records",
            "integration_candidates",
            ["candidate_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_result_rework_records_candidate_workspace",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_result_rework_records"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_result_rework_records_identity"),
        sa.Index(
            "ix_result_rework_records_root_history",
            "workspace_id",
            "root_task_id",
            "created_at",
        ),
    )

    op.create_table(
        "result_apply_operations",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("root_task_id", _uuid(), nullable=False),
        sa.Column("acceptance_id", _uuid(), nullable=False),
        sa.Column("candidate_id", _uuid(), nullable=False),
        sa.Column("repository_ref", sa.String(500), nullable=False),
        sa.Column("target_ref", sa.String(1024), nullable=False),
        sa.Column("expected_old_commit", sa.String(64), nullable=False),
        sa.Column("accepted_commit", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(255)),
        sa.Column("lease_token", sa.String(255)),
        sa.Column("lease_expiry", sa.DateTime(timezone=True)),
        sa.Column("receipt_ref", sa.String(1024)),
        sa.Column("failure_code", sa.String(128)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_ref LIKE 'refs/%' AND expected_old_commit ~ '^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$' "
            "AND accepted_commit ~ '^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$'",
            name="ck_result_apply_operations_git_identity",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'blocked', 'unknown', 'failed') "
            "AND attempt >= 1 AND version >= 1",
            name="ck_result_apply_operations_state",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_expiry IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expiry IS NOT NULL)",
            name="ck_result_apply_operations_lease_pair",
        ),
        _workspace_fk(
            "result_apply_operations",
            "tasks",
            ["root_task_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_result_apply_operations_root_workspace",
        ),
        _workspace_fk(
            "result_apply_operations",
            "task_acceptance_decisions",
            ["acceptance_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_result_apply_operations_acceptance_workspace",
        ),
        _workspace_fk(
            "result_apply_operations",
            "integration_candidates",
            ["candidate_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_result_apply_operations_candidate_workspace",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_result_apply_operations"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_result_apply_operations_identity"),
        sa.Index(
            "uq_result_apply_operations_active_root",
            "workspace_id",
            "root_task_id",
            unique=True,
            postgresql_where=sa.text(
                "status IN ('pending', 'running', 'blocked', 'unknown')"
            ),
        ),
        sa.Index(
            "ix_result_apply_operations_claims",
            "workspace_id",
            "status",
            "lease_expiry",
            "created_at",
        ),
    )

    op.create_table(
        "task_result_heads",
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("root_task_id", _uuid(), nullable=False),
        sa.Column("current_candidate_id", _uuid()),
        sa.Column("current_acceptance_id", _uuid()),
        sa.Column("result_version", sa.Integer(), nullable=False),
        sa.Column("active_operation_id", _uuid()),
        sa.Column("auto_rounds_used", sa.Integer(), nullable=False),
        sa.Column("auto_rounds_limit", sa.Integer(), nullable=False),
        sa.Column("auto_seconds_limit", sa.Integer(), nullable=False),
        sa.Column("auto_started_at", sa.DateTime(timezone=True)),
        sa.Column("auto_deadline", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "result_version >= 1 AND auto_rounds_used >= 0 AND auto_rounds_limit >= 0 "
            "AND auto_seconds_limit >= 1",
            name="ck_task_result_heads_versions",
        ),
        sa.CheckConstraint(
            "(auto_started_at IS NULL AND auto_deadline IS NULL) OR "
            "(auto_started_at IS NOT NULL AND auto_deadline IS NOT NULL AND auto_deadline > auto_started_at)",
            name="ck_task_result_heads_timer",
        ),
        _workspace_fk(
            "task_result_heads",
            "tasks",
            ["root_task_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_task_result_heads_root_workspace",
        ),
        _workspace_fk(
            "task_result_heads",
            "integration_candidates",
            ["current_candidate_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_task_result_heads_candidate_workspace",
        ),
        _workspace_fk(
            "task_result_heads",
            "task_acceptance_decisions",
            ["current_acceptance_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_task_result_heads_acceptance_workspace",
        ),
        _workspace_fk(
            "task_result_heads",
            "result_apply_operations",
            ["active_operation_id", "workspace_id"],
            ["id", "workspace_id"],
            "fk_task_result_heads_operation_workspace",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "root_task_id",
            name="pk_task_result_heads_identity",
        ),
        sa.Index(
            "ix_task_result_heads_active_operation",
            "workspace_id",
            "active_operation_id",
        ),
    )

    op.add_column(
        "execution_requests",
        sa.Column("result_derivation_kind", sa.String(32)),
    )
    op.add_column("execution_requests", sa.Column("source_candidate_id", _uuid()))
    op.add_column("execution_requests", sa.Column("rework_record_id", _uuid()))
    op.create_check_constraint(
        "ck_execution_requests_result_derivation",
        "execution_requests",
        "(result_derivation_kind IS NULL AND source_candidate_id IS NULL AND rework_record_id IS NULL) OR "
        "(result_derivation_kind = 'verification' AND source_candidate_id IS NOT NULL AND rework_record_id IS NULL) OR "
        "(result_derivation_kind = 'rework' AND source_candidate_id IS NOT NULL AND rework_record_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_execution_requests_source_candidate_workspace",
        "execution_requests",
        "integration_candidates",
        ["source_candidate_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_execution_requests_rework_record_workspace",
        "execution_requests",
        "result_rework_records",
        ["rework_record_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_execution_requests_result_derivation",
        "execution_requests",
        ["workspace_id", "root_task_id", "result_derivation_kind", "created_at"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "R6 result integration migration is forward-only; immutable result evidence must not be dropped"
    )
