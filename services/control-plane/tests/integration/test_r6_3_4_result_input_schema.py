from __future__ import annotations

import pytest
from sqlalchemy import inspect, select, update
from sqlalchemy.exc import IntegrityError

from sidera_control_plane.persistence.input_selection_repository import (
    _selection_values,
)
from sidera_control_plane.persistence.models import (
    CollaborationInputSelectionModel,
    ExecutionRequestModel,
)

from .test_input_selection_repository import seed_selection_dependencies
from .test_r5_1_execution_request_schema import (
    WORKSPACE_B,
    request_row,
    seed_execution_request_facts,
)

pytestmark = pytest.mark.integration


def test_result_input_schema_has_ledger_and_paired_references(database) -> None:
    inspector = inspect(database.engine)
    assert "collaboration_input_selections" in set(inspector.get_table_names())

    selection_columns = {
        column["name"]
        for column in inspector.get_columns("collaboration_input_selections")
    }
    assert {
        "id",
        "workspace_id",
        "schema_version",
        "root_task_id",
        "run_id",
        "plan_snapshot_id",
        "plan_generation",
        "plan_content_hash",
        "target_worker_item_id",
        "target_execution_request_id",
        "target_request_version",
        "projection_snapshot_id",
        "projection_hash",
        "decision_id",
        "intent_id",
        "groups_json",
        "hard_inputs_json",
        "selection_hash",
        "created_at",
    } <= selection_columns

    unique_constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "collaboration_input_selections"
        )
    }
    assert {
        "uq_collaboration_input_selections_identity",
        "uq_collaboration_input_selections_intent",
    } <= unique_constraints

    for table_name in (
        "execution_requests",
        "runtime_releases",
        "collaboration_release_proposals",
    ):
        columns = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        assert {"input_selection_id", "input_selection_hash"} <= columns

    indexes = {
        index["name"]
        for index in inspector.get_indexes("collaboration_input_selections")
    }
    assert {
        "ix_collaboration_input_selections_consumer",
        "ix_collaboration_input_selections_target_request",
        "ix_collaboration_input_selections_plan",
    } <= indexes

    paired_checks = {
        "execution_requests": "ck_execution_requests_input_selection_pair",
        "runtime_releases": "ck_runtime_releases_input_selection_pair",
        "collaboration_release_proposals": (
            "ck_collaboration_release_proposals_input_selection_pair"
        ),
    }
    for table_name, check_name in paired_checks.items():
        assert check_name in {
            item["name"] for item in inspector.get_check_constraints(table_name)
        }


def test_historical_request_keeps_both_selection_fields_null(
    database, workspace_id
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    with database.session() as session:
        session.add(request_row(ids, request_id=ids["request"]))
        session.commit()

    with database.session() as session:
        row = session.scalar(
            select(ExecutionRequestModel).where(
                ExecutionRequestModel.id == ids["request"]
            )
        )
        assert row is not None
        assert row.input_selection_id is None
        assert row.input_selection_hash is None


def test_selection_reference_cannot_be_half_populated(
    database, workspace_id
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    with database.session() as session:
        session.add(request_row(ids, request_id=ids["request"]))
        session.commit()

    with pytest.raises(IntegrityError), database.session() as session:
        session.execute(
            update(ExecutionRequestModel)
            .where(ExecutionRequestModel.id == ids["request"])
            .values(input_selection_id=ids["execution"])
        )
        session.commit()

    with pytest.raises(IntegrityError), database.session() as session:
        session.execute(
            update(ExecutionRequestModel)
            .where(ExecutionRequestModel.id == ids["request"])
            .values(input_selection_hash="a" * 64)
        )
        session.commit()


def test_selection_schema_version_and_workspace_scope_are_enforced(
    database, workspace_id, uow_factory
) -> None:
    value = seed_selection_dependencies(database, workspace_id)
    with uow_factory() as uow:
        uow.input_selections.add(value)

    with pytest.raises(IntegrityError), database.session() as session:
        session.execute(
            update(CollaborationInputSelectionModel)
            .where(CollaborationInputSelectionModel.id == value.id)
            .values(schema_version=2)
        )
        session.commit()

    cross_workspace_values = _selection_values(value)
    cross_workspace_values["id"] = value.intent_id
    cross_workspace_values["workspace_id"] = WORKSPACE_B
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(CollaborationInputSelectionModel(**cross_workspace_values))
        session.commit()
