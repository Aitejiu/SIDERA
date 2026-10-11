"""Add CP-owned assignment history, native links, authority and planner pins."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0034_agent_bindings_and_run_links"
down_revision = "0033_execution_purpose_text"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_bindings",
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("target_type", sa.String(32), primary_key=True),
        sa.Column("target_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("is_current", sa.Boolean, nullable=False),
        sa.Column("assignee_kind", sa.String(32), nullable=False),
        sa.Column("assignee_id", sa.String(255), nullable=False),
        sa.Column("profile_ref", JSONB),
        sa.Column("runnable", sa.Boolean, nullable=False),
        sa.Column("not_runnable_reasons", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_type IN ('subtask', 'worker_item')", name="ck_agent_bindings_target"
        ),
        sa.CheckConstraint("version >= 1", name="ck_agent_bindings_version"),
        sa.CheckConstraint(
            "(assignee_kind = 'human' AND profile_ref IS NULL AND NOT runnable) OR "
            "(assignee_kind = 'profile' AND profile_ref IS NOT NULL AND "
            "profile_ref->>'workspace_id' = workspace_id::text AND "
            "profile_ref->>'profile_id' = assignee_id)",
            name="ck_agent_bindings_pin",
        ),
    )
    op.create_index(
        "uq_agent_bindings_current",
        "agent_bindings",
        ["workspace_id", "target_type", "target_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "execution_authorities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", UUID(as_uuid=True), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", UUID(as_uuid=True), nullable=False),
        sa.Column("binding_version", sa.Integer, nullable=False),
        sa.Column("policy", JSONB, nullable=False),
        sa.Column("revoked", sa.Boolean, nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "target_type", "target_id", "binding_version"],
            [
                "agent_bindings.workspace_id",
                "agent_bindings.target_type",
                "agent_bindings.target_id",
                "agent_bindings.version",
            ],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "execution_id", "workspace_id"),
    )
    op.create_table(
        "agent_execution_links",
        sa.Column("workspace_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("execution_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("start_operation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("profile_ref", JSONB, nullable=False),
        sa.Column("run_id", UUID(as_uuid=True)),
        sa.Column("event_cursor", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("authority_id", UUID(as_uuid=True), nullable=False),
        sa.Column("binding_version", sa.Integer, nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["authority_id", "execution_id", "workspace_id"],
            [
                "execution_authorities.id",
                "execution_authorities.execution_id",
                "execution_authorities.workspace_id",
            ],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("workspace_id", "start_operation_id"),
        sa.UniqueConstraint("workspace_id", "run_id"),
        sa.CheckConstraint(
            "binding_version >= 1 AND event_cursor >= 0",
            name="ck_agent_execution_links_versions",
        ),
    )
    op.add_column(
        "planning_profiles",
        sa.Column(
            "execution_mode", sa.String(32), nullable=False, server_default="legacy"
        ),
    )
    op.add_column("planning_profiles", sa.Column("planner_agent_ref", JSONB))
    op.alter_column(
        "planning_profiles",
        "planner_module_instance_id",
        existing_type=UUID(as_uuid=True),
        nullable=True,
    )
    op.alter_column(
        "planning_profiles",
        "planner_module_config_version",
        existing_type=sa.Integer,
        nullable=True,
    )
    op.create_check_constraint(
        "ck_planning_profiles_execution_mode",
        "planning_profiles",
        "(execution_mode = 'legacy' AND planner_module_instance_id IS NOT NULL AND "
        "planner_module_config_version IS NOT NULL AND planner_agent_ref IS NULL) OR "
        "(execution_mode = 'native_profile' AND planner_module_instance_id IS NULL AND "
        "planner_module_config_version IS NULL AND planner_agent_ref IS NOT NULL AND "
        "planner_agent_ref->>'workspace_id' = workspace_id::text)",
    )


def downgrade():
    # Never discard native policy evidence to satisfy the old NOT NULL contract.
    op.alter_column(
        "planning_profiles",
        "planner_module_instance_id",
        existing_type=UUID(as_uuid=True),
        nullable=False,
    )
    op.alter_column(
        "planning_profiles",
        "planner_module_config_version",
        existing_type=sa.Integer,
        nullable=False,
    )
    op.drop_constraint(
        "ck_planning_profiles_execution_mode", "planning_profiles", type_="check"
    )
    op.drop_column("planning_profiles", "planner_agent_ref")
    op.drop_column("planning_profiles", "execution_mode")
    op.drop_table("agent_execution_links")
    op.drop_table("execution_authorities")
    op.drop_table("agent_bindings")
