"""Persist the R6.3.3 notification gateway ledger."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_r6_3_3_notification_gateway"
down_revision: str | None = "0024_r6_3_2_human_release_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid() -> sa.types.TypeEngine:
    return sa.dialects.postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "notification_facts",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("recipient_type", sa.String(64), nullable=False),
        sa.Column("recipient_id", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(100), nullable=False),
        sa.Column("priority", sa.String(32), nullable=False),
        sa.Column("transition", sa.String(32), nullable=False),
        sa.Column("topic", sa.String(255), nullable=False),
        sa.Column("root_task_id", _uuid(), nullable=False),
        sa.Column("subtask_id", _uuid(), nullable=True),
        sa.Column("worker_item_id", _uuid(), nullable=True),
        sa.Column("source_projection_cursor", sa.BigInteger(), nullable=False),
        sa.Column("source_projection_type", sa.String(100), nullable=False),
        sa.Column("source_resource_id", _uuid(), nullable=False),
        sa.Column("source_resource_version", sa.Integer(), nullable=False),
        sa.Column("semantic_fingerprint", sa.String(64), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("summary", sa.String(16_384), nullable=False),
        sa.Column("action", sa.String(255), nullable=False),
        sa.Column("deep_link", sa.String(2_048), nullable=False),
        sa.Column("correlation_id", sa.String(255), nullable=False),
        sa.Column("baseline_import", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "policy_version >= 1 AND source_projection_cursor >= 0 AND "
            "source_resource_version >= 1",
            name="ck_notification_facts_versions",
        ),
        sa.CheckConstraint(
            "priority IN ('action', 'progress', 'operations')",
            name="ck_notification_facts_priority",
        ),
        sa.CheckConstraint(
            "transition IN ('opened', 'updated', 'recovered', 'completed', 'degraded')",
            name="ck_notification_facts_transition",
        ),
        sa.CheckConstraint(
            "semantic_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_notification_facts_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_notification_facts_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_notification_facts_root_task_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["subtask_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_notification_facts_subtask_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_notification_facts_worker_item_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_notification_facts_identity"),
        sa.UniqueConstraint(
            "workspace_id",
            "recipient_type",
            "recipient_id",
            "semantic_fingerprint",
            name="uq_notification_facts_semantic",
        ),
        sa.Index(
            "ix_notification_facts_history",
            "workspace_id",
            "recipient_type",
            "recipient_id",
            "created_at",
        ),
    )

    op.create_table(
        "notification_threads",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("recipient_type", sa.String(64), nullable=False),
        sa.Column("recipient_id", sa.String(255), nullable=False),
        sa.Column("thread_key", sa.String(1_024), nullable=False),
        sa.Column("topic", sa.String(255), nullable=False),
        sa.Column("root_task_id", _uuid(), nullable=False),
        sa.Column("subtask_id", _uuid(), nullable=True),
        sa.Column("worker_item_id", _uuid(), nullable=True),
        sa.Column("latest_fact_id", _uuid(), nullable=False),
        sa.Column("business_status", sa.String(32), nullable=False),
        sa.Column("organization_status", sa.String(32), nullable=False),
        sa.Column("last_read_fact_id", _uuid(), nullable=True),
        sa.Column("unread_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "business_status IN ('open', 'resolved')",
            name="ck_notification_threads_business_status",
        ),
        sa.CheckConstraint(
            "organization_status IN ('active', 'archived')",
            name="ck_notification_threads_organization_status",
        ),
        sa.CheckConstraint(
            "unread_count >= 0",
            name="ck_notification_threads_unread_count",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_notification_threads_versions",
        ),
        sa.CheckConstraint(
            "((business_status = 'open' AND resolved_at IS NULL) OR "
            "(business_status = 'resolved' AND resolved_at IS NOT NULL)) AND "
            "((organization_status = 'active' AND archived_at IS NULL) OR "
            "(organization_status = 'archived' AND archived_at IS NOT NULL))",
            name="ck_notification_threads_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_notification_threads_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_notification_threads_root_task_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["subtask_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_notification_threads_subtask_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_notification_threads_worker_item_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["latest_fact_id", "workspace_id"],
            ["notification_facts.id", "notification_facts.workspace_id"],
            ondelete="RESTRICT",
            name="fk_notification_threads_latest_fact_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["last_read_fact_id", "workspace_id"],
            ["notification_facts.id", "notification_facts.workspace_id"],
            ondelete="RESTRICT",
            name="fk_notification_threads_last_read_fact_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_notification_threads_identity"),
        sa.UniqueConstraint(
            "workspace_id",
            "recipient_type",
            "recipient_id",
            "thread_key",
            name="uq_notification_threads_key",
        ),
        sa.Index(
            "ix_notification_threads_inbox",
            "workspace_id",
            "recipient_type",
            "recipient_id",
            "organization_status",
            "business_status",
            "unread_count",
            "updated_at",
        ),
    )

    op.create_table(
        "notification_policy_states",
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("recipient_type", sa.String(64), nullable=False),
        sa.Column("recipient_id", sa.String(255), nullable=False),
        sa.Column("subject_type", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column("topic", sa.String(255), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("observed_status", sa.String(64), nullable=False),
        sa.Column("observed_fingerprint", sa.String(64), nullable=True),
        sa.Column(
            "observed_payload",
            sa.dialects.postgresql.JSONB,
            nullable=False,
        ),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notified_fingerprint", sa.String(64), nullable=True),
        sa.Column("active_thread_id", _uuid(), nullable=True),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "policy_version >= 1 AND resource_version >= 1",
            name="ck_notification_policy_states_versions",
        ),
        sa.CheckConstraint(
            "not_before IS NULL OR "
            "(first_observed_at IS NOT NULL AND not_before >= first_observed_at)",
            name="ck_notification_policy_states_pending_window",
        ),
        sa.CheckConstraint(
            "observed_fingerprint IS NULL OR "
            "observed_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_notification_policy_states_observed_fingerprint",
        ),
        sa.CheckConstraint(
            "last_notified_fingerprint IS NULL OR "
            "last_notified_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_notification_policy_states_notified_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_notification_policy_states_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["active_thread_id", "workspace_id"],
            ["notification_threads.id", "notification_threads.workspace_id"],
            ondelete="RESTRICT",
            name="fk_notification_policy_states_thread_workspace",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "recipient_type",
            "recipient_id",
            "subject_type",
            "subject_id",
            "topic",
            name="pk_notification_policy_states_scope",
        ),
        sa.Index(
            "ix_notification_policy_states_due",
            "workspace_id",
            "not_before",
            postgresql_where=sa.text("not_before IS NOT NULL"),
        ),
    )

    op.create_table(
        "notification_routes",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("recipient_type", sa.String(64), nullable=False),
        sa.Column("recipient_id", sa.String(255), nullable=False),
        sa.Column("channel_type", sa.String(100), nullable=False),
        sa.Column("channel_module_instance_id", _uuid(), nullable=False),
        sa.Column("source_ref", sa.String(512), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'draining', 'disabled', 'revoked')",
            name="ck_notification_routes_status",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_notification_routes_version",
        ),
        sa.CheckConstraint(
            "length(source_ref) >= 32 AND "
            "source_ref ~ '^[A-Za-z0-9][A-Za-z0-9._~:/+\\-]*$'",
            name="ck_notification_routes_source_ref",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_notification_routes_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["channel_module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_notification_routes_module_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_notification_routes_identity"),
        sa.UniqueConstraint(
            "workspace_id",
            "channel_module_instance_id",
            "source_ref",
            name="uq_notification_routes_source",
        ),
        sa.Index(
            "ix_notification_routes_active",
            "workspace_id",
            "status",
            "channel_type",
        ),
    )

    op.create_table(
        "notification_source_bindings",
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", _uuid(), nullable=False),
        sa.Column("notification_route_id", _uuid(), nullable=False),
        sa.Column("binding_kind", sa.String(32), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "resource_type IN ('planning_request', 'root_task')",
            name="ck_notification_source_bindings_resource",
        ),
        sa.CheckConstraint(
            "binding_kind = 'origin'",
            name="ck_notification_source_bindings_kind",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_notification_source_bindings_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["notification_route_id", "workspace_id"],
            ["notification_routes.id", "notification_routes.workspace_id"],
            ondelete="RESTRICT",
            name="fk_notification_source_bindings_route_workspace",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "resource_type",
            "resource_id",
            "binding_kind",
            name="pk_notification_source_bindings_identity",
        ),
        sa.Index(
            "ix_notification_source_bindings_lookup",
            "workspace_id",
            "resource_type",
            "resource_id",
        ),
    )

    op.create_table(
        "notification_delivery_intents",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("notification_fact_id", _uuid(), nullable=False),
        sa.Column("notification_route_id", _uuid(), nullable=False),
        sa.Column("channel_module_instance_id", _uuid(), nullable=False),
        sa.Column("delivery_round", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", _uuid(), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("provider_message_ref", sa.String(512), nullable=True),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exhausted_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('pending', 'claimed', 'retry_wait', 'delivered', 'exhausted', 'suppressed')",
            name="ck_notification_delivery_intents_status",
        ),
        sa.CheckConstraint(
            "delivery_round >= 1 AND contract_version >= 1 AND attempts >= 0 AND "
            "fencing_token >= 1",
            name="ck_notification_delivery_intents_attempts",
        ),
        sa.CheckConstraint(
            "(status = 'claimed' AND claimed_until IS NOT NULL AND claim_token IS NOT NULL) OR "
            "(status <> 'claimed' AND claimed_until IS NULL)",
            name="ck_notification_delivery_intents_claim",
        ),
        sa.CheckConstraint(
            "(status = 'delivered' AND delivered_at IS NOT NULL) OR "
            "(status <> 'delivered' AND delivered_at IS NULL)",
            name="ck_notification_delivery_intents_delivered_timestamp",
        ),
        sa.CheckConstraint(
            "(status = 'exhausted' AND exhausted_at IS NOT NULL) OR "
            "(status <> 'exhausted' AND exhausted_at IS NULL)",
            name="ck_notification_delivery_intents_terminal_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_notification_delivery_intents_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["notification_fact_id", "workspace_id"],
            ["notification_facts.id", "notification_facts.workspace_id"],
            ondelete="RESTRICT",
            name="fk_notification_delivery_intents_fact_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["notification_route_id", "workspace_id"],
            ["notification_routes.id", "notification_routes.workspace_id"],
            ondelete="RESTRICT",
            name="fk_notification_delivery_intents_route_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["channel_module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_notification_delivery_intents_module_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_notification_delivery_intents_identity"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "notification_fact_id",
            "notification_route_id",
            "delivery_round",
            name="uq_notification_delivery_intents_round",
        ),
        sa.Index(
            "ix_notification_delivery_intents_due",
            "workspace_id",
            "channel_module_instance_id",
            "status",
            "next_attempt_at",
            postgresql_where=sa.text("status IN ('pending', 'retry_wait')"),
        ),
        sa.Index(
            "ix_notification_delivery_intents_claimed",
            "workspace_id",
            "channel_module_instance_id",
            "claimed_until",
            postgresql_where=sa.text("status = 'claimed'"),
        ),
    )

    op.create_table(
        "notification_delivery_attempts",
        sa.Column("delivery_intent_id", _uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("claim_token", _uuid(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("provider_message_ref", sa.String(512), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(outcome IS NULL AND error_code IS NULL AND finished_at IS NULL) OR "
            "(outcome = 'delivered' AND error_code IS NULL AND finished_at IS NOT NULL) OR "
            "(outcome <> 'delivered' AND error_code IS NOT NULL AND finished_at IS NOT NULL)",
            name="ck_notification_delivery_attempts_settlement",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('delivered', 'retryable_failure', 'terminal_failure', 'unknown_result')",
            name="ck_notification_delivery_attempts_error",
        ),
        sa.CheckConstraint(
            "attempt_number >= 1 AND fencing_token >= 1",
            name="ck_notification_delivery_attempts_versions",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_intent_id"],
            ["notification_delivery_intents.id"],
            ondelete="RESTRICT",
            name="fk_notification_delivery_attempts_intent",
        ),
        sa.PrimaryKeyConstraint(
            "delivery_intent_id",
            "attempt_number",
            name="pk_notification_delivery_attempts_identity",
        ),
        sa.Index(
            "ix_notification_delivery_attempts_claims",
            "delivery_intent_id",
            "fencing_token",
            "attempt_number",
        ),
    )

    op.create_table(
        "notification_reconciliation_cursors",
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("last_projection_cursor", sa.BigInteger(), nullable=False),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(255), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "policy_version >= 1 AND last_projection_cursor >= 0 AND version >= 1",
            name="ck_notification_reconciliation_cursors_versions",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_until IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_notification_reconciliation_cursors_lease",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_notification_reconciliation_cursors_workspace",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "policy_version",
            name="pk_notification_reconciliation_cursors_identity",
        ),
        sa.Index(
            "ix_notification_reconciliation_cursors_processing",
            "workspace_id",
            "policy_version",
            "last_projection_cursor",
        ),
    )


def downgrade() -> None:
    raise RuntimeError(
        "R6.3.3 notification gateway migration is forward-only; "
        "notification evidence must not be dropped"
    )
