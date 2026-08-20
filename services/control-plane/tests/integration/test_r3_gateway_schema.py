import pytest
from sqlalchemy import inspect

from sidera_control_plane.persistence.database import Database

pytestmark = pytest.mark.integration


def test_r3_gateway_schema_has_state_tables_and_safety_constraints(
    database: Database,
) -> None:
    expected_tables = {
        "module_definitions",
        "module_instances",
        "execution_bindings",
        "gateway_operations",
        "execution_workspaces",
        "process_descriptors",
        "execution_events",
        "artifacts",
        "evidence_claims",
        "execution_inputs",
    }
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        input_indexes = {
            item["name"] for item in inspector.get_indexes("execution_inputs")
        }
        event_constraints = {
            item["name"]
            for item in inspector.get_unique_constraints("execution_events")
        }
        evidence_checks = {
            item["name"]
            for item in inspector.get_check_constraints("evidence_claims")
        }

    assert expected_tables <= tables
    assert "uq_execution_inputs_one_pending_session" in input_indexes
    assert "uq_execution_events_session_sequence" in event_constraints
    assert "ck_evidence_claims_candidate_only" in evidence_checks
