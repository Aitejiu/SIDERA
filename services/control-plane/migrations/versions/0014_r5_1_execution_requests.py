"""Persist proposal materialization and static activation request facts.

Revision ID: 0014_r5_1_execution_requests
Revises: 0013_r5_1_planning_core
Create Date: 2026-08-23
"""

from collections.abc import Sequence
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_r5_1_execution_requests"
down_revision: str | None = "0013_r5_1_planning_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_plan_snapshots_id_workspace",
        "task_plan_snapshots",
        ["id", "workspace_id"],
    )
    _add_proposal_revision_identity()

    op.create_table(
        "proposal_materializations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "planning_request_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "proposal_revision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("root_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("subtask_task_mapping", postgresql.JSONB(), nullable=False),
        sa.Column("worker_item_mapping", postgresql.JSONB(), nullable=False),
        sa.Column("execution_request_mapping", postgresql.JSONB(), nullable=False),
        sa.Column("deep_link", sa.String(length=500), nullable=False),
        sa.Column("confirmed_by", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "plan_generation >= 1",
            name="ck_proposal_materializations_generation",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(subtask_task_mapping) = 'object'",
            name="ck_proposal_materializations_subtasks_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(worker_item_mapping) = 'object'",
            name="ck_proposal_materializations_workers_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(execution_request_mapping) = 'object'",
            name="ck_proposal_materializations_requests_object",
        ),
        sa.ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_proposal_materializations_request_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_revision_id", "workspace_id"],
            [
                "planning_proposal_revisions.id",
                "planning_proposal_revisions.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_proposal_materializations_proposal_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_proposal_materializations_root_task_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_proposal_materializations_snapshot_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "planning_request_id", name="uq_proposal_materializations_request"
        ),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_proposal_materializations_identity"
        ),
        sa.Index(
            "ix_proposal_materializations_workspace_created",
            "workspace_id",
            "created_at",
        ),
    )

    op.create_table(
        "execution_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "planning_request_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "proposal_revision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("plan_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("root_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subtask_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_item_key", sa.String(length=500), nullable=False),
        sa.Column("intent_key", sa.String(length=500), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("capability_requirements", postgresql.JSONB(), nullable=False),
        sa.Column("adapter_preferences", postgresql.JSONB(), nullable=False),
        sa.Column("budget_class", sa.String(length=100), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("expected_outputs", postgresql.JSONB(), nullable=False),
        sa.Column("expected_artifact_refs", postgresql.JSONB(), nullable=False),
        sa.Column("expected_evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("gate_reason", sa.String(length=255), nullable=False),
        sa.Column("gate_details", postgresql.JSONB(), nullable=False),
        sa.Column("gate_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "execution_lineage_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("first_execution_id", postgresql.UUID(as_uuid=True), nullable=True),
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
            "plan_generation >= 1", name="ck_execution_requests_generation"
        ),
        sa.CheckConstraint(
            "max_attempts >= 1", name="ck_execution_requests_attempts"
        ),
        sa.CheckConstraint("version >= 1", name="ck_execution_requests_version"),
        sa.CheckConstraint(
            "status IN ('blocked', 'ready', 'materialized', 'cancelled', 'obsolete')",
            name="ck_execution_requests_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(capability_requirements) = 'array'",
            name="ck_execution_requests_capabilities_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(adapter_preferences) = 'array'",
            name="ck_execution_requests_preferences_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(expected_outputs) = 'array'",
            name="ck_execution_requests_outputs_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(expected_artifact_refs) = 'array'",
            name="ck_execution_requests_artifacts_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(expected_evidence_refs) = 'array'",
            name="ck_execution_requests_evidence_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(gate_details) = 'object'",
            name="ck_execution_requests_gate_details_object",
        ),
        sa.CheckConstraint(
            "(status = 'materialized' AND execution_lineage_id IS NOT NULL "
            "AND first_execution_id IS NOT NULL) OR "
            "(status <> 'materialized' AND execution_lineage_id IS NULL "
            "AND first_execution_id IS NULL)",
            name="ck_execution_requests_materialized_lineage",
        ),
        sa.ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_request_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_revision_id", "workspace_id"],
            [
                "planning_proposal_revisions.id",
                "planning_proposal_revisions.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_execution_requests_proposal_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_snapshot_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_root_task_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["subtask_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_subtask_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_worker_item_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["execution_lineage_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_lineage_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["first_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_first_execution_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_execution_requests_identity"
        ),
        sa.UniqueConstraint(
            "proposal_revision_id",
            "worker_item_key",
            name="uq_execution_requests_proposal_worker_item",
        ),
        sa.Index(
            "uq_execution_requests_lineage",
            "workspace_id",
            "execution_lineage_id",
            unique=True,
            postgresql_where=sa.text("execution_lineage_id IS NOT NULL"),
        ),
        sa.Index(
            "ix_execution_requests_workspace_status",
            "workspace_id",
            "status",
            "not_before",
        ),
    )

    op.create_table(
        "execution_request_dependencies",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "downstream_request_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "upstream_request_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("enforcement", sa.String(length=32), nullable=False),
        sa.Column("required_artifact_refs", postgresql.JSONB(), nullable=False),
        sa.Column("required_evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "enforcement IN ('hard', 'informational')",
            name="ck_execution_request_dependencies_enforcement",
        ),
        sa.CheckConstraint(
            "downstream_request_id <> upstream_request_id",
            name="ck_execution_request_dependencies_not_self",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(required_artifact_refs) = 'array'",
            name="ck_execution_request_dependencies_artifacts_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(required_evidence_refs) = 'array'",
            name="ck_execution_request_dependencies_evidence_array",
        ),
        sa.ForeignKeyConstraint(
            ["downstream_request_id", "workspace_id"],
            ["execution_requests.id", "execution_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_request_dependencies_downstream_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["upstream_request_id", "workspace_id"],
            ["execution_requests.id", "execution_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_request_dependencies_upstream_workspace",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id", "downstream_request_id", "upstream_request_id"
        ),
        sa.Index(
            "ix_execution_request_dependencies_upstream",
            "workspace_id",
            "upstream_request_id",
        ),
    )


def _add_proposal_revision_identity() -> None:
    op.add_column(
        "planning_proposal_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT planning_request_id, revision "
            "FROM planning_proposal_revisions WHERE id IS NULL"
        )
    ).all()
    for planning_request_id, revision_number in rows:
        proposal_id = uuid5(
            NAMESPACE_URL,
            f"sidera:planning-proposal:{planning_request_id}:{revision_number}",
        )
        connection.execute(
            sa.text(
                "UPDATE planning_proposal_revisions SET id = :id "
                "WHERE planning_request_id = :request_id AND revision = :revision"
            ),
            {
                "id": proposal_id,
                "request_id": planning_request_id,
                "revision": revision_number,
            },
        )
    op.alter_column("planning_proposal_revisions", "id", nullable=False)
    op.drop_constraint(
        "planning_proposal_revisions_pkey",
        "planning_proposal_revisions",
        type_="primary",
    )
    op.create_primary_key(
        "planning_proposal_revisions_pkey",
        "planning_proposal_revisions",
        ["id"],
    )
    op.create_unique_constraint(
        "uq_planning_proposals_identity",
        "planning_proposal_revisions",
        ["id", "workspace_id"],
    )


def downgrade() -> None:
    op.drop_table("execution_request_dependencies")
    op.drop_table("execution_requests")
    op.drop_table("proposal_materializations")
    op.drop_constraint(
        "uq_planning_proposals_identity",
        "planning_proposal_revisions",
        type_="unique",
    )
    op.drop_constraint(
        "planning_proposal_revisions_pkey",
        "planning_proposal_revisions",
        type_="primary",
    )
    op.create_primary_key(
        "planning_proposal_revisions_pkey",
        "planning_proposal_revisions",
        ["planning_request_id", "revision"],
    )
    op.drop_column("planning_proposal_revisions", "id")
    op.drop_constraint(
        "uq_plan_snapshots_id_workspace",
        "task_plan_snapshots",
        type_="unique",
    )
