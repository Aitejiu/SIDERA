"""Persist R6.3.2 human release governance ledger."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_r6_3_2_human_release_governance"
down_revision: str | None = "0023_r6_3_1_schema_alignment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid() -> sa.types.TypeEngine:
    return sa.dialects.postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.drop_constraint("ck_runtime_intent_applications_status", "runtime_intent_applications", type_="check")
    op.create_check_constraint(
        "ck_runtime_intent_applications_status",
        "runtime_intent_applications",
        "status IN ('pending', 'applied', 'rejected', 'stale', 'duplicate', 'deferred')",
    )
    op.create_table(
        "collaboration_governance_holds",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("root_task_id", _uuid(), nullable=False),
        sa.Column("run_id", _uuid(), nullable=False),
        sa.Column("plan_snapshot_id", _uuid(), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("plan_content_hash", sa.String(64), nullable=False),
        sa.Column("worker_item_id", _uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_by_actor_id", sa.String(255), nullable=False),
        sa.Column("created_via", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(2000)),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('active', 'resumed', 'superseded')", name="ck_collaboration_governance_holds_status"),
        sa.CheckConstraint("plan_generation >= 1 AND resource_version >= 1", name="ck_collaboration_governance_holds_versions"),
        sa.CheckConstraint("plan_content_hash ~ '^[0-9a-f]{64}$'", name="ck_collaboration_governance_holds_plan_hash"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_collaboration_governance_holds_workspace"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_collaboration_governance_holds_identity"),
        sa.Index("uq_collaboration_governance_holds_active", "workspace_id", "run_id", "worker_item_id", unique=True, postgresql_where=sa.text("status = 'active'")),
    )
    op.create_table(
        "collaboration_release_proposals",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("hold_id", _uuid(), nullable=False),
        sa.Column("run_id", _uuid(), nullable=False),
        sa.Column("projection_snapshot_id", _uuid(), nullable=False),
        sa.Column("projection_hash", sa.String(64), nullable=False),
        sa.Column("decision_id", _uuid(), nullable=False),
        sa.Column("intent_id", _uuid(), nullable=False),
        sa.Column("execution_request_id", _uuid(), nullable=False),
        sa.Column("target_request_version", sa.Integer(), nullable=False),
        sa.Column("input_fingerprint_version", sa.Integer(), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(255), nullable=False),
        sa.Column("summary", sa.String(16384), nullable=False),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'stale')", name="ck_collaboration_release_proposals_status"),
        sa.CheckConstraint("target_request_version >= 1 AND input_fingerprint_version >= 1 AND resource_version >= 1", name="ck_collaboration_release_proposals_versions"),
        sa.CheckConstraint("projection_hash ~ '^[0-9a-f]{64}$' AND input_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_collaboration_release_proposals_hashes"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_collaboration_release_proposals_identity"),
        sa.Index("uq_collaboration_release_proposals_pending_request", "workspace_id", "hold_id", "execution_request_id", unique=True, postgresql_where=sa.text("status = 'pending'")),
        sa.Index("uq_collaboration_release_proposals_fingerprint", "workspace_id", "hold_id", "execution_request_id", "target_request_version", "input_fingerprint_version", "input_fingerprint", unique=True),
    )
    op.create_table(
        "collaboration_governance_resolutions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("proposal_id", _uuid(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("transport", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(2000)),
        sa.Column("command_id", _uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("action IN ('approve', 'reject')", name="ck_collaboration_governance_resolutions_action"),
        sa.CheckConstraint("result IN ('applied', 'stale')", name="ck_collaboration_governance_resolutions_result"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_collaboration_governance_resolutions_identity"),
        sa.UniqueConstraint("workspace_id", "proposal_id", name="uq_collaboration_governance_resolutions_proposal"),
        sa.UniqueConstraint("workspace_id", "command_id", name="uq_collaboration_governance_resolutions_command"),
    )


def downgrade() -> None:
    op.drop_table("collaboration_governance_resolutions")
    op.drop_table("collaboration_release_proposals")
    op.drop_table("collaboration_governance_holds")
    op.drop_constraint("ck_runtime_intent_applications_status", "runtime_intent_applications", type_="check")
    op.create_check_constraint(
        "ck_runtime_intent_applications_status",
        "runtime_intent_applications",
        "status IN ('pending', 'applied', 'rejected', 'stale', 'duplicate')",
    )
