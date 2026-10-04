from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from sidera_control_plane.domain.errors import InputSelectionConflict
from sidera_control_plane.domain.input_selections import InputSelectionHardInputV1
from sidera_control_plane.persistence.migration_resources import migration_config
from sidera_control_plane.persistence.models import CollaborationInputSelectionModel
from tests.integration.test_input_selection_repository import (
    seed_selection_dependencies,
)
from tests.support.native_agents import (
    native_cp_guarded_database,  # noqa: F401
    native_cp_test_cleanup,  # noqa: F401
    native_cp_test_target_guard,  # noqa: F401
)

pytestmark = pytest.mark.integration


def test_hard_only_selection_keeps_original_repository_lineage_and_idempotency(
    database, workspace_id, uow_factory, monkeypatch
):
    original = seed_selection_dependencies(database, workspace_id)
    hard = InputSelectionHardInputV1(
        "source-a-to-target", "a" * 64, original.groups[0].sources[0]
    )
    draft = replace(original, groups=(), hard_inputs=(hard,))
    value = replace(draft, selection_hash=draft.compute_selection_hash())
    with uow_factory() as uow:
        assert uow.input_selections.add(value) == value
    with uow_factory() as uow:
        assert uow.input_selections.get(workspace_id, value.id) == value
        assert (
            uow.input_selections.get_by_intent(workspace_id, value.intent_id) == value
        )
        assert uow.input_selections.add(replace(value, id=uuid4())) == value
    changed = replace(value, hard_inputs=(replace(hard, rule_hash="b" * 64),))
    changed = replace(changed, selection_hash=changed.compute_selection_hash())
    with pytest.raises(InputSelectionConflict), uow_factory() as uow:
        uow.input_selections.add(changed)
    for groups, hard_inputs in (([], []), ({}, [hard.to_mapping()]), ([], {})):
        with pytest.raises(DBAPIError), database.session() as session:
            session.execute(
                update(CollaborationInputSelectionModel)
                .where(
                    CollaborationInputSelectionModel.id == value.id,
                )
                .values(groups_json=groups, hard_inputs_json=hard_inputs)
            )
            session.commit()
    with uow_factory() as uow:
        assert uow.input_selections.get(workspace_id, value.id) == value
        assert (
            uow.session.scalar(
                select(CollaborationInputSelectionModel.id).where(
                    CollaborationInputSelectionModel.intent_id == value.intent_id,
                )
            )
            == value.id
        )
    revision = ScriptDirectory.from_config(migration_config()).get_revision(
        "0039_hard_only_input_selections"
    )

    def forbidden_ddl(*args, **kwargs):
        pytest.fail("Hard-only downgrade must reject before any DDL")

    with database.engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        before = connection.scalar(text("SELECT version_num FROM alembic_version"))
        monkeypatch.setattr(
            revision.module,
            "op",
            SimpleNamespace(
                get_bind=lambda: connection,
                drop_constraint=forbidden_ddl,
                create_check_constraint=forbidden_ddl,
            ),
        )
        with pytest.raises(
            RuntimeError, match="Hard-only input selections prevent downgrade"
        ):
            revision.module.downgrade()
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == before
            == "0039_hard_only_input_selections"
        )
    with uow_factory() as uow:
        assert uow.input_selections.get(workspace_id, value.id) == value
