import pytest
from sqlalchemy import inspect

pytestmark = pytest.mark.integration


def test_collaboration_schema_has_release_and_active_run_guards(database) -> None:
    names = set(inspect(database.engine).get_table_names())
    assert {
        "collaboration_runs",
        "runtime_projection_snapshots",
        "collaboration_evaluations",
        "runtime_decisions",
        "runtime_intents",
        "runtime_intent_applications",
        "runtime_releases",
    } <= names

    request_columns = {
        column["name"]
        for column in inspect(database.engine).get_columns("execution_requests")
    }
    assert "collaboration_gate_mode" in request_columns

    indexes = {
        index["name"]
        for index in inspect(database.engine).get_indexes("collaboration_runs")
    }
    assert "uq_collaboration_runs_root_active" in indexes

    release_constraints = {
        constraint["name"]
        for constraint in inspect(database.engine).get_unique_constraints(
            "runtime_releases"
        )
    }
    assert {
        "uq_runtime_releases_workspace_request",
        "uq_runtime_releases_intent",
    } <= release_constraints
