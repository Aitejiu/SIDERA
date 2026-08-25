from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from sqlalchemy import inspect, text

from sidera_control_plane.persistence.database import Database

pytestmark = pytest.mark.integration


def _column(columns: Sequence[Mapping[str, Any]], name: str) -> Mapping[str, Any]:
    return next(column for column in columns if column["name"] == name)


def _constraint_names(connection, table_name: str) -> set[str]:
    rows = connection.execute(
        text(
            """
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_schema = current_schema()
              AND table_name = :table_name
            """
        ),
        {"table_name": table_name},
    )
    return {row.constraint_name for row in rows}


def test_r5_1_1_schema_adds_runtime_binding_and_scheduling_tables(
    database: Database,
) -> None:
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        assert {
            "worker_runtime_configurations",
            "scheduling_blocks",
        }.issubset(tables)

        runtime_columns = inspector.get_columns("worker_runtime_configurations")
        assert {
            "worker_id",
            "workspace_id",
            "adapter_module_instance_id",
            "adapter_module_config_version",
            "workspace_module_instance_id",
            "workspace_module_config_version",
            "version",
            "created_at",
            "updated_at",
        }.issubset({column["name"] for column in runtime_columns})
        assert inspector.get_pk_constraint("worker_runtime_configurations")[
            "constrained_columns"
        ] == ["worker_id"]

        block_columns = inspector.get_columns("scheduling_blocks")
        assert {
            "id",
            "workspace_id",
            "execution_id",
            "status",
            "reason_code",
            "reason_details",
            "fingerprint",
            "requires_operator",
            "first_seen_at",
            "last_seen_at",
            "occurrence_count",
            "resolved_at",
            "version",
            "created_at",
            "updated_at",
        }.issubset({column["name"] for column in block_columns})

        assert {
            "ck_worker_runtime_configurations_adapter_config_version",
            "ck_worker_runtime_configurations_workspace_config_version",
            "ck_worker_runtime_configurations_version",
        }.issubset(
            _constraint_names(connection, "worker_runtime_configurations")
        )
        assert {
            "ck_scheduling_blocks_reason_details_object",
            "ck_scheduling_blocks_occurrence_count",
            "ck_scheduling_blocks_version",
        }.issubset(_constraint_names(connection, "scheduling_blocks"))


def test_r5_1_1_schema_has_tenant_safe_restrict_fks_and_active_block_index(
    database: Database,
) -> None:
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        runtime_fks = {
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                fk.get("options", {}).get("ondelete"),
            )
            for fk in inspector.get_foreign_keys("worker_runtime_configurations")
        }
        assert (
            ("worker_id", "workspace_id"),
            "workers",
            "RESTRICT",
        ) in runtime_fks
        assert (
            ("adapter_module_instance_id", "workspace_id"),
            "module_instances",
            "RESTRICT",
        ) in runtime_fks
        assert (
            ("workspace_module_instance_id", "workspace_id"),
            "module_instances",
            "RESTRICT",
        ) in runtime_fks

        block_fks = {
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                fk.get("options", {}).get("ondelete"),
            )
            for fk in inspector.get_foreign_keys("scheduling_blocks")
        }
        assert (
            ("execution_id", "workspace_id"),
            "executions",
            "RESTRICT",
        ) in block_fks

        index_rows = connection.execute(
            text(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'scheduling_blocks'
                """
            )
        )
        indexes = {row.indexname: row.indexdef for row in index_rows}
        active_index = indexes["uq_scheduling_blocks_active_execution"]
        assert "UNIQUE" in active_index
        assert "execution_id, workspace_id" in active_index
        assert "status" in active_index and "'active'" in active_index


def test_r5_1_1_schema_adds_policy_arrays_and_resource_versions(
    database: Database,
) -> None:
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        for table_name, column_name in (
            ("executions", "adapter_preferences"),
            ("executions", "adapter_forbidden"),
            ("execution_requests", "adapter_forbidden"),
        ):
            column = _column(inspector.get_columns(table_name), column_name)
            assert column["nullable"] is False
            assert "[]" in str(column["default"])

        for table_name in ("module_definitions", "module_instances"):
            column = _column(inspector.get_columns(table_name), "resource_version")
            assert column["nullable"] is False
            assert "1" in str(column["default"])
            assert f"ck_{table_name}_resource_version" in _constraint_names(
                connection, table_name
            )

        assert {
            "ck_executions_adapter_preferences_array",
            "ck_executions_adapter_forbidden_array",
        }.issubset(_constraint_names(connection, "executions"))
        assert "ck_execution_requests_adapter_forbidden_array" in _constraint_names(
            connection, "execution_requests"
        )


def test_r5_1_1_schema_remains_present_after_r5_3_upgrade(
    database: Database,
) -> None:
    with database.engine.connect() as connection:
        version = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        assert version == "0016_r5_3_work_context_packages"
