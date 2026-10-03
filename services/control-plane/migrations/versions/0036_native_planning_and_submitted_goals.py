"""Persist native planning pins and stable submitted root tasks."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0036_native_planning_and_submitted_goals"
down_revision = "0035_native_admission_intentions"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("planning_runs", "planning_proposal_revisions"):
        op.add_column(
            table,
            sa.Column(
                "execution_mode", sa.String(32), nullable=False, server_default="legacy"
            ),
        )
        op.add_column(table, sa.Column("planner_agent_ref", JSONB))
        op.alter_column(table, "planner_module_instance_id", nullable=True)
        op.alter_column(table, "planner_module_config_version", nullable=True)
        capability = ""
        native_capability = ""
        if table == "planning_runs":
            op.alter_column(table, "capability_snapshot", nullable=True)
            capability = " AND capability_snapshot IS NOT NULL"
            native_capability = " AND capability_snapshot IS NULL"
        prefix = "planning_runs" if table == "planning_runs" else "planning_proposals"
        op.create_check_constraint(
            f"ck_{prefix}_execution_mode",
            table,
            "(execution_mode = 'legacy' AND planner_module_instance_id IS NOT NULL "
            "AND planner_module_config_version IS NOT NULL AND planner_agent_ref IS NULL"
            + capability
            + ") OR (execution_mode = 'native_profile' "
            "AND planner_module_instance_id IS NULL AND planner_module_config_version IS NULL "
            "AND planner_agent_ref IS NOT NULL AND jsonb_typeof(planner_agent_ref) = 'object'"
            + native_capability
            + ")",
        )
    op.create_table(
        "task_goal_submissions",
        sa.Column("workspace_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("planning_request_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("root_task_id", UUID(as_uuid=True), nullable=False),
        sa.Column("root_task_version", sa.Integer, nullable=False),
        sa.Column("principal_id", sa.String(255), nullable=False),
        sa.Column("grant_id", UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workspace_id", "root_task_id", name="uq_task_goal_submissions_root"
        ),
        sa.CheckConstraint(
            "root_task_version >= 1", name="ck_task_goal_submissions_version"
        ),
    )
    op.create_table(
        "planning_authorities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("planning_request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("planning_run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("lease_id", UUID(as_uuid=True), nullable=False),
        sa.Column("policy", JSONB, nullable=False),
        sa.Column("revoked", sa.Boolean, nullable=False),
        sa.ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["planning_run_id", "workspace_id"],
            ["planning_runs.id", "planning_runs.workspace_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lease_id", "workspace_id"],
            ["planning_run_leases.id", "planning_run_leases.workspace_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "id",
            "workspace_id",
            "planning_run_id",
            name="uq_planning_authorities_identity",
        ),
        sa.UniqueConstraint(
            "workspace_id", "planning_run_id", name="uq_planning_authorities_run"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(policy) = 'object'", name="ck_planning_authorities_policy"
        ),
    )
    op.create_table(
        "planning_agent_links",
        sa.Column("workspace_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("planning_run_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("authority_id", UUID(as_uuid=True), nullable=False),
        sa.Column("start_operation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("start_request", JSONB, nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True)),
        sa.Column("start_receipt", JSONB),
        sa.Column("event_cursor", sa.BigInteger, nullable=False),
        sa.Column("response_id", UUID(as_uuid=True)),
        sa.Column("closed", sa.Boolean, nullable=False),
        sa.Column("reason_code", sa.String(100)),
        sa.ForeignKeyConstraint(
            ["authority_id", "workspace_id", "planning_run_id"],
            [
                "planning_authorities.id",
                "planning_authorities.workspace_id",
                "planning_authorities.planning_run_id",
            ],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "start_operation_id", name="uq_planning_agent_links_operation"
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$' AND event_cursor >= 0",
            name="ck_planning_agent_links_fingerprint",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'starting', 'waiting_capacity', 'running', 'unknown', 'cleanup_pending', 'closed', 'rejected')",
            name="ck_planning_agent_links_status",
        ),
        sa.CheckConstraint(
            "NOT closed OR (status = 'closed' AND run_id IS NOT NULL)",
            name="ck_planning_agent_links_closed",
        ),
        sa.CheckConstraint(
            "status <> 'closed' OR closed", name="ck_planning_agent_links_closed_status"
        ),
        sa.CheckConstraint(
            "status <> 'waiting_capacity' OR (run_id IS NULL AND event_cursor = 0 AND start_receipt IS NOT NULL AND start_receipt->>'status' = 'busy' AND start_receipt->>'operation_id' = start_operation_id::text AND start_receipt->>'run_id' IS NULL)",
            name="ck_planning_agent_links_busy",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(start_request) = 'object' AND start_request->>'operation_id' = start_operation_id::text AND start_request->>'grant_id' = authority_id::text",
            name="ck_planning_agent_links_request",
        ),
    )


def downgrade():
    raise RuntimeError("native planning and submitted task history cannot be discarded")
