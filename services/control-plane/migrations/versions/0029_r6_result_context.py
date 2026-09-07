"""Persist exact code and verification input context contracts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_r6_result_context"
down_revision: str | None = "0028_r6_result_integration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb() -> sa.types.TypeEngine:
    return sa.dialects.postgresql.JSONB


def upgrade() -> None:
    op.drop_constraint(
        "ck_context_assembly_requests_manifest_protocol",
        "context_assembly_requests",
        type_="check",
    )
    op.add_column(
        "context_assembly_requests",
        sa.Column("code_input_manifest", _jsonb()),
    )
    op.add_column(
        "context_assembly_requests",
        sa.Column("verification_manifest", _jsonb()),
    )
    op.create_check_constraint(
        "ck_context_assembly_requests_manifest_protocol",
        "context_assembly_requests",
        "(manifest_schema_version = 1 AND input_selection_id IS NULL "
        "AND input_selection_hash IS NULL AND result_input_budget IS NULL "
        "AND code_input_manifest IS NULL AND verification_manifest IS NULL) OR "
        "(manifest_schema_version = 2 AND input_selection_id IS NOT NULL "
        "AND input_selection_hash ~ '^[0-9a-f]{64}$' "
        "AND jsonb_typeof(result_input_budget) = 'object' "
        "AND code_input_manifest IS NULL AND verification_manifest IS NULL) OR "
        "(manifest_schema_version = 3 AND "
        "((code_input_manifest IS NOT NULL AND verification_manifest IS NULL) OR "
        "(code_input_manifest IS NULL AND verification_manifest IS NOT NULL)) AND "
        "((input_selection_id IS NULL AND input_selection_hash IS NULL "
        "AND result_input_budget IS NULL) OR "
        "(input_selection_id IS NOT NULL "
        "AND input_selection_hash ~ '^[0-9a-f]{64}$' "
        "AND jsonb_typeof(result_input_budget) = 'object')) AND "
        "jsonb_typeof(COALESCE(code_input_manifest, verification_manifest)) = 'object')",
    )

    op.drop_constraint(
        "ck_context_packages_manifest_protocol",
        "context_packages",
        type_="check",
    )
    op.add_column(
        "context_packages",
        sa.Column("code_input_manifest", _jsonb()),
    )
    op.add_column(
        "context_packages",
        sa.Column("verification_manifest", _jsonb()),
    )
    op.create_check_constraint(
        "ck_context_packages_manifest_protocol",
        "context_packages",
        "(manifest_schema_version = 1 AND result_input_manifest IS NULL "
        "AND code_input_manifest IS NULL AND verification_manifest IS NULL) OR "
        "(manifest_schema_version = 2 "
        "AND jsonb_typeof(result_input_manifest) = 'object' "
        "AND code_input_manifest IS NULL AND verification_manifest IS NULL) OR "
        "(manifest_schema_version = 3 AND "
        "((code_input_manifest IS NOT NULL AND verification_manifest IS NULL) OR "
        "(code_input_manifest IS NULL AND verification_manifest IS NOT NULL)) AND "
        "(result_input_manifest IS NULL OR "
        "jsonb_typeof(result_input_manifest) = 'object') AND "
        "jsonb_typeof(COALESCE(code_input_manifest, verification_manifest)) = 'object')",
    )


def downgrade() -> None:
    raise RuntimeError(
        "R6 result context contracts are forward-only and must not be dropped"
    )
