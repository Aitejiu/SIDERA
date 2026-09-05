from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.errors import InputSelectionConflict, ValidationError
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
)

from ..unit.result_input_fixtures import selection_mapping
from .test_collaboration_repository import (
    _decision,
    _evaluation,
    _intent,
    _projection,
    _run,
)
from .test_r5_1_execution_request_schema import (
    NOW,
    request_row,
    seed_execution_request_facts,
)

pytestmark = pytest.mark.integration


def seed_selection_dependencies(
    database, workspace_id: UUID
) -> CollaborationInputSelectionV1:
    ids = seed_execution_request_facts(database, workspace_id)
    with database.session() as session:
        session.add(request_row(ids, request_id=ids["request"]))
        session.commit()

    run = _run(ids, workspace_id)
    projection = _projection(run)
    evaluation = _evaluation(run, projection)
    decision = _decision(run, projection, evaluation, sequence=1)
    intent = _intent(run, decision, ids["request"])
    from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.collaboration_runs.add(run)
        uow.flush()
        uow.collaboration_ledger.get_or_add_projection(projection)
        uow.collaboration_evaluations.get_or_add(evaluation)
        uow.collaboration_ledger.add_decision_bundle(decision, (intent,))

    fixture = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    draft = replace(
        fixture,
        id=uuid4(),
        workspace_id=workspace_id,
        root_task_id=ids["root_task"],
        run_id=run.id,
        plan_snapshot_id=ids["plan"],
        plan_generation=1,
        plan_content_hash=run.plan_content_hash,
        target_worker_item_id=ids["worker_item"],
        target_execution_request_id=ids["request"],
        target_request_version=1,
        projection_snapshot_id=projection.id,
        projection_hash=projection.content_hash,
        decision_id=decision.id,
        intent_id=intent.id,
        selection_hash="0" * 64,
        created_at=NOW,
    )
    return replace(draft, selection_hash=draft.compute_selection_hash())


def test_selection_repository_is_workspace_scoped(
    database, workspace_id: UUID, uow_factory
) -> None:
    value = seed_selection_dependencies(database, workspace_id)

    with uow_factory() as uow:
        stored = uow.input_selections.add(value)
        assert stored == value

    with uow_factory() as uow:
        loaded = uow.input_selections.get(workspace_id, value.id)
        assert loaded is not None
        assert loaded.selection_hash == value.selection_hash
        assert uow.input_selections.get(uuid4(), value.id) is None
        assert uow.input_selections.get_by_intent(
            workspace_id, value.intent_id
        ) == value
        assert uow.input_selections.get_by_intent(
            uuid4(), value.intent_id
        ) is None


def test_same_intent_and_hash_returns_original_selection(
    database, workspace_id: UUID, uow_factory
) -> None:
    value = seed_selection_dependencies(database, workspace_id)
    replay = replace(value, id=uuid4())

    with uow_factory() as uow:
        original = uow.input_selections.add(value)
    with uow_factory() as uow:
        stored = uow.input_selections.add(replay)

    assert stored == original
    assert stored.id == value.id


def test_same_intent_with_different_hash_conflicts(
    database, workspace_id: UUID, uow_factory
) -> None:
    value = seed_selection_dependencies(database, workspace_id)
    changed_group = replace(value.groups[0], rule_hash="f" * 64)
    draft = replace(
        value,
        id=uuid4(),
        groups=(changed_group,),
        selection_hash="0" * 64,
    )
    conflicting = replace(draft, selection_hash=draft.compute_selection_hash())

    with uow_factory() as uow:
        uow.input_selections.add(value)

    with (
        pytest.raises(InputSelectionConflict, match="input_selection_conflict"),
        uow_factory() as uow,
    ):
        uow.input_selections.add(conflicting)


def test_selection_repository_rejects_mismatched_semantic_hash(
    database, workspace_id: UUID, uow_factory
) -> None:
    value = seed_selection_dependencies(database, workspace_id)
    malformed = replace(value, selection_hash="f" * 64)

    with (
        pytest.raises(ValidationError, match="immutable contents"),
        uow_factory() as uow,
    ):
        uow.input_selections.add(malformed)
