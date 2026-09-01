"""Repair handoff lineage indexing and bind a transfer target once.

Revision ID: 0020_r6_2_workspace_transfer_outcome
Revises: 0019_r6_2_agent_handoff
Create Date: 2026-09-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020_r6_2_workspace_transfer_outcome"
down_revision: str | None = "0019_r6_2_agent_handoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_inputs_handoff_source
        ON execution_inputs (workspace_id, handoff_transition_id, source_input_id)
        WHERE source_input_id IS NOT NULL
        """
    )
    op.execute(_transfer_provenance_function(allow_initial_target_binding=True))


def downgrade() -> None:
    op.execute(_transfer_provenance_function(allow_initial_target_binding=False))
    op.execute("DROP INDEX IF EXISTS uq_execution_inputs_handoff_source")


def _transfer_provenance_function(*, allow_initial_target_binding: bool) -> str:
    target_manifest_guard = (
        "OR (OLD.target_manifest_id IS NOT NULL "
        "AND NEW.target_manifest_id IS DISTINCT FROM OLD.target_manifest_id)"
        if allow_initial_target_binding
        else "OR NEW.target_manifest_id IS DISTINCT FROM OLD.target_manifest_id"
    )
    return f"""
        CREATE OR REPLACE FUNCTION sidera_reject_workspace_transfer_provenance_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.handoff_transition_id IS DISTINCT FROM OLD.handoff_transition_id
               OR NEW.logical_attempt IS DISTINCT FROM OLD.logical_attempt
               OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
               OR NEW.source_manifest_id IS DISTINCT FROM OLD.source_manifest_id
               {target_manifest_guard}
               OR NEW.source_execution_id IS DISTINCT FROM OLD.source_execution_id
               OR NEW.target_execution_id IS DISTINCT FROM OLD.target_execution_id
               OR NEW.source_fencing_token IS DISTINCT FROM OLD.source_fencing_token
               OR NEW.target_fencing_token IS DISTINCT FROM OLD.target_fencing_token
               OR NEW.location_ref_hash IS DISTINCT FROM OLD.location_ref_hash
               OR NEW.repository_state_digest IS DISTINCT FROM OLD.repository_state_digest
               OR NEW.provider_operation_id IS DISTINCT FROM OLD.provider_operation_id
               OR NEW.transfer_mode IS DISTINCT FROM OLD.transfer_mode THEN
                RAISE EXCEPTION 'workspace transfer provenance is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """
