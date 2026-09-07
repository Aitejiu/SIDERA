from __future__ import annotations

import pytest
import sqlalchemy as sa

from sidera_control_plane.persistence.database import Database


@pytest.mark.integration
def test_result_ledger_tables_are_workspace_scoped(
    database: Database, clean_database: None
) -> None:
    inspector = sa.inspect(database.engine)
    tables = set(inspector.get_table_names())

    assert {
        "result_operation_attempts",
        "code_result_snapshots",
        "integration_candidates",
        "result_verification_results",
        "task_acceptance_decisions",
        "result_rework_records",
        "result_apply_operations",
        "task_result_heads",
    } <= tables

    columns = {
        row["name"] for row in inspector.get_columns("task_result_heads")
    }
    assert {
        "workspace_id",
        "root_task_id",
        "result_version",
        "auto_rounds_used",
        "auto_rounds_limit",
        "auto_seconds_limit",
    } <= columns

    primary_key = inspector.get_pk_constraint("task_result_heads")
    assert primary_key["name"] == "pk_task_result_heads_identity"
    assert primary_key["constrained_columns"] == ["workspace_id", "root_task_id"]


@pytest.mark.integration
def test_result_operation_ledger_exposes_idempotency_constraints(
    database: Database, clean_database: None
) -> None:
    inspector = sa.inspect(database.engine)

    operation_columns = {
        row["name"] for row in inspector.get_columns("result_operation_attempts")
    }
    assert {"operation_id", "operation_kind", "status", "payload"} <= operation_columns
    indexes = {
        item["name"]
        for item in inspector.get_indexes("result_apply_operations")
    }
    assert "uq_result_apply_operations_active_root" in indexes

    request_columns = {
        row["name"] for row in inspector.get_columns("execution_requests")
    }
    assert {
        "result_derivation_kind",
        "source_candidate_id",
        "rework_record_id",
    } <= request_columns
