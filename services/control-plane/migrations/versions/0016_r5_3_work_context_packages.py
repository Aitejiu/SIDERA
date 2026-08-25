"""Add WorkContext, context package, and candidate submission schema.

Revision ID: 0016_r5_3_work_context_packages
Revises: 0015_r5_1_1_worker_runtime_binding
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_r5_3_work_context_packages"
down_revision: str | None = "0015_r5_1_1_worker_runtime_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The current-revision pointer is added after both sides of the cycle exist.
    op.create_table(
        "work_contexts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("planning_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "current_revision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("current_revision_number", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived', 'legacy_incomplete')",
            name="ck_work_contexts_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_work_contexts_version"),
        sa.CheckConstraint(
            "current_revision_number >= 1",
            name="ck_work_contexts_current_revision_number",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_work_contexts_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_work_contexts_planning_request_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_work_contexts_root_task_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_work_contexts_identity"),
        sa.UniqueConstraint(
            "planning_request_id",
            "workspace_id",
            name="uq_work_contexts_planning_request",
        ),
        sa.Index(
            "uq_work_contexts_root_task",
            "root_task_id",
            "workspace_id",
            unique=True,
            postgresql_where=sa.text("root_task_id IS NOT NULL"),
        ),
        sa.Index("ix_work_contexts_workspace_status", "workspace_id", "status"),
    )

    op.create_table(
        "work_context_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_context_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column(
            "brief_planning_request_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("brief_revision", sa.Integer(), nullable=False),
        sa.Column(
            "proposal_revision_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("plan_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "revision_number >= 1",
            name="ck_work_context_revisions_revision_number",
        ),
        sa.CheckConstraint(
            "brief_revision >= 1",
            name="ck_work_context_revisions_brief_revision",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_work_context_revisions_snapshot_object",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_work_context_revisions_content_hash",
        ),
        sa.CheckConstraint(
            "reason IN ('request_created', 'clarification_accepted', "
            "'proposal_confirmed', 'user_intervention', 'replanned', "
            "'knowledge_attached', 'legacy_backfill')",
            name="ck_work_context_revisions_reason",
        ),
        sa.ForeignKeyConstraint(
            ["work_context_id", "workspace_id"],
            ["work_contexts.id", "work_contexts.workspace_id"],
            ondelete="RESTRICT",
            name="fk_work_context_revisions_context_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["brief_planning_request_id", "workspace_id", "brief_revision"],
            [
                "planning_brief_revisions.planning_request_id",
                "planning_brief_revisions.workspace_id",
                "planning_brief_revisions.revision",
            ],
            ondelete="RESTRICT",
            name="fk_work_context_revisions_brief",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_revision_id", "workspace_id"],
            [
                "planning_proposal_revisions.id",
                "planning_proposal_revisions.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_work_context_revisions_proposal",
        ),
        sa.ForeignKeyConstraint(
            ["plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_work_context_revisions_plan_snapshot",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "work_context_id",
            "revision_number",
            name="uq_work_context_revisions_context_revision",
        ),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_work_context_revisions_identity"
        ),
        sa.Index(
            "ix_work_context_revisions_workspace_context_number",
            "workspace_id",
            "work_context_id",
            "revision_number",
        ),
    )
    op.create_foreign_key(
        "fk_work_contexts_current_revision",
        "work_contexts",
        "work_context_revisions",
        ["current_revision_id"],
        ["id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "context_assembly_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("consumer_type", sa.String(length=32), nullable=False),
        sa.Column("consumer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "work_context_revision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("context_version", sa.Integer(), nullable=True),
        sa.Column(
            "knowledge_search_scope",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "knowledge_filter_refs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "fact_scope_refs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("budget", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "purpose IN ('planning_run', 'execution')",
            name="ck_context_assembly_requests_purpose",
        ),
        sa.CheckConstraint(
            "consumer_type IN ('planner', 'worker')",
            name="ck_context_assembly_requests_consumer_type",
        ),
        sa.CheckConstraint(
            "(purpose = 'planning_run' AND consumer_type = 'planner' AND "
            "worker_item_id IS NULL AND context_version IS NULL) OR "
            "(purpose = 'execution' AND consumer_type = 'worker' AND "
            "worker_item_id IS NOT NULL AND context_version >= 1)",
            name="ck_context_assembly_requests_consumer_fields",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'assembling', 'ready', 'blocked', 'retry_wait', 'failed')",
            name="ck_context_assembly_requests_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(knowledge_search_scope) = 'object'",
            name="ck_context_assembly_requests_search_scope_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(knowledge_filter_refs) = 'array'",
            name="ck_context_assembly_requests_filter_refs_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(fact_scope_refs) = 'array'",
            name="ck_context_assembly_requests_fact_scope_refs_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(budget) = 'object'",
            name="ck_context_assembly_requests_budget_object",
        ),
        sa.CheckConstraint(
            "attempt >= 1", name="ck_context_assembly_requests_attempt"
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_context_assembly_requests_fingerprint",
        ),
        sa.CheckConstraint("version >= 1", name="ck_context_assembly_requests_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_context_assembly_requests_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_assembly_requests_worker_item",
        ),
        sa.ForeignKeyConstraint(
            ["work_context_revision_id", "workspace_id"],
            ["work_context_revisions.id", "work_context_revisions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_assembly_requests_context_revision",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_context_assembly_requests_identity"
        ),
        sa.UniqueConstraint(
            "purpose",
            "consumer_id",
            name="uq_context_assembly_requests_purpose_consumer",
        ),
        sa.Index(
            "ix_context_assembly_requests_claimable",
            "status",
            "next_attempt_at",
        ),
    )

    op.create_table(
        "context_packages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "assembly_request_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("consumer_type", sa.String(length=32), nullable=False),
        sa.Column("consumer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "work_context_revision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("context_version", sa.Integer(), nullable=True),
        sa.Column("control_plane_fact_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "artifact_refs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("budget", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.String(length=100), nullable=True),
        sa.Column("revocation_scope", sa.String(length=32), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "purpose IN ('planning_run', 'execution')",
            name="ck_context_packages_purpose",
        ),
        sa.CheckConstraint(
            "consumer_type IN ('planner', 'worker')",
            name="ck_context_packages_consumer_type",
        ),
        sa.CheckConstraint(
            "(purpose = 'planning_run' AND consumer_type = 'planner' AND "
            "worker_item_id IS NULL AND context_version IS NULL) OR "
            "(purpose = 'execution' AND consumer_type = 'worker' AND "
            "worker_item_id IS NOT NULL AND context_version >= 1)",
            name="ck_context_packages_consumer_fields",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(control_plane_fact_snapshot) = 'object'",
            name="ck_context_packages_fact_snapshot_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(artifact_refs) = 'array' AND "
            "jsonb_typeof(evidence_refs) = 'array'",
            name="ck_context_packages_refs_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(budget) = 'object'",
            name="ck_context_packages_budget_object",
        ),
        sa.CheckConstraint(
            "package_hash ~ '^[0-9a-f]{64}$'",
            name="ck_context_packages_manifest_hash",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'corrupt')",
            name="ck_context_packages_status",
        ),
        sa.CheckConstraint(
            "revocation_scope IS NULL OR revocation_scope IN ('new_starts', 'all_materialization')",
            name="ck_context_packages_revocation_scope",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_context_packages_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["assembly_request_id", "workspace_id"],
            [
                "context_assembly_requests.id",
                "context_assembly_requests.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_context_packages_assembly_request",
        ),
        sa.ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_packages_worker_item",
        ),
        sa.ForeignKeyConstraint(
            ["work_context_revision_id", "workspace_id"],
            ["work_context_revisions.id", "work_context_revisions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_packages_context_revision",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_context_packages_identity"),
        sa.UniqueConstraint(
            "assembly_request_id", name="uq_context_packages_assembly_request"
        ),
        sa.UniqueConstraint(
            "purpose", "consumer_id", name="uq_context_packages_purpose_consumer"
        ),
        sa.Index(
            "ix_context_packages_workspace_status_expiry",
            "workspace_id",
            "status",
            "expires_at",
        ),
    )

    op.create_table(
        "context_package_knowledge_refs",
        sa.Column("package_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_hash", sa.String(length=64), nullable=False),
        sa.Column("section_key", sa.String(length=500), nullable=False),
        sa.Column("section_hash", sa.String(length=64), nullable=False),
        sa.Column("source_ids", postgresql.JSONB(), nullable=False),
        sa.Column("selection_reason", sa.String(length=32), nullable=False),
        sa.Column("trust_level", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0", name="ck_context_package_knowledge_refs_ordinal"
        ),
        sa.CheckConstraint(
            "revision_hash ~ '^[0-9a-f]{64}$' AND section_hash ~ '^[0-9a-f]{64}$'",
            name="ck_context_package_knowledge_refs_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_ids) = 'array'",
            name="ck_context_package_knowledge_refs_source_ids_array",
        ),
        sa.CheckConstraint(
            "selection_reason IN ('explicit', 'work_context', 'retrieval')",
            name="ck_context_package_knowledge_refs_selection_reason",
        ),
        sa.CheckConstraint(
            "trust_level IN ('workspace_confirmed', 'external_reference')",
            name="ck_context_package_knowledge_refs_trust_level",
        ),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["context_packages.id"],
            ondelete="RESTRICT",
            name="fk_context_package_knowledge_refs_package",
        ),
        sa.PrimaryKeyConstraint("package_id", "ordinal"),
    )

    op.create_table(
        "context_package_acknowledgements",
        sa.Column("package_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consumer_type", sa.String(length=32), nullable=False),
        sa.Column("consumer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("context_version", sa.Integer(), nullable=True),
        sa.Column("acknowledged_package_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "adapter_module_instance_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("adapter_config_version", sa.Integer(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "consumer_type IN ('planner', 'worker')",
            name="ck_context_package_acknowledgements_consumer_type",
        ),
        sa.CheckConstraint(
            "(consumer_type = 'planner' AND context_version IS NULL) OR "
            "(consumer_type = 'worker' AND context_version >= 1)",
            name="ck_context_package_acknowledgements_context_version",
        ),
        sa.CheckConstraint(
            "acknowledged_package_hash ~ '^[0-9a-f]{64}$'",
            name="ck_context_package_acknowledgements_package_hash",
        ),
        sa.CheckConstraint(
            "adapter_config_version >= 1",
            name="ck_context_package_acknowledgements_adapter_version",
        ),
        sa.ForeignKeyConstraint(
            ["package_id", "workspace_id"],
            ["context_packages.id", "context_packages.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_package_acknowledgements_package",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_package_acknowledgements_adapter_module",
        ),
        sa.PrimaryKeyConstraint("package_id", "consumer_type", "consumer_id"),
    )

    op.create_table(
        "context_package_materialization_audits",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("package_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consumer_type", sa.String(length=32), nullable=False),
        sa.Column("consumer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("context_version", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("section_count", sa.Integer(), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("input_token_count", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "consumer_type IN ('planner', 'worker')",
            name="ck_context_package_materialization_audits_consumer_type",
        ),
        sa.CheckConstraint(
            "(consumer_type = 'planner' AND context_version IS NULL) OR "
            "(consumer_type = 'worker' AND context_version >= 1)",
            name="ck_context_package_materialization_audits_context_version",
        ),
        sa.CheckConstraint(
            "attempt >= 1",
            name="ck_context_package_materialization_audits_attempt",
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'failed')",
            name="ck_context_package_materialization_audits_outcome",
        ),
        sa.CheckConstraint(
            "section_count >= 0 AND byte_count >= 0 AND input_token_count >= 0",
            name="ck_context_package_materialization_audits_counts",
        ),
        sa.ForeignKeyConstraint(
            ["package_id", "workspace_id"],
            ["context_packages.id", "context_packages.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_package_materialization_audits_package",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "package_id",
            "consumer_type",
            "consumer_id",
            "attempt",
            name="uq_context_package_materialization_audits_attempt",
        ),
        sa.Index(
            "ix_context_package_materialization_audits_package_occurred",
            "package_id",
            "occurred_at",
        ),
    )

    op.create_table(
        "knowledge_candidate_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("producer_type", sa.String(length=64), nullable=False),
        sa.Column("producer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("planning_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("worker_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("subtask_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "artifact_refs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("proposal_hash", sa.String(length=64), nullable=False),
        sa.Column("content_ref", sa.String(length=1000), nullable=False),
        sa.Column("content_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "knowledge_command_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "terminal_candidate_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('waiting_for_task', 'queued', 'submitting', 'retry_wait', "
            "'submitted', 'rejected', 'discarded')",
            name="ck_knowledge_candidate_submissions_status",
        ),
        sa.CheckConstraint(
            "proposal_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_candidate_submissions_proposal_hash",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(artifact_refs) = 'array'",
            name="ck_knowledge_candidate_submissions_artifacts_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array'",
            name="ck_knowledge_candidate_submissions_evidence_array",
        ),
        sa.CheckConstraint(
            "content_bytes >= 0",
            name="ck_knowledge_candidate_submissions_content_bytes",
        ),
        sa.CheckConstraint(
            "attempt >= 1",
            name="ck_knowledge_candidate_submissions_attempt",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_knowledge_candidate_submissions_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_knowledge_candidate_submissions_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["planning_run_id", "workspace_id"],
            ["planning_runs.id", "planning_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_knowledge_candidate_submissions_planning_run",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_knowledge_candidate_submissions_execution",
        ),
        sa.ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_knowledge_candidate_submissions_worker_item",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_knowledge_candidate_submissions_task",
        ),
        sa.ForeignKeyConstraint(
            ["subtask_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_knowledge_candidate_submissions_subtask_task",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_command_id"],
            ["commands.id"],
            ondelete="RESTRICT",
            name="fk_knowledge_candidate_submissions_command",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "producer_type",
            "producer_id",
            "proposal_hash",
            name="uq_knowledge_candidate_submissions_producer_hash",
        ),
        sa.Index(
            "ix_knowledge_candidate_submissions_claimable",
            "status",
            "next_attempt_at",
        ),
        sa.Index(
            "ix_knowledge_candidate_submissions_workspace_producer",
            "workspace_id",
            "producer_type",
            "producer_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("knowledge_candidate_submissions")
    op.drop_table("context_package_materialization_audits")
    op.drop_table("context_package_acknowledgements")
    op.drop_table("context_package_knowledge_refs")
    op.drop_table("context_packages")
    op.drop_table("context_assembly_requests")
    op.drop_constraint(
        "fk_work_contexts_current_revision",
        "work_contexts",
        type_="foreignkey",
    )
    op.drop_table("work_context_revisions")
    op.drop_table("work_contexts")
