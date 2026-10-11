"""Preserve proven BUSY pins and exact native start request identities."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0035_native_admission_intentions"
down_revision = "0034_agent_bindings_and_run_links"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_execution_links", sa.Column("request_sha256", sa.String(64)))
    op.add_column("agent_execution_links", sa.Column("start_receipt", JSONB))
    op.add_column("agent_execution_links", sa.Column("preparation", JSONB))
    op.create_check_constraint(
        "ck_agent_execution_links_request_sha",
        "agent_execution_links",
        "request_sha256 IS NULL OR request_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_agent_execution_links_busy",
        "agent_execution_links",
        "status <> 'waiting_capacity' OR (run_id IS NULL AND event_cursor = 0 "
        "AND request_sha256 IS NOT NULL AND start_receipt IS NOT NULL "
        "AND start_receipt->>'status' = 'busy' "
        "AND start_receipt->>'operation_id' = start_operation_id::text "
        "AND start_receipt->>'run_id' IS NULL)",
    )
    op.create_table(
        "agent_admission_pin_history",
        sa.Column("workspace_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("start_operation_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("execution_id", UUID(as_uuid=True), nullable=False),
        sa.Column("authority_id", UUID(as_uuid=True), nullable=False),
        sa.Column("binding_version", sa.Integer, nullable=False),
        sa.Column("profile_ref", JSONB, nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("start_receipt", JSONB, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True)),
        sa.Column("event_cursor", sa.BigInteger, nullable=False),
        sa.Column(
            "lease_id",
            UUID(as_uuid=True),
            sa.ForeignKey("assignment_leases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("fencing_token", sa.BigInteger, nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cause_command_id", UUID(as_uuid=True), nullable=False),
        sa.Column("preparation", JSONB),
        sa.Column("execution_binding_snapshot", JSONB),
        sa.Column("workspace_snapshot", JSONB),
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
        sa.CheckConstraint(
            "status = 'waiting_capacity' AND run_id IS NULL AND event_cursor = 0 "
            "AND binding_version >= 1 AND fencing_token >= 1 "
            "AND request_sha256 ~ '^[0-9a-f]{64}$' "
            "AND start_receipt->>'status' = 'busy' "
            "AND start_receipt->>'operation_id' = start_operation_id::text "
            "AND start_receipt->>'run_id' IS NULL",
            name="ck_agent_admission_history_busy",
        ),
    )
    op.execute("""
        CREATE FUNCTION sidera_reject_admission_pin_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'native admission pin history is append-only';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER trg_agent_admission_pin_append_only
        BEFORE UPDATE OR DELETE ON agent_admission_pin_history
        FOR EACH ROW EXECUTE FUNCTION sidera_reject_admission_pin_mutation()
    """)


def downgrade():
    # Intentionally fail rather than discard immutable admission evidence.
    raise RuntimeError("native admission evidence cannot be downgraded")
