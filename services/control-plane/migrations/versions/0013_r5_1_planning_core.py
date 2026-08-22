"""Persist the R5.1 planning control-loop core facts.

Revision ID: 0013_r5_1_planning_core
Revises: 0012_r4_1_worker_items
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_r5_1_planning_core"
down_revision: str | None = "0012_r4_1_worker_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "planning_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_key", sa.String(length=255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column(
            "planner_module_instance_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("planner_module_config_version", sa.Integer(), nullable=False),
        sa.Column("planner_interface_version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("user_selectable", sa.Boolean(), nullable=False),
        sa.Column("policy", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="ck_planning_profiles_version"),
        sa.CheckConstraint(
            "planner_module_config_version >= 1",
            name="ck_planning_profiles_module_config_version",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_planning_profiles_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(policy) = 'object'",
            name="ck_planning_profiles_policy_object",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_planning_profiles_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["planner_module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_profiles_module_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_planning_profiles_identity"),
        sa.UniqueConstraint(
            "workspace_id",
            "profile_key",
            "version",
            name="uq_planning_profiles_workspace_key_version",
        ),
        sa.Index(
            "ix_planning_profiles_workspace_status_selectable",
            "workspace_id",
            "status",
            "user_selectable",
        ),
    )

    op.create_table(
        "planning_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_brief_revision", sa.Integer(), nullable=True),
        sa.Column("current_proposal_revision", sa.Integer(), nullable=True),
        sa.Column("current_proposal_content_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "planning_profile_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("planning_profile_version", sa.Integer(), nullable=False),
        sa.Column("profile_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("source_type", sa.String(length=100), nullable=False),
        sa.Column("source_ref", sa.String(length=500), nullable=True),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
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
            "kind IN ('create', 'revision')", name="ck_planning_requests_kind"
        ),
        sa.CheckConstraint(
            "status IN ('submitted', 'planning', 'waiting_for_user', "
            "'proposal_ready', 'materialized', 'failed', 'conflicted', "
            "'rejected', 'cancelled')",
            name="ck_planning_requests_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_planning_requests_version"),
        sa.CheckConstraint(
            "planning_profile_version >= 1",
            name="ck_planning_requests_profile_version",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(profile_snapshot) = 'object'",
            name="ck_planning_requests_profile_snapshot_object",
        ),
        sa.CheckConstraint(
            "current_proposal_content_hash IS NULL OR "
            "current_proposal_content_hash ~ '^[0-9a-fA-F]{64}$'",
            name="ck_planning_requests_proposal_hash",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_planning_requests_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["planning_profile_id", "workspace_id"],
            ["planning_profiles.id", "planning_profiles.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_requests_profile_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_planning_requests_identity"),
        sa.Index("ix_planning_requests_workspace_status", "workspace_id", "status"),
    )

    op.create_table(
        "planning_brief_revisions",
        sa.Column(
            "planning_request_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("revision >= 1", name="ck_planning_briefs_revision"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_planning_briefs_payload_object",
        ),
        sa.ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_briefs_request_workspace",
        ),
        sa.PrimaryKeyConstraint("planning_request_id", "revision"),
        sa.UniqueConstraint(
            "planning_request_id",
            "workspace_id",
            "revision",
            name="uq_planning_briefs_request_workspace_revision",
        ),
    )

    op.create_table(
        "planning_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "planning_request_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("brief_revision", sa.Integer(), nullable=False),
        sa.Column(
            "planning_profile_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("planning_profile_version", sa.Integer(), nullable=False),
        sa.Column(
            "planner_module_instance_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("planner_module_config_version", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_kind", sa.String(length=64), nullable=True),
        sa.Column("capability_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("usage", postgresql.JSONB(), nullable=False),
        sa.Column("provider_ref", sa.String(length=500), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("diagnostic_refs", postgresql.JSONB(), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("attempt >= 1", name="ck_planning_runs_attempt"),
        sa.CheckConstraint("version >= 1", name="ck_planning_runs_version"),
        sa.CheckConstraint(
            "brief_revision >= 1", name="ck_planning_runs_brief_revision"
        ),
        sa.CheckConstraint(
            "planning_profile_version >= 1",
            name="ck_planning_runs_profile_version",
        ),
        sa.CheckConstraint(
            "planner_module_config_version >= 1",
            name="ck_planning_runs_module_config_version",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', "
            "'needs_clarification', 'failed', 'blocked', 'cancelled')",
            name="ck_planning_runs_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(capability_snapshot) = 'object'",
            name="ck_planning_runs_capability_snapshot_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(usage) = 'object'",
            name="ck_planning_runs_usage_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(diagnostic_refs) = 'array'",
            name="ck_planning_runs_diagnostic_refs_array",
        ),
        sa.ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_runs_request_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["planning_request_id", "workspace_id", "brief_revision"],
            [
                "planning_brief_revisions.planning_request_id",
                "planning_brief_revisions.workspace_id",
                "planning_brief_revisions.revision",
            ],
            ondelete="RESTRICT",
            name="fk_planning_runs_brief_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["planning_profile_id", "workspace_id"],
            ["planning_profiles.id", "planning_profiles.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_runs_profile_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["planner_module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_runs_module_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_planning_runs_identity"),
        sa.UniqueConstraint("operation_id", name="uq_planning_runs_operation_id"),
        sa.UniqueConstraint(
            "planning_request_id",
            "attempt",
            name="uq_planning_runs_request_attempt",
        ),
        sa.Index("ix_planning_runs_workspace_status", "workspace_id", "status"),
    )

    op.create_table(
        "planning_run_leases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("planning_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('active', 'released', 'expired')",
            name="ck_planning_run_leases_status",
        ),
        sa.CheckConstraint(
            "fencing_token >= 1",
            name="ck_planning_run_leases_fencing_token",
        ),
        sa.CheckConstraint("version >= 1", name="ck_planning_run_leases_version"),
        sa.ForeignKeyConstraint(
            ["planning_run_id", "workspace_id"],
            ["planning_runs.id", "planning_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_run_leases_run_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_planning_run_leases_identity"
        ),
        sa.Index(
            "uq_planning_run_leases_active_run",
            "planning_run_id",
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
        ),
        sa.Index("ix_planning_run_leases_expiry", "status", "expires_at"),
    )

    op.create_table(
        "planning_clarifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "planning_request_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("planning_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("answer_type", sa.String(length=32), nullable=False),
        sa.Column("options", postgresql.JSONB(), nullable=False),
        sa.Column("blocking", sa.Boolean(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("answered_by", sa.String(length=255), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answer_brief_revision", sa.Integer(), nullable=True),
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
            "sequence >= 1", name="ck_planning_clarifications_sequence"
        ),
        sa.CheckConstraint("version >= 1", name="ck_planning_clarifications_version"),
        sa.CheckConstraint(
            "jsonb_typeof(options) = 'array'",
            name="ck_planning_clarifications_options_array",
        ),
        sa.CheckConstraint(
            "answer_brief_revision IS NULL OR answer_brief_revision >= 1",
            name="ck_planning_clarifications_answer_revision",
        ),
        sa.ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_clarifications_request_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["planning_run_id", "workspace_id"],
            ["planning_runs.id", "planning_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_clarifications_run_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "planning_request_id",
            "sequence",
            name="uq_planning_clarifications_request_sequence",
        ),
        sa.Index(
            "ix_planning_clarifications_workspace_request",
            "workspace_id",
            "planning_request_id",
        ),
    )

    op.create_table(
        "planning_proposal_revisions",
        sa.Column(
            "planning_request_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("brief_revision", sa.Integer(), nullable=False),
        sa.Column("planning_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_schema_version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column(
            "planner_module_instance_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("planner_module_config_version", sa.Integer(), nullable=False),
        sa.Column("decision_summary", postgresql.JSONB(), nullable=False),
        sa.Column("plan_template", postgresql.JSONB(), nullable=False),
        sa.Column("execution_intents", postgresql.JSONB(), nullable=False),
        sa.Column("impact_policy", postgresql.JSONB(), nullable=False),
        sa.Column("derived_diff", postgresql.JSONB(), nullable=False),
        sa.Column("canonical_payload", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("revision >= 1", name="ck_planning_proposals_revision"),
        sa.CheckConstraint(
            "brief_revision >= 1", name="ck_planning_proposals_brief_revision"
        ),
        sa.CheckConstraint(
            "proposal_schema_version >= 1",
            name="ck_planning_proposals_schema_version",
        ),
        sa.CheckConstraint(
            "profile_version >= 1", name="ck_planning_proposals_profile_version"
        ),
        sa.CheckConstraint(
            "planner_module_config_version >= 1",
            name="ck_planning_proposals_module_config_version",
        ),
        sa.CheckConstraint(
            "kind IN ('create', 'revision')", name="ck_planning_proposals_kind"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(decision_summary) = 'object'",
            name="ck_planning_proposals_summary_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(plan_template) = 'object'",
            name="ck_planning_proposals_template_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(execution_intents) = 'array'",
            name="ck_planning_proposals_intents_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(impact_policy) = 'object'",
            name="ck_planning_proposals_impact_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(derived_diff) = 'object'",
            name="ck_planning_proposals_diff_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(canonical_payload) = 'object'",
            name="ck_planning_proposals_canonical_object",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-fA-F]{64}$'",
            name="ck_planning_proposals_content_hash",
        ),
        sa.ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_proposals_request_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["planning_request_id", "workspace_id", "brief_revision"],
            [
                "planning_brief_revisions.planning_request_id",
                "planning_brief_revisions.workspace_id",
                "planning_brief_revisions.revision",
            ],
            ondelete="RESTRICT",
            name="fk_planning_proposals_brief_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["planning_run_id", "workspace_id"],
            ["planning_runs.id", "planning_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_proposals_run_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id", "workspace_id"],
            ["planning_profiles.id", "planning_profiles.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_proposals_profile_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["planner_module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_proposals_module_workspace",
        ),
        sa.PrimaryKeyConstraint("planning_request_id", "revision"),
        sa.UniqueConstraint(
            "planning_request_id",
            "workspace_id",
            "revision",
            name="uq_planning_proposals_request_workspace_revision",
        ),
        sa.Index(
            "ix_planning_proposals_workspace_request_revision",
            "workspace_id",
            "planning_request_id",
            "revision",
        ),
    )

    op.create_foreign_key(
        "fk_planning_requests_current_brief",
        "planning_requests",
        "planning_brief_revisions",
        ["id", "workspace_id", "current_brief_revision"],
        ["planning_request_id", "workspace_id", "revision"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_planning_requests_current_proposal",
        "planning_requests",
        "planning_proposal_revisions",
        ["id", "workspace_id", "current_proposal_revision"],
        ["planning_request_id", "workspace_id", "revision"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_planning_requests_current_proposal",
        "planning_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_planning_requests_current_brief",
        "planning_requests",
        type_="foreignkey",
    )
    op.drop_table("planning_proposal_revisions")
    op.drop_table("planning_clarifications")
    op.drop_table("planning_run_leases")
    op.drop_table("planning_runs")
    op.drop_table("planning_brief_revisions")
    op.drop_table("planning_requests")
    op.drop_table("planning_profiles")
