from alembic import op
from sqlalchemy import Column, DateTime, Integer, String, Text, Uuid
from sqlalchemy import JSON

revision = "0004_inputs_events"
down_revision = "0003_agent_runs_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        Column("original_grant_id", Uuid(as_uuid=True), nullable=True),
        schema="agents",
    )
    op.create_table(
        "run_inputs",
        Column("workspace_id", Uuid(as_uuid=True), primary_key=True),
        Column("run_id", Uuid(as_uuid=True), primary_key=True),
        Column("operation_id", Uuid(as_uuid=True), primary_key=True),
        Column("fingerprint", String(71), nullable=False),
        Column("response_id", Uuid(as_uuid=True), nullable=False),
        Column("queue_sequence", Integer, nullable=False),
        Column("status", String(24), nullable=False),
        Column("reason_code", String(120), nullable=True),
        Column("evidence_sequence", Integer, nullable=False),
        Column("text", Text, nullable=True),
        Column("context", JSON, nullable=True),
        schema="agents",
    )
    op.create_unique_constraint(
        "uq_run_inputs_run_queue_sequence",
        "run_inputs",
        ["run_id", "queue_sequence"],
        schema="agents",
    )
    op.create_table(
        "run_events",
        Column("run_id", Uuid(as_uuid=True), primary_key=True),
        Column("sequence", Integer, primary_key=True),
        Column("occurred_at", DateTime(timezone=True), nullable=False),
        Column("kind", String(80), nullable=False),
        Column("operation_id", Uuid(as_uuid=True), nullable=True),
        Column("response_id", Uuid(as_uuid=True), nullable=True),
        Column("status", String(24), nullable=False),
        Column("reason_code", String(120), nullable=True),
        Column("native_cursor", Integer, nullable=True),
        Column("data", JSON, nullable=False),
        schema="agents",
    )


def downgrade() -> None:
    op.drop_table("run_events", schema="agents")
    op.drop_table("run_inputs", schema="agents")
    op.drop_column("runs", "original_grant_id", schema="agents")
