"""Persist versioned ContextPackage result-input delivery contracts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_r6_3_4_result_input_context"
down_revision: str | None = "0026_r6_3_4_result_inputs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid() -> sa.types.TypeEngine:
    return sa.dialects.postgresql.UUID(as_uuid=True)


def _jsonb() -> sa.types.TypeEngine:
    return sa.dialects.postgresql.JSONB


def _ensure_selection_consumer_index() -> None:
    """Repair databases that applied 0026 without its consumer index."""

    bind = op.get_bind()
    index_names = {
        item["name"]
        for item in sa.inspect(bind).get_indexes("collaboration_input_selections")
    }
    if "ix_collaboration_input_selections_consumer" not in index_names:
        op.create_index(
            "ix_collaboration_input_selections_consumer",
            "collaboration_input_selections",
            ["workspace_id", "target_worker_item_id", "created_at"],
        )


def upgrade() -> None:
    _ensure_selection_consumer_index()
    op.add_column(
        "context_assembly_requests",
        sa.Column(
            "manifest_schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column("context_assembly_requests", sa.Column("input_selection_id", _uuid()))
    op.add_column(
        "context_assembly_requests",
        sa.Column("input_selection_hash", sa.String(64)),
    )
    op.add_column(
        "context_assembly_requests",
        sa.Column("result_input_budget", _jsonb()),
    )
    op.drop_constraint(
        "uq_context_assembly_requests_purpose_consumer",
        "context_assembly_requests",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_context_assembly_requests_purpose_consumer_protocol",
        "context_assembly_requests",
        ["purpose", "consumer_id", "manifest_schema_version"],
    )
    op.create_check_constraint(
        "ck_context_assembly_requests_manifest_protocol",
        "context_assembly_requests",
        "(manifest_schema_version = 1 AND input_selection_id IS NULL "
        "AND input_selection_hash IS NULL AND result_input_budget IS NULL) OR "
        "(manifest_schema_version = 2 AND input_selection_id IS NOT NULL "
        "AND input_selection_hash ~ '^[0-9a-f]{64}$' "
        "AND jsonb_typeof(result_input_budget) = 'object')",
    )
    op.create_foreign_key(
        "fk_context_assembly_requests_input_selection_workspace",
        "context_assembly_requests",
        "collaboration_input_selections",
        ["input_selection_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "context_packages",
        sa.Column(
            "manifest_schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column("context_packages", sa.Column("result_input_manifest", _jsonb()))
    op.drop_constraint(
        "uq_context_packages_purpose_consumer",
        "context_packages",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_context_packages_purpose_consumer_protocol",
        "context_packages",
        ["purpose", "consumer_id", "manifest_schema_version"],
    )
    op.create_check_constraint(
        "ck_context_packages_manifest_protocol",
        "context_packages",
        "(manifest_schema_version = 1 AND result_input_manifest IS NULL) OR "
        "(manifest_schema_version = 2 "
        "AND jsonb_typeof(result_input_manifest) = 'object')",
    )


def downgrade() -> None:
    raise RuntimeError(
        "R6.3.4 context result-input protocol migration is forward-only; "
        "immutable delivery contracts must not be dropped"
    )
