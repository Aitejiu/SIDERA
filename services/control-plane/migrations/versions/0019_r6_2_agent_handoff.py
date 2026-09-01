"""Add cross-agent handoff state, evidence, and lineage.

Revision ID: 0019_r6_2_agent_handoff
Revises: 0018_r6_1_execution_request_lineage
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_r6_2_agent_handoff"
down_revision: str | None = "0018_r6_1_execution_request_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _add_lineage_columns()
    _replace_execution_request_uniqueness()
    _create_handoff_proposals()
    _create_handoff_transitions()
    _create_handoff_recovery_decisions()
    _create_handoff_context_bundles()
    _create_workspace_transfers()
    _add_handoff_foreign_keys()
    _create_workspace_location_index()
    _create_immutability_triggers()


def downgrade() -> None:
    _drop_immutability_triggers()
    op.drop_index(
        "uq_execution_workspaces_active_location", table_name="execution_workspaces"
    )
    _drop_handoff_foreign_keys()
    op.drop_table("execution_workspace_transfers")
    op.drop_table("handoff_context_bundles")
    op.drop_table("handoff_recovery_decisions")
    op.drop_table("handoff_transitions")
    op.drop_table("handoff_proposals")
    _restore_execution_request_uniqueness()
    _drop_lineage_columns()


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


def _add_lineage_columns() -> None:
    op.add_column(
        "executions",
        sa.Column(
            "handoff_transition_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.add_column(
        "execution_requests",
        sa.Column(
            "handoff_transition_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.add_column(
        "execution_inputs",
        sa.Column("source_input_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "execution_inputs",
        sa.Column(
            "handoff_transition_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_unique_constraint(
        "uq_execution_inputs_identity", "execution_inputs", ["id", "workspace_id"]
    )
    op.create_check_constraint(
        "ck_execution_inputs_handoff_lineage_pair",
        "execution_inputs",
        "(source_input_id IS NULL) = (handoff_transition_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_execution_inputs_source_distinct",
        "execution_inputs",
        "source_input_id IS NULL OR source_input_id <> id",
    )
    op.create_foreign_key(
        "fk_execution_inputs_source_input_workspace",
        "execution_inputs",
        "execution_inputs",
        ["source_input_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )


def _drop_lineage_columns() -> None:
    op.drop_constraint(
        "fk_execution_inputs_source_input_workspace",
        "execution_inputs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_execution_inputs_source_distinct", "execution_inputs", type_="check"
    )
    op.drop_constraint(
        "ck_execution_inputs_handoff_lineage_pair", "execution_inputs", type_="check"
    )
    op.drop_constraint(
        "uq_execution_inputs_identity", "execution_inputs", type_="unique"
    )
    op.drop_column("execution_inputs", "handoff_transition_id")
    op.drop_column("execution_inputs", "source_input_id")
    op.drop_column("execution_requests", "handoff_transition_id")
    op.drop_column("executions", "handoff_transition_id")


def _replace_execution_request_uniqueness() -> None:
    op.create_index(
        "uq_execution_requests_proposal_worker_item_partial",
        "execution_requests",
        ["proposal_revision_id", "worker_item_key"],
        unique=True,
        postgresql_where=sa.text("handoff_transition_id IS NULL"),
    )
    op.drop_constraint(
        "uq_execution_requests_proposal_worker_item",
        "execution_requests",
        type_="unique",
    )
    op.execute(
        "ALTER INDEX uq_execution_requests_proposal_worker_item_partial "
        "RENAME TO uq_execution_requests_proposal_worker_item"
    )
    op.create_index(
        "uq_execution_requests_handoff_transition",
        "execution_requests",
        ["workspace_id", "handoff_transition_id"],
        unique=True,
        postgresql_where=sa.text("handoff_transition_id IS NOT NULL"),
    )


def _restore_execution_request_uniqueness() -> None:
    op.drop_index(
        "uq_execution_requests_handoff_transition", table_name="execution_requests"
    )
    op.drop_index(
        "uq_execution_requests_proposal_worker_item",
        table_name="execution_requests",
    )
    op.create_unique_constraint(
        "uq_execution_requests_proposal_worker_item",
        "execution_requests",
        ["proposal_revision_id", "worker_item_key"],
    )


def _create_handoff_proposals() -> None:
    op.create_table(
        "handoff_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subtask_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_execution_version", sa.Integer(), nullable=False),
        sa.Column("source_session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_session_version", sa.Integer(), nullable=False),
        sa.Column("source_session_last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("source_worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_worker_version", sa.Integer(), nullable=False),
        sa.Column("source_runtime_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "source_workspace_manifest_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("source_workspace_manifest_version", sa.Integer(), nullable=False),
        sa.Column(
            "source_context_package_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("source_context_package_hash", sa.String(length=64), nullable=False),
        sa.Column("source_worker_context_version", sa.Integer(), nullable=False),
        sa.Column(
            "observed_plan_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("observed_plan_generation", sa.Integer(), nullable=False),
        sa.Column("observed_plan_head_version", sa.Integer(), nullable=False),
        sa.Column("target_worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_worker_version", sa.Integer(), nullable=False),
        sa.Column("target_runtime_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "target_capability_snapshot_hash", sa.String(length=64), nullable=False
        ),
        sa.Column("target_policy_reason", postgresql.JSONB(), nullable=False),
        sa.Column("target_policy_facts", postgresql.JSONB(), nullable=False),
        sa.Column("transfer_mode", sa.String(length=32), nullable=False),
        sa.Column("handoff_reason", sa.Text(), nullable=False),
        sa.Column("handoff_instruction", sa.Text(), nullable=False),
        sa.Column("input_plan", postgresql.JSONB(), nullable=False),
        sa.Column("impact_summary", postgresql.JSONB(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("proposal_schema_version", sa.Integer(), nullable=False),
        sa.Column("proposal_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stale_reason", sa.String(length=255), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by_type", sa.String(length=50), nullable=False),
        sa.Column("created_by_id", sa.String(length=255), nullable=False),
        sa.Column("source_channel", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "proposal_schema_version >= 1", name="ck_handoff_proposals_schema_version"
        ),
        sa.CheckConstraint(
            "source_execution_version >= 1 AND source_session_version >= 1 "
            "AND source_worker_version >= 1 AND source_workspace_manifest_version >= 1 "
            "AND source_worker_context_version >= 1 AND observed_plan_generation >= 1 "
            "AND observed_plan_head_version >= 1 AND target_worker_version >= 1",
            name="ck_handoff_proposals_positive_versions",
        ),
        sa.CheckConstraint(
            "source_session_last_sequence >= 0",
            name="ck_handoff_proposals_source_sequence",
        ),
        sa.CheckConstraint(
            "transfer_mode = 'reuse_exact'", name="ck_handoff_proposals_transfer_mode"
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'confirmed', 'stale', 'cancelled', 'expired')",
            name="ck_handoff_proposals_status",
        ),
        sa.CheckConstraint(
            "source_channel = 'web'", name="ck_handoff_proposals_source_channel"
        ),
        sa.CheckConstraint(
            "proposal_hash ~ '^[0-9a-f]{64}$' AND source_context_package_hash ~ '^[0-9a-f]{64}$' "
            "AND target_capability_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_handoff_proposals_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object' AND jsonb_typeof(source_runtime_snapshot) = 'object' "
            "AND jsonb_typeof(target_runtime_snapshot) = 'object' "
            "AND jsonb_typeof(target_policy_reason) = 'object' "
            "AND jsonb_typeof(target_policy_facts) = 'object' "
            "AND jsonb_typeof(input_plan) = 'object' AND jsonb_typeof(impact_summary) = 'object'",
            name="ck_handoff_proposals_json_objects",
        ),
        sa.CheckConstraint("version >= 1", name="ck_handoff_proposals_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_root_task_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["subtask_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_subtask_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_worker_item_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_source_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_source_session_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_source_worker_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_workspace_manifest_id", "workspace_id"],
            ["execution_workspaces.id", "execution_workspaces.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_source_manifest_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_context_package_id", "workspace_id"],
            ["context_packages.id", "context_packages.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_source_package_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["observed_plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_plan_snapshot_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_target_worker_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_handoff_proposals_identity"),
        sa.Index(
            "uq_handoff_proposals_prepared_worker_item",
            "workspace_id",
            "worker_item_id",
            unique=True,
            postgresql_where=sa.text("status = 'prepared'"),
        ),
        sa.Index(
            "ix_handoff_proposals_workspace_status",
            "workspace_id",
            "status",
            "expires_at",
        ),
    )


def _create_handoff_transitions() -> None:
    op.create_table(
        "handoff_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source_workspace_manifest_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("target_worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_runtime_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("target_execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "target_workspace_manifest_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "handoff_context_bundle_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("target_context_version", sa.Integer(), nullable=False),
        sa.Column(
            "target_context_package_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("target_context_package_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("writer_state", sa.String(length=32), nullable=False),
        sa.Column("source_checkpoint_sequence", sa.BigInteger(), nullable=True),
        sa.Column("source_checkpoint_facts", postgresql.JSONB(), nullable=True),
        sa.Column("source_stop_sequence", sa.BigInteger(), nullable=True),
        sa.Column("source_stop_facts", postgresql.JSONB(), nullable=True),
        sa.Column(
            "source_workspace_freeze_operation_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("source_workspace_freeze_facts", postgresql.JSONB(), nullable=True),
        sa.Column("source_input_cutoff_sequence", sa.BigInteger(), nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("blocked_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("logical_attempt", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("confirmed_by_type", sa.String(length=50), nullable=False),
        sa.Column("confirmed_by_id", sa.String(length=255), nullable=False),
        sa.Column("confirmed_source_channel", sa.String(length=32), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'blocked', 'blocked_unknown', 'failed', 'converged')",
            name="ck_handoff_transitions_status",
        ),
        sa.CheckConstraint(
            "phase IN ('checkpoint_source', 'stop_source', 'freeze_source_workspace', "
            "'create_target_execution', 'assemble_bundle', 'transfer_workspace', "
            "'start_target_session', 'acknowledge_context', 'transfer_safe_inputs', "
            "'activate_target', 'complete')",
            name="ck_handoff_transitions_phase",
        ),
        sa.CheckConstraint(
            "writer_state IN ('source_active', 'source_stop_requested', 'writer_none', "
            "'target_starting', 'target_active', 'unknown')",
            name="ck_handoff_transitions_writer_state",
        ),
        sa.CheckConstraint(
            "target_context_version >= 1 AND source_input_cutoff_sequence >= 0 "
            "AND logical_attempt >= 1 AND version >= 1",
            name="ck_handoff_transitions_positive_fields",
        ),
        sa.CheckConstraint(
            "(source_checkpoint_sequence IS NULL OR source_checkpoint_sequence >= 0) "
            "AND (source_stop_sequence IS NULL OR source_stop_sequence >= 0)",
            name="ck_handoff_transitions_source_sequences",
        ),
        sa.CheckConstraint(
            "confirmed_source_channel = 'web'",
            name="ck_handoff_transitions_source_channel",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(target_runtime_snapshot) = 'object' "
            "AND (source_checkpoint_facts IS NULL OR jsonb_typeof(source_checkpoint_facts) = 'object') "
            "AND (source_stop_facts IS NULL OR jsonb_typeof(source_stop_facts) = 'object') "
            "AND (source_workspace_freeze_facts IS NULL OR jsonb_typeof(source_workspace_freeze_facts) = 'object')",
            name="ck_handoff_transitions_json_objects",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_worker_item_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id", "workspace_id"],
            ["handoff_proposals.id", "handoff_proposals.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_proposal_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_source_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_source_session_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_workspace_manifest_id", "workspace_id"],
            ["execution_workspaces.id", "execution_workspaces.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_source_manifest_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_target_worker_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_target_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_target_session_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_workspace_manifest_id", "workspace_id"],
            ["execution_workspaces.id", "execution_workspaces.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_target_manifest_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_context_package_id", "workspace_id"],
            ["context_packages.id", "context_packages.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_target_package_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_handoff_transitions_identity"
        ),
        sa.UniqueConstraint("proposal_id", name="uq_handoff_transitions_proposal"),
        sa.Index(
            "uq_handoff_transitions_active_worker_item",
            "workspace_id",
            "worker_item_id",
            unique=True,
            postgresql_where=sa.text(
                "status IN ('pending', 'running', 'blocked', 'blocked_unknown', 'failed')"
            ),
        ),
        sa.Index(
            "uq_handoff_transitions_active_source_session",
            "workspace_id",
            "source_session_id",
            unique=True,
            postgresql_where=sa.text(
                "status IN ('pending', 'running', 'blocked', 'blocked_unknown', 'failed')"
            ),
        ),
        sa.Index(
            "ix_handoff_transitions_coordinator",
            "workspace_id",
            "status",
            "next_attempt_at",
        ),
    )


def _create_handoff_recovery_decisions() -> None:
    op.create_table(
        "handoff_recovery_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "handoff_transition_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("expected_transition_version", sa.Integer(), nullable=False),
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=50), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("source_channel", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("replacement_target_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("resulting_operation_refs", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expected_transition_version >= 1 AND version >= 1",
            name="ck_handoff_recovery_decisions_versions",
        ),
        sa.CheckConstraint(
            "strategy IN ('retry_current_phase', 'retry_same_target', 'select_new_target', "
            "'continue_with_source_agent', 'confirm_source_stopped', 'confirm_target_absent', "
            "'confirm_target_active')",
            name="ck_handoff_recovery_decisions_strategy",
        ),
        sa.CheckConstraint(
            "source_channel = 'web'",
            name="ck_handoff_recovery_decisions_source_channel",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_typeof(resulting_operation_refs) = 'array' "
            "AND (replacement_target_snapshot IS NULL OR jsonb_typeof(replacement_target_snapshot) = 'object')",
            name="ck_handoff_recovery_decisions_json_shapes",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_handoff_recovery_decisions_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["handoff_transition_id", "workspace_id"],
            ["handoff_transitions.id", "handoff_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_recovery_decisions_transition_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_handoff_recovery_decisions_identity"
        ),
        sa.Index(
            "ix_handoff_recovery_decisions_transition_created",
            "workspace_id",
            "handoff_transition_id",
            "created_at",
        ),
    )


def _create_handoff_context_bundles() -> None:
    op.create_table(
        "handoff_context_bundles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "handoff_transition_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("logical_attempt", sa.Integer(), nullable=False),
        sa.Column("recovery_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_checkpoint_sequence", sa.BigInteger(), nullable=False),
        sa.Column("source_last_accepted_sequence", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_context_package_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("source_context_package_hash", sa.String(length=64), nullable=False),
        sa.Column("source_worker_context_version", sa.Integer(), nullable=False),
        sa.Column("target_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_runtime_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("target_context_version", sa.Integer(), nullable=False),
        sa.Column(
            "source_workspace_manifest_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("source_workspace_version", sa.Integer(), nullable=False),
        sa.Column("workspace_location_ref_hash", sa.String(length=64), nullable=False),
        sa.Column("repository_state", postgresql.JSONB(), nullable=False),
        sa.Column("artifact_refs", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("handoff_reason", sa.Text(), nullable=False),
        sa.Column("handoff_instruction", sa.Text(), nullable=False),
        sa.Column("delivered_input_refs", postgresql.JSONB(), nullable=False),
        sa.Column("transferable_input_refs", postgresql.JSONB(), nullable=False),
        sa.Column("excluded_input_refs", postgresql.JSONB(), nullable=False),
        sa.Column("bundle_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "schema_version >= 1", name="ck_handoff_context_bundles_schema_version"
        ),
        sa.CheckConstraint(
            "logical_attempt >= 1 AND source_checkpoint_sequence >= 0 "
            "AND source_last_accepted_sequence >= source_checkpoint_sequence "
            "AND source_worker_context_version >= 1 AND target_context_version >= 1 "
            "AND source_workspace_version >= 1",
            name="ck_handoff_context_bundles_positive_fields",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(repository_state) = 'array' AND jsonb_typeof(artifact_refs) = 'array' "
            "AND jsonb_typeof(evidence_refs) = 'array' AND jsonb_typeof(delivered_input_refs) = 'array' "
            "AND jsonb_typeof(transferable_input_refs) = 'array' "
            "AND jsonb_typeof(excluded_input_refs) = 'array'",
            name="ck_handoff_context_bundles_json_arrays",
        ),
        sa.CheckConstraint(
            "source_context_package_hash ~ '^[0-9a-f]{64}$' "
            "AND target_runtime_snapshot_hash ~ '^[0-9a-f]{64}$' "
            "AND workspace_location_ref_hash ~ '^[0-9a-f]{64}$' "
            "AND bundle_hash ~ '^[0-9a-f]{64}$'",
            name="ck_handoff_context_bundles_hashes",
        ),
        sa.CheckConstraint("version >= 1", name="ck_handoff_context_bundles_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_worker_item_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["handoff_transition_id", "workspace_id"],
            ["handoff_transitions.id", "handoff_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_transition_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["recovery_decision_id", "workspace_id"],
            [
                "handoff_recovery_decisions.id",
                "handoff_recovery_decisions.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_recovery_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_source_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_source_session_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_context_package_id", "workspace_id"],
            ["context_packages.id", "context_packages.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_source_package_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_target_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_target_worker_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_workspace_manifest_id", "workspace_id"],
            ["execution_workspaces.id", "execution_workspaces.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_source_manifest_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_handoff_context_bundles_identity"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "handoff_transition_id",
            "logical_attempt",
            name="uq_handoff_context_bundles_attempt",
        ),
        sa.Index(
            "uq_handoff_context_bundles_hash",
            "workspace_id",
            "bundle_hash",
            unique=True,
        ),
    )


def _create_workspace_transfers() -> None:
    op.create_table(
        "execution_workspace_transfers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "handoff_transition_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("logical_attempt", sa.Integer(), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_manifest_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_manifest_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("target_fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("location_ref_hash", sa.String(length=64), nullable=False),
        sa.Column("repository_state_digest", sa.String(length=64), nullable=False),
        sa.Column("transfer_mode", sa.String(length=32), nullable=False),
        sa.Column(
            "provider_operation_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "logical_attempt >= 1 AND source_fencing_token >= 1 "
            "AND target_fencing_token > source_fencing_token AND version >= 1",
            name="ck_execution_workspace_transfers_fencing",
        ),
        sa.CheckConstraint(
            "transfer_mode = 'reuse_exact'",
            name="ck_execution_workspace_transfers_mode",
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'succeeded', 'failed', 'unknown')",
            name="ck_execution_workspace_transfers_status",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND target_manifest_id IS NOT NULL) OR status <> 'succeeded'",
            name="ck_execution_workspace_transfers_success_target",
        ),
        sa.CheckConstraint(
            "location_ref_hash ~ '^[0-9a-f]{64}$' AND repository_state_digest ~ '^[0-9a-f]{64}$'",
            name="ck_execution_workspace_transfers_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_execution_workspace_transfers_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["handoff_transition_id", "workspace_id"],
            ["handoff_transitions.id", "handoff_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_workspace_transfers_transition_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_workspace_transfers_source_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_workspace_transfers_target_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_manifest_id", "workspace_id"],
            ["execution_workspaces.id", "execution_workspaces.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_workspace_transfers_source_manifest_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_manifest_id", "workspace_id"],
            ["execution_workspaces.id", "execution_workspaces.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_workspace_transfers_target_manifest_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_execution_workspace_transfers_identity"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "handoff_transition_id",
            "logical_attempt",
            name="uq_execution_workspace_transfers_attempt",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "source_manifest_id",
            "target_manifest_id",
            name="uq_execution_workspace_transfers_manifest_pair",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "provider_operation_id",
            name="uq_execution_workspace_transfers_provider_operation",
        ),
        sa.Index(
            "ix_execution_workspace_transfers_coordinator",
            "workspace_id",
            "status",
            "updated_at",
        ),
    )


def _add_handoff_foreign_keys() -> None:
    op.create_foreign_key(
        "fk_executions_handoff_transition_workspace",
        "executions",
        "handoff_transitions",
        ["handoff_transition_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_execution_requests_handoff_transition_workspace",
        "execution_requests",
        "handoff_transitions",
        ["handoff_transition_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_execution_inputs_handoff_transition_workspace",
        "execution_inputs",
        "handoff_transitions",
        ["handoff_transition_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_handoff_transitions_context_bundle_workspace",
        "handoff_transitions",
        "handoff_context_bundles",
        ["handoff_context_bundle_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )


def _drop_handoff_foreign_keys() -> None:
    op.drop_constraint(
        "fk_handoff_transitions_context_bundle_workspace",
        "handoff_transitions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_execution_inputs_handoff_transition_workspace",
        "execution_inputs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_execution_requests_handoff_transition_workspace",
        "execution_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_executions_handoff_transition_workspace", "executions", type_="foreignkey"
    )


def _create_workspace_location_index() -> None:
    op.create_index(
        "uq_execution_workspaces_active_location",
        "execution_workspaces",
        ["workspace_id", "location_ref"],
        unique=True,
        postgresql_where=sa.text("lifecycle = 'active'"),
    )


def _create_immutability_triggers() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sidera_reject_handoff_proposal_payload_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.payload IS DISTINCT FROM OLD.payload
               OR NEW.proposal_hash IS DISTINCT FROM OLD.proposal_hash THEN
                RAISE EXCEPTION 'handoff proposal payload is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_handoff_proposals_immutable_payload
        BEFORE UPDATE ON handoff_proposals
        FOR EACH ROW EXECUTE FUNCTION sidera_reject_handoff_proposal_payload_update()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sidera_reject_handoff_bundle_payload_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.bundle_hash IS DISTINCT FROM OLD.bundle_hash
               OR NEW.repository_state IS DISTINCT FROM OLD.repository_state
               OR NEW.artifact_refs IS DISTINCT FROM OLD.artifact_refs
               OR NEW.evidence_refs IS DISTINCT FROM OLD.evidence_refs
               OR NEW.delivered_input_refs IS DISTINCT FROM OLD.delivered_input_refs
               OR NEW.transferable_input_refs IS DISTINCT FROM OLD.transferable_input_refs
               OR NEW.excluded_input_refs IS DISTINCT FROM OLD.excluded_input_refs THEN
                RAISE EXCEPTION 'handoff context bundle payload is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_handoff_context_bundles_immutable_payload
        BEFORE UPDATE ON handoff_context_bundles
        FOR EACH ROW EXECUTE FUNCTION sidera_reject_handoff_bundle_payload_update()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sidera_reject_workspace_transfer_provenance_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.handoff_transition_id IS DISTINCT FROM OLD.handoff_transition_id
               OR NEW.logical_attempt IS DISTINCT FROM OLD.logical_attempt
               OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
               OR NEW.source_manifest_id IS DISTINCT FROM OLD.source_manifest_id
               OR NEW.target_manifest_id IS DISTINCT FROM OLD.target_manifest_id
               OR NEW.source_execution_id IS DISTINCT FROM OLD.source_execution_id
               OR NEW.target_execution_id IS DISTINCT FROM OLD.target_execution_id
               OR NEW.source_fencing_token IS DISTINCT FROM OLD.source_fencing_token
               OR NEW.target_fencing_token IS DISTINCT FROM OLD.target_fencing_token
               OR NEW.location_ref_hash IS DISTINCT FROM OLD.location_ref_hash
               OR NEW.repository_state_digest IS DISTINCT FROM OLD.repository_state_digest
               OR NEW.provider_operation_id IS DISTINCT FROM OLD.provider_operation_id
               OR NEW.transfer_mode IS DISTINCT FROM OLD.transfer_mode THEN
                RAISE EXCEPTION 'workspace transfer provenance is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_execution_workspace_transfers_immutable_provenance
        BEFORE UPDATE ON execution_workspace_transfers
        FOR EACH ROW EXECUTE FUNCTION sidera_reject_workspace_transfer_provenance_update()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sidera_reject_handoff_recovery_decision_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'handoff recovery decisions are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_handoff_recovery_decisions_append_only
        BEFORE UPDATE OR DELETE ON handoff_recovery_decisions
        FOR EACH ROW EXECUTE FUNCTION sidera_reject_handoff_recovery_decision_mutation()
        """
    )


def _drop_immutability_triggers() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_handoff_recovery_decisions_append_only ON handoff_recovery_decisions"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_execution_workspace_transfers_immutable_provenance ON execution_workspace_transfers"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_handoff_context_bundles_immutable_payload ON handoff_context_bundles"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_handoff_proposals_immutable_payload ON handoff_proposals"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS sidera_reject_handoff_recovery_decision_mutation()"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS sidera_reject_workspace_transfer_provenance_update()"
    )
    op.execute("DROP FUNCTION IF EXISTS sidera_reject_handoff_bundle_payload_update()")
    op.execute(
        "DROP FUNCTION IF EXISTS sidera_reject_handoff_proposal_payload_update()"
    )
