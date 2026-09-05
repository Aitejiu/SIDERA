"""Persist immutable R6.3.4 collaboration result-input selections."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_r6_3_4_result_inputs"
down_revision: str | None = "0025_r6_3_3_notification_gateway"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid() -> sa.types.TypeEngine:
    return sa.dialects.postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "collaboration_input_selections",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("root_task_id", _uuid(), nullable=False),
        sa.Column("run_id", _uuid(), nullable=False),
        sa.Column("plan_snapshot_id", _uuid(), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("plan_content_hash", sa.String(64), nullable=False),
        sa.Column("target_worker_item_id", _uuid(), nullable=False),
        sa.Column("target_execution_request_id", _uuid(), nullable=False),
        sa.Column("target_request_version", sa.Integer(), nullable=False),
        sa.Column("projection_snapshot_id", _uuid(), nullable=False),
        sa.Column("projection_hash", sa.String(64), nullable=False),
        sa.Column("decision_id", _uuid(), nullable=False),
        sa.Column("intent_id", _uuid(), nullable=False),
        sa.Column("groups_json", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column("hard_inputs_json", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column("selection_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 1 AND plan_generation >= 1 "
            "AND target_request_version >= 1",
            name="ck_collaboration_input_selections_versions",
        ),
        sa.CheckConstraint(
            "plan_content_hash ~ '^[0-9a-f]{64}$' "
            "AND projection_hash ~ '^[0-9a-f]{64}$' "
            "AND selection_hash ~ '^[0-9a-f]{64}$'",
            name="ck_collaboration_input_selections_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(groups_json) = 'array' "
            "AND jsonb_array_length(groups_json) >= 1",
            name="ck_collaboration_input_selections_groups_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(hard_inputs_json) = 'array'",
            name="ck_collaboration_input_selections_hard_inputs_array",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_input_selections_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_input_selections_root_task_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["collaboration_runs.id", "collaboration_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_input_selections_run_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_input_selections_plan_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_input_selections_worker_item_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["target_execution_request_id", "workspace_id"],
            ["execution_requests.id", "execution_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_input_selections_target_request_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["projection_snapshot_id", "workspace_id"],
            [
                "runtime_projection_snapshots.id",
                "runtime_projection_snapshots.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_input_selections_projection_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id", "workspace_id"],
            ["runtime_decisions.id", "runtime_decisions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_input_selections_decision_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["intent_id", "workspace_id"],
            ["runtime_intents.id", "runtime_intents.workspace_id"],
            ondelete="RESTRICT",
            name="fk_input_selections_intent_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "workspace_id",
            name="uq_collaboration_input_selections_identity",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "intent_id",
            name="uq_collaboration_input_selections_intent",
        ),
        sa.Index(
            "ix_collaboration_input_selections_target_request",
            "workspace_id",
            "target_execution_request_id",
            "created_at",
        ),
        sa.Index(
            "ix_collaboration_input_selections_consumer",
            "workspace_id",
            "target_worker_item_id",
            "created_at",
        ),
        sa.Index(
            "ix_collaboration_input_selections_plan",
            "workspace_id",
            "root_task_id",
            "plan_snapshot_id",
            "plan_generation",
        ),
    )

    for table_name in (
        "execution_requests",
        "runtime_releases",
        "collaboration_release_proposals",
    ):
        op.add_column(table_name, sa.Column("input_selection_id", _uuid()))
        op.add_column(
            table_name,
            sa.Column("input_selection_hash", sa.String(64)),
        )

    _add_selection_reference(
        "execution_requests",
        pair_name="ck_execution_requests_input_selection_pair",
        foreign_key_name="fk_execution_requests_input_selection_workspace",
    )
    _add_selection_reference(
        "runtime_releases",
        pair_name="ck_runtime_releases_input_selection_pair",
        foreign_key_name="fk_runtime_releases_input_selection_workspace",
    )
    _add_selection_reference(
        "collaboration_release_proposals",
        pair_name="ck_collaboration_release_proposals_input_selection_pair",
        foreign_key_name=(
            "fk_collaboration_release_proposals_input_selection_workspace"
        ),
    )


def _add_selection_reference(
    table_name: str, *, pair_name: str, foreign_key_name: str
) -> None:
    op.create_check_constraint(
        pair_name,
        table_name,
        "(input_selection_id IS NULL AND input_selection_hash IS NULL) OR "
        "(input_selection_id IS NOT NULL AND input_selection_hash "
        "~ '^[0-9a-f]{64}$')",
    )
    op.create_foreign_key(
        foreign_key_name,
        table_name,
        "collaboration_input_selections",
        ["input_selection_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    raise RuntimeError(
        "R6.3.4 result-input selection migration is forward-only; "
        "immutable authorization evidence must not be dropped"
    )
