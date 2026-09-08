from __future__ import annotations

import pytest
from sqlalchemy import inspect

from sidera_control_plane.persistence.database import Database

pytestmark = pytest.mark.integration


def test_real_run_identity_schema_is_forward_compatible(database: Database) -> None:
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        run_columns = {
            column["name"] for column in inspector.get_columns("real_result_run_budgets")
        }
        reservation_columns = {
            column["name"]
            for column in inspector.get_columns("real_result_execution_reservations")
        }
        assert {"profile_fingerprint", "expires_at"}.issubset(run_columns)
        assert {"execution_id", "session_id"}.issubset(reservation_columns)

        checks = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(
                "real_result_execution_reservations"
            )
        }
        assert "ck_real_result_execution_reservations_binding_pair" in checks

        indexes = {
            index["name"] for index in inspector.get_indexes("real_result_execution_reservations")
        }
        assert {
            "uq_real_result_execution_reservations_execution",
            "uq_real_result_execution_reservations_session",
        }.issubset(indexes)
