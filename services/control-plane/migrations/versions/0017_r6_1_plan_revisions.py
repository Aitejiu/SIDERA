"""Add explicit plan revision heads, transitions, lineages, and context patches.

Revision ID: 0017_r6_1_plan_revisions
Revises: 0016_r5_3_work_context_packages
Create Date: 2026-08-28
"""

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_r6_1_plan_revisions"
down_revision: str | None = "0016_r5_3_work_context_packages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ACTIVE_REVISION_STATUSES = (
    "'submitted', 'planning', 'waiting_for_user', 'proposal_ready', 'failed'"
)
ACTIVE_WORKER_TRANSITION_STATUSES = (
    "'pending', 'waiting_safe_boundary', 'applying', "
    "'waiting_activation', 'blocked_unknown'"
)


def upgrade() -> None:
    _add_compatibility_columns()
    _create_revision_request_constraints()
    _create_root_heads()
    _create_plan_transitions()
    _create_worker_transitions()
    _create_lineages()
    _create_context_patches()
    _create_context_patch_acknowledgements()
    _complete_materialization_constraints()
    _backfill_plan_heads()


def _add_compatibility_columns() -> None:
    op.add_column(
        "planning_requests",
        sa.Column("root_task_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "planning_requests",
        sa.Column(
            "base_plan_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.add_column(
        "planning_requests",
        sa.Column("base_plan_generation", sa.Integer(), nullable=True),
    )
    op.add_column(
        "planning_requests",
        sa.Column(
            "base_plan_content_hash", sa.String(length=64), nullable=True
        ),
    )
    op.add_column(
        "planning_requests",
        sa.Column("base_root_task_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "planning_requests",
        sa.Column("base_captured_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "planning_requests",
        sa.Column("conflict_reason", sa.String(length=100), nullable=True),
    )

    op.add_column(
        "proposal_materializations",
        sa.Column("kind", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "proposal_materializations",
        sa.Column("diff_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "proposal_materializations",
        sa.Column(
            "plan_transition_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )

    op.add_column(
        "execution_inputs",
        sa.Column("superseded_reason", sa.String(length=500), nullable=True),
    )
    op.drop_constraint(
        "ck_execution_inputs_status", "execution_inputs", type_="check"
    )
    op.create_check_constraint(
        "ck_execution_inputs_status",
        "execution_inputs",
        "status IN ('accepted', 'queued', 'dispatching', 'delivered', 'blocked', "
        "'failed', 'cancelled', 'superseded')",
    )
    op.create_check_constraint(
        "ck_execution_inputs_superseded_reason",
        "execution_inputs",
        "status <> 'superseded' OR superseded_reason IS NOT NULL",
    )


def _create_revision_request_constraints() -> None:
    op.create_check_constraint(
        "ck_planning_requests_base_generation",
        "planning_requests",
        "base_plan_generation IS NULL OR base_plan_generation >= 1",
    )
    op.create_check_constraint(
        "ck_planning_requests_base_root_version",
        "planning_requests",
        "base_root_task_version IS NULL OR base_root_task_version >= 1",
    )
    op.create_check_constraint(
        "ck_planning_requests_base_hash",
        "planning_requests",
        "base_plan_content_hash IS NULL OR "
        "base_plan_content_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_planning_requests_revision_base",
        "planning_requests",
        "(kind = 'create' AND root_task_id IS NULL "
        "AND base_plan_snapshot_id IS NULL AND base_plan_generation IS NULL "
        "AND base_plan_content_hash IS NULL AND base_root_task_version IS NULL "
        "AND base_captured_at IS NULL) OR "
        "(kind = 'revision' AND root_task_id IS NOT NULL "
        "AND base_plan_snapshot_id IS NOT NULL AND base_plan_generation IS NOT NULL "
        "AND base_plan_content_hash IS NOT NULL AND base_root_task_version IS NOT NULL "
        "AND base_captured_at IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_planning_requests_root_task_workspace",
        "planning_requests",
        "tasks",
        ["root_task_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_planning_requests_base_snapshot_workspace",
        "planning_requests",
        "task_plan_snapshots",
        ["base_plan_snapshot_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_planning_requests_active_revision",
        "planning_requests",
        ["workspace_id", "root_task_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN (" + ACTIVE_REVISION_STATUSES + ") AND kind = 'revision'"
        ),
    )


def _create_root_heads() -> None:
    op.create_table(
        "root_task_plan_heads",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "active_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("active_generation", sa.Integer(), nullable=False),
        sa.Column("active_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "active_generation >= 1", name="ck_plan_heads_generation"
        ),
        sa.CheckConstraint(
            "active_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_plan_heads_snapshot_hash",
        ),
        sa.CheckConstraint("version >= 1", name="ck_plan_heads_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_plan_heads_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_heads_root_task_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["active_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_heads_snapshot_workspace",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id", "root_task_id", name="pk_root_task_plan_heads"
        ),
        sa.Index(
            "ix_plan_heads_workspace_generation",
            "workspace_id",
            "active_generation",
        ),
    )


def _create_plan_transitions() -> None:
    op.create_table(
        "plan_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "planning_request_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "proposal_revision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "from_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("from_generation", sa.Integer(), nullable=False),
        sa.Column("to_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('converging', 'converged', 'degraded')",
            name="ck_plan_transitions_status",
        ),
        sa.CheckConstraint(
            "from_generation >= 1 AND to_generation = from_generation + 1",
            name="ck_plan_transitions_generations",
        ),
        sa.CheckConstraint(
            "from_snapshot_id <> to_snapshot_id",
            name="ck_plan_transitions_distinct_snapshots",
        ),
        sa.CheckConstraint("version >= 1", name="ck_plan_transitions_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_plan_transitions_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_transitions_root_task_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_transitions_request_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_revision_id", "workspace_id"],
            [
                "planning_proposal_revisions.id",
                "planning_proposal_revisions.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_plan_transitions_proposal_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["from_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_transitions_from_snapshot_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["to_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_transitions_to_snapshot_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_plan_transitions_identity"
        ),
        sa.UniqueConstraint(
            "planning_request_id",
            "proposal_revision_id",
            name="uq_plan_transitions_request_proposal",
        ),
        sa.Index(
            "sidera_plan_transitions_active",
            "workspace_id",
            "root_task_id",
            "started_at",
            postgresql_where=sa.text("status = 'converging'"),
        ),
    )


def _create_worker_transitions() -> None:
    op.create_table(
        "worker_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "plan_transition_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_subtask_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("to_subtask_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("fallback_action", sa.String(length=32), nullable=True),
        sa.Column("target_context_version", sa.Integer(), nullable=False),
        sa.Column(
            "source_execution_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("source_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "target_execution_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("target_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("writer_state", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "action IN ('preserve', 'move', 'patch', 'continue', 'create', 'retire')",
            name="ck_worker_transitions_action",
        ),
        sa.CheckConstraint(
            "fallback_action IS NULL OR fallback_action IN "
            "('preserve', 'move', 'patch', 'continue', 'create', 'retire')",
            name="ck_worker_transitions_fallback_action",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'waiting_safe_boundary', 'applying', "
            "'waiting_activation', 'converged', 'failed', 'blocked_unknown')",
            name="ck_worker_transitions_status",
        ),
        sa.CheckConstraint(
            "writer_state IN ('active', 'fenced', 'closed', 'unknown')",
            name="ck_worker_transitions_writer_state",
        ),
        sa.CheckConstraint(
            "target_context_version >= 1",
            name="ck_worker_transitions_target_context_version",
        ),
        sa.CheckConstraint(
            "fencing_token >= 0", name="ck_worker_transitions_fencing_token"
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_worker_transitions_attempt_count"
        ),
        sa.CheckConstraint("version >= 1", name="ck_worker_transitions_version"),
        sa.CheckConstraint(
            "lease_id IS NULL OR source_execution_id IS NOT NULL",
            name="ck_worker_transitions_lease_source",
        ),
        sa.ForeignKeyConstraint(
            ["plan_transition_id", "workspace_id"],
            ["plan_transitions.id", "plan_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_plan_transition_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_root_task_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_worker_item_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["from_subtask_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_from_subtask_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["to_subtask_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_to_subtask_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_source_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_source_session_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_target_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_target_session_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["lease_id", "source_execution_id", "workspace_id"],
            [
                "assignment_leases.id",
                "assignment_leases.execution_id",
                "assignment_leases.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_worker_transitions_lease_source_execution_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_worker_transitions_identity"
        ),
        sa.Index(
            "uq_worker_transitions_active_worker_item",
            "worker_item_id",
            unique=True,
            postgresql_where=sa.text(
                "status IN (" + ACTIVE_WORKER_TRANSITION_STATUSES + ")"
            ),
        ),
        sa.Index(
            "ix_worker_transitions_claimable",
            "status",
            "next_attempt_at",
        ),
    )


def _create_lineages() -> None:
    op.create_table(
        "plan_entity_lineages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "plan_transition_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "length(btrim(target_type)) > 0",
            name="ck_plan_entity_lineages_target_type",
        ),
        sa.CheckConstraint(
            "length(btrim(source_type)) > 0",
            name="ck_plan_entity_lineages_source_type",
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) > 0", name="ck_plan_entity_lineages_reason"
        ),
        sa.ForeignKeyConstraint(
            ["plan_transition_id", "workspace_id"],
            ["plan_transitions.id", "plan_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_entity_lineages_transition_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_plan_entity_lineages_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_transition_id",
            "target_type",
            "target_id",
            "source_type",
            "source_id",
            name="uq_plan_entity_lineages_identity",
        ),
        sa.Index(
            "ix_plan_entity_lineages_workspace_target",
            "workspace_id",
            "target_type",
            "target_id",
        ),
    )


def _create_context_patches() -> None:
    op.create_table(
        "context_patches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "plan_transition_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "worker_transition_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "base_plan_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("base_generation", sa.Integer(), nullable=False),
        sa.Column(
            "target_plan_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("target_generation", sa.Integer(), nullable=False),
        sa.Column(
            "base_context_package_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("base_package_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "base_work_context_revision_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("base_context_hash", sa.String(length=64), nullable=False),
        sa.Column("target_context_version", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column(
            "changed_requirements", postgresql.JSONB(), nullable=False
        ),
        sa.Column(
            "changed_acceptance_refs", postgresql.JSONB(), nullable=False
        ),
        sa.Column(
            "changed_dependency_refs", postgresql.JSONB(), nullable=False
        ),
        sa.Column("decision_refs", postgresql.JSONB(), nullable=False),
        sa.Column("artifact_refs", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("intervention_refs", postgresql.JSONB(), nullable=False),
        sa.Column("unfinished_work", sa.Text(), nullable=False),
        sa.Column("checkpoint_ref", sa.String(length=1000), nullable=False),
        sa.Column("patch_hash", sa.String(length=64), nullable=False),
        sa.Column("proposer", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "base_generation >= 1 AND target_generation = base_generation + 1",
            name="ck_context_patches_generation",
        ),
        sa.CheckConstraint(
            "target_context_version >= 1",
            name="ck_context_patches_target_context_version",
        ),
        sa.CheckConstraint(
            "scope = 'worker_item'", name="ck_context_patches_scope"
        ),
        sa.CheckConstraint(
            "base_package_hash ~ '^[0-9a-f]{64}$' AND "
            "base_context_hash ~ '^[0-9a-f]{64}$' AND "
            "patch_hash ~ '^[0-9a-f]{64}$'",
            name="ck_context_patches_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(changed_requirements) = 'array' AND "
            "jsonb_typeof(changed_acceptance_refs) = 'array' AND "
            "jsonb_typeof(changed_dependency_refs) = 'array' AND "
            "jsonb_typeof(decision_refs) = 'array' AND "
            "jsonb_typeof(artifact_refs) = 'array' AND "
            "jsonb_typeof(evidence_refs) = 'array' AND "
            "jsonb_typeof(intervention_refs) = 'array'",
            name="ck_context_patches_refs_arrays",
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_context_patches_expiry"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_context_patches_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patches_root_task_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patches_worker_item_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["plan_transition_id", "workspace_id"],
            ["plan_transitions.id", "plan_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patches_plan_transition_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["worker_transition_id", "workspace_id"],
            ["worker_transitions.id", "worker_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patches_worker_transition_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["base_plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patches_base_snapshot_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patches_target_snapshot_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["base_context_package_id", "workspace_id"],
            ["context_packages.id", "context_packages.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patches_base_package_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["base_work_context_revision_id", "workspace_id"],
            [
                "work_context_revisions.id",
                "work_context_revisions.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_context_patches_base_context_revision_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_context_patches_identity"),
        sa.UniqueConstraint("patch_hash", name="uq_context_patches_patch_hash"),
        sa.UniqueConstraint(
            "worker_transition_id",
            "target_context_version",
            name="uq_context_patches_transition_context_version",
        ),
        sa.Index(
            "ix_context_patches_workspace_worker_item",
            "workspace_id",
            "worker_item_id",
            "created_at",
        ),
    )


def _create_context_patch_acknowledgements() -> None:
    op.create_table(
        "context_patch_acknowledgements",
        sa.Column("patch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patch_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("accepted_sequence", sa.BigInteger(), nullable=False),
        sa.Column("adapter_ref", sa.String(length=500), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "patch_hash ~ '^[0-9a-f]{64}$'",
            name="ck_context_patch_acknowledgements_hash",
        ),
        sa.CheckConstraint(
            "accepted_sequence >= 0",
            name="ck_context_patch_acknowledgements_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["patch_id", "workspace_id"],
            ["context_patches.id", "context_patches.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patch_acknowledgements_patch_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patch_acknowledgements_execution_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patch_acknowledgements_session_workspace",
        ),
        sa.PrimaryKeyConstraint("patch_id"),
        sa.Index(
            "ix_context_patch_acknowledgements_workspace_execution",
            "workspace_id",
            "execution_id",
        ),
    )


def _complete_materialization_constraints() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE proposal_materializations AS materialization "
            "SET kind = request.kind "
            "FROM planning_requests AS request "
            "WHERE materialization.planning_request_id = request.id "
            "AND materialization.kind IS NULL"
        )
    )
    missing_kind = bind.execute(
        sa.text(
            "SELECT count(*) FROM proposal_materializations "
            "WHERE kind IS NULL"
        )
    ).scalar_one()
    if missing_kind:
        raise RuntimeError(
            "cannot backfill proposal_materializations.kind for existing rows"
        )
    op.alter_column(
        "proposal_materializations",
        "kind",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_proposal_materializations_kind",
        "proposal_materializations",
        "kind IN ('create', 'revision')",
    )
    op.create_check_constraint(
        "ck_proposal_materializations_diff_hash",
        "proposal_materializations",
        "diff_hash IS NULL OR diff_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_foreign_key(
        "fk_proposal_materializations_transition_workspace",
        "proposal_materializations",
        "plan_transitions",
        ["plan_transition_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )


def _backfill_plan_heads() -> None:
    bind = op.get_bind()
    inconsistent = bind.execute(
        sa.text(
            "SELECT count(*) "
            "FROM task_plan_snapshots AS snapshot "
            "LEFT JOIN tasks AS root_task ON root_task.id = snapshot.root_task_id "
            "WHERE root_task.id IS NULL "
            "OR root_task.workspace_id <> snapshot.workspace_id"
        )
    ).scalar_one()
    if inconsistent:
        raise RuntimeError(
            "task_plan_snapshots contains a workspace/root task inconsistency"
        )

    duplicates = bind.execute(
        sa.text(
            "SELECT workspace_id, root_task_id, generation "
            "FROM task_plan_snapshots "
            "GROUP BY workspace_id, root_task_id, generation "
            "HAVING count(*) > 1"
        )
    ).fetchall()
    if duplicates:
        raise RuntimeError(
            "task_plan_snapshots contains duplicate root/generation rows"
        )

    rows = bind.execute(
        sa.text(
            "SELECT workspace_id, root_task_id, id, generation, payload "
            "FROM task_plan_snapshots "
            "ORDER BY workspace_id, root_task_id, generation DESC"
        )
    ).mappings()
    seen_roots: set[tuple[object, object]] = set()
    for row in rows:
        root_key = (row["workspace_id"], row["root_task_id"])
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)
        payload = row["payload"]
        if not isinstance(payload, dict):
            raise TypeError(
                f"snapshot {row['id']} payload is not a JSON object"
            )
        try:
            canonical = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            if json.loads(canonical) != payload:
                raise ValueError("canonical JSON round-trip changed payload")
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"snapshot {row['id']} payload cannot be canonicalized"
            ) from exc
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        bind.execute(
            sa.text(
                "INSERT INTO root_task_plan_heads "
                "(workspace_id, root_task_id, active_snapshot_id, "
                "active_generation, active_snapshot_hash, version) "
                "VALUES (:workspace_id, :root_task_id, :snapshot_id, "
                ":generation, :snapshot_hash, 1)"
            ),
            {
                "workspace_id": row["workspace_id"],
                "root_task_id": row["root_task_id"],
                "snapshot_id": row["id"],
                "generation": row["generation"],
                "snapshot_hash": content_hash,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    superseded_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM execution_inputs WHERE status = 'superseded'"
        )
    ).scalar_one()
    if superseded_count:
        raise RuntimeError(
            "cannot downgrade 0017 while superseded execution inputs exist"
        )

    op.drop_constraint(
        "fk_proposal_materializations_transition_workspace",
        "proposal_materializations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_proposal_materializations_diff_hash",
        "proposal_materializations",
        type_="check",
    )
    op.drop_constraint(
        "ck_proposal_materializations_kind",
        "proposal_materializations",
        type_="check",
    )
    op.drop_column("proposal_materializations", "plan_transition_id")
    op.drop_column("proposal_materializations", "diff_hash")
    op.drop_column("proposal_materializations", "kind")

    op.drop_index(
        "uq_planning_requests_active_revision", table_name="planning_requests"
    )
    op.drop_constraint(
        "fk_planning_requests_base_snapshot_workspace",
        "planning_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_planning_requests_root_task_workspace",
        "planning_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_planning_requests_revision_base", "planning_requests", type_="check"
    )
    op.drop_constraint(
        "ck_planning_requests_base_hash", "planning_requests", type_="check"
    )
    op.drop_constraint(
        "ck_planning_requests_base_root_version",
        "planning_requests",
        type_="check",
    )
    op.drop_constraint(
        "ck_planning_requests_base_generation",
        "planning_requests",
        type_="check",
    )
    for column_name in (
        "conflict_reason",
        "base_captured_at",
        "base_root_task_version",
        "base_plan_content_hash",
        "base_plan_generation",
        "base_plan_snapshot_id",
        "root_task_id",
    ):
        op.drop_column("planning_requests", column_name)

    op.drop_constraint(
        "ck_execution_inputs_superseded_reason",
        "execution_inputs",
        type_="check",
    )
    op.drop_constraint(
        "ck_execution_inputs_status", "execution_inputs", type_="check"
    )
    op.create_check_constraint(
        "ck_execution_inputs_status",
        "execution_inputs",
        "status IN ('accepted', 'queued', 'dispatching', 'delivered', 'blocked', "
        "'failed', 'cancelled')",
    )
    op.drop_column("execution_inputs", "superseded_reason")

    op.drop_table("context_patch_acknowledgements")
    op.drop_table("context_patches")
    op.drop_table("plan_entity_lineages")
    op.drop_table("worker_transitions")
    op.drop_table("plan_transitions")
    op.drop_table("root_task_plan_heads")
