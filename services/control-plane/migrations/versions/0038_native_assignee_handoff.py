"""Add explicit native-assignee variants to the existing handoff lifecycle."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0038_native_assignee_handoff"
down_revision = "0037_native_input_query_schedule"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        "target_worker_id",
        "target_worker_version",
        "target_capability_snapshot_hash",
    ):
        op.alter_column("handoff_proposals", column, nullable=True)
    op.drop_constraint(
        "ck_handoff_proposals_schema_version", "handoff_proposals", type_="check"
    )
    op.create_check_constraint(
        "ck_handoff_proposals_schema_version",
        "handoff_proposals",
        "proposal_schema_version IN (1, 2)",
    )
    op.create_check_constraint(
        "ck_handoff_proposals_variant",
        "handoff_proposals",
        "((proposal_schema_version = 1 AND target_worker_id IS NOT NULL "
        "AND target_worker_version IS NOT NULL AND target_capability_snapshot_hash IS NOT NULL) "
        "OR (proposal_schema_version = 2 AND target_worker_id IS NULL "
        "AND target_worker_version IS NULL AND target_capability_snapshot_hash IS NULL "
        "AND jsonb_typeof(payload->'proposal_schema_version') = 'number' "
        "AND payload->>'proposal_schema_version' = '2' "
        "AND jsonb_typeof(payload->'target_assignee') = 'object' "
        "AND payload->'target_assignee'->>'kind' IN ('profile', 'human') "
        "AND jsonb_typeof(payload->'target_assignee'->'id') = 'string')) IS TRUE",
    )
    op.alter_column("handoff_transitions", "target_worker_id", nullable=True)
    op.add_column(
        "handoff_transitions",
        sa.Column(
            "handoff_schema_version", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    op.add_column(
        "handoff_transitions",
        sa.Column("native_payload", postgresql.JSONB(none_as_null=True)),
    )
    op.add_column(
        "handoff_transitions", sa.Column("target_binding_version", sa.Integer())
    )
    op.add_column(
        "handoff_transitions",
        sa.Column("manual_context_facts", postgresql.JSONB(none_as_null=True)),
    )
    op.create_check_constraint(
        "ck_handoff_transitions_variant",
        "handoff_transitions",
        "((handoff_schema_version = 1 AND target_worker_id IS NOT NULL "
        "AND native_payload IS NULL AND target_binding_version IS NULL AND manual_context_facts IS NULL) "
        "OR (handoff_schema_version = 2 AND native_payload IS NOT NULL "
        "AND jsonb_typeof(native_payload) = 'object' "
        "AND jsonb_typeof(native_payload->'proposal_schema_version') = 'number' "
        "AND native_payload->>'proposal_schema_version' = '2' "
        "AND jsonb_typeof(native_payload->'target_assignee') = 'object' "
        "AND native_payload->'target_assignee'->>'kind' IN ('profile', 'human') "
        "AND jsonb_typeof(native_payload->'target_assignee'->'id') = 'string' "
        "AND (target_binding_version IS NULL OR target_binding_version >= 1) "
        "AND (manual_context_facts IS NULL OR jsonb_typeof(manual_context_facts) = 'object'))) IS TRUE",
    )
    for column in ("target_worker_id", "target_runtime_snapshot_hash"):
        op.alter_column("handoff_context_bundles", column, nullable=True)
    op.add_column(
        "handoff_context_bundles",
        sa.Column("target_profile_ref", postgresql.JSONB(none_as_null=True)),
    )
    op.add_column(
        "handoff_context_bundles", sa.Column("target_binding_version", sa.Integer())
    )
    op.add_column(
        "handoff_context_bundles",
        sa.Column("target_native_metadata", postgresql.JSONB(none_as_null=True)),
    )
    op.drop_constraint(
        "ck_handoff_context_bundles_schema_version",
        "handoff_context_bundles",
        type_="check",
    )
    op.create_check_constraint(
        "ck_handoff_context_bundles_schema_version",
        "handoff_context_bundles",
        "schema_version IN (1, 2)",
    )
    op.create_check_constraint(
        "ck_handoff_context_bundles_variant",
        "handoff_context_bundles",
        "((schema_version = 1 AND target_worker_id IS NOT NULL "
        "AND target_runtime_snapshot_hash IS NOT NULL AND target_profile_ref IS NULL "
        "AND target_binding_version IS NULL AND target_native_metadata IS NULL) "
        "OR (schema_version = 2 AND target_worker_id IS NULL "
        "AND target_runtime_snapshot_hash IS NULL AND target_profile_ref IS NOT NULL "
        "AND jsonb_typeof(target_profile_ref) = 'object' "
        "AND target_profile_ref ?& ARRAY['workspace_id', 'profile_id', 'revision'] "
        "AND target_binding_version IS NOT NULL AND target_binding_version >= 1 "
        "AND target_native_metadata IS NOT NULL AND jsonb_typeof(target_native_metadata) = 'object' "
        "AND target_native_metadata->>'execution_mode' = 'native_profile')) IS TRUE",
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION sidera_reject_handoff_bundle_payload_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.bundle_hash IS DISTINCT FROM OLD.bundle_hash
               OR NEW.repository_state IS DISTINCT FROM OLD.repository_state
               OR NEW.artifact_refs IS DISTINCT FROM OLD.artifact_refs
               OR NEW.evidence_refs IS DISTINCT FROM OLD.evidence_refs
               OR NEW.delivered_input_refs IS DISTINCT FROM OLD.delivered_input_refs
               OR NEW.transferable_input_refs IS DISTINCT FROM OLD.transferable_input_refs
               OR NEW.excluded_input_refs IS DISTINCT FROM OLD.excluded_input_refs
               OR ((NEW.schema_version = 2 OR OLD.schema_version = 2)
                   AND (to_jsonb(NEW) - 'version') IS DISTINCT FROM (to_jsonb(OLD) - 'version')) THEN
                RAISE EXCEPTION 'handoff context bundle payload is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)


def downgrade():
    raise RuntimeError("native handoff history cannot be downgraded")
