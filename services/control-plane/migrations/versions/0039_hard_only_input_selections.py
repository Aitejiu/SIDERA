"""Allow existing schema-v1 hard inputs without optional groups."""

import sqlalchemy as sa
from alembic import op

revision = "0039_hard_only_input_selections"
down_revision = "0038_native_assignee_handoff"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "ck_collaboration_input_selections_groups_array",
        "collaboration_input_selections",
        type_="check",
    )
    op.create_check_constraint(
        "ck_collaboration_input_selections_groups_array",
        "collaboration_input_selections",
        "jsonb_typeof(groups_json) = 'array'",
    )
    op.create_check_constraint(
        "ck_collaboration_input_selections_nonempty",
        "collaboration_input_selections",
        "jsonb_array_length(groups_json) + jsonb_array_length(hard_inputs_json) >= 1",
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM collaboration_input_selections "
            "WHERE jsonb_array_length(groups_json) = 0)"
        )
    ):
        raise RuntimeError("Hard-only input selections prevent downgrade")
    op.drop_constraint(
        "ck_collaboration_input_selections_groups_array",
        "collaboration_input_selections",
        type_="check",
    )
    op.create_check_constraint(
        "ck_collaboration_input_selections_groups_array",
        "collaboration_input_selections",
        "jsonb_typeof(groups_json) = 'array' AND jsonb_array_length(groups_json) >= 1",
    )
    op.drop_constraint(
        "ck_collaboration_input_selections_nonempty",
        "collaboration_input_selections",
        type_="check",
    )
