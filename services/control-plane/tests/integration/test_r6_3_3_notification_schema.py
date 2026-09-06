from __future__ import annotations

import importlib.util
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect, text

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    NotificationDeliveryAttemptModel,
    NotificationDeliveryIntentModel,
    NotificationFactModel,
    NotificationPolicyStateModel,
    NotificationReconciliationCursorModel,
    NotificationRouteModel,
    NotificationSourceBindingModel,
    NotificationThreadModel,
)

pytestmark = pytest.mark.integration


EXPECTED_TABLES = {
    "notification_policy_states",
    "notification_facts",
    "notification_threads",
    "notification_routes",
    "notification_source_bindings",
    "notification_delivery_intents",
    "notification_delivery_attempts",
    "notification_reconciliation_cursors",
}

EXPECTED_COLUMNS: dict[str, set[str]] = {
    "notification_policy_states": {
        "workspace_id",
        "recipient_type",
        "recipient_id",
        "subject_type",
        "subject_id",
        "topic",
        "policy_version",
        "observed_status",
        "observed_fingerprint",
        "observed_payload",
        "first_observed_at",
        "not_before",
        "last_notified_fingerprint",
        "active_thread_id",
        "resource_version",
        "updated_at",
    },
    "notification_facts": {
        "id",
        "workspace_id",
        "policy_version",
        "recipient_type",
        "recipient_id",
        "kind",
        "priority",
        "transition",
        "topic",
        "root_task_id",
        "subtask_id",
        "worker_item_id",
        "source_projection_cursor",
        "source_projection_type",
        "source_resource_id",
        "source_resource_version",
        "semantic_fingerprint",
        "title",
        "summary",
        "action",
        "deep_link",
        "correlation_id",
        "baseline_import",
        "occurred_at",
        "created_at",
    },
    "notification_threads": {
        "id",
        "workspace_id",
        "recipient_type",
        "recipient_id",
        "thread_key",
        "topic",
        "root_task_id",
        "subtask_id",
        "worker_item_id",
        "latest_fact_id",
        "business_status",
        "organization_status",
        "last_read_fact_id",
        "unread_count",
        "version",
        "opened_at",
        "resolved_at",
        "archived_at",
        "updated_at",
    },
    "notification_routes": {
        "id",
        "workspace_id",
        "recipient_type",
        "recipient_id",
        "channel_type",
        "channel_module_instance_id",
        "source_ref",
        "status",
        "version",
        "registered_at",
        "updated_at",
    },
    "notification_source_bindings": {
        "workspace_id",
        "resource_type",
        "resource_id",
        "notification_route_id",
        "binding_kind",
        "bound_at",
    },
    "notification_delivery_intents": {
        "id",
        "workspace_id",
        "notification_fact_id",
        "notification_route_id",
        "channel_module_instance_id",
        "delivery_round",
        "contract_version",
        "status",
        "attempts",
        "next_attempt_at",
        "claimed_until",
        "claim_token",
        "fencing_token",
        "provider_message_ref",
        "last_error_code",
        "delivered_at",
        "exhausted_at",
        "created_at",
        "updated_at",
    },
    "notification_delivery_attempts": {
        "delivery_intent_id",
        "attempt_number",
        "claim_token",
        "fencing_token",
        "outcome",
        "error_code",
        "provider_message_ref",
        "started_at",
        "finished_at",
    },
    "notification_reconciliation_cursors": {
        "workspace_id",
        "policy_version",
        "last_projection_cursor",
        "last_reconciled_at",
        "lease_owner",
        "lease_until",
        "version",
    },
}


def _column_names(columns: Sequence[Mapping[str, Any]]) -> set[str]:
    return {str(column["name"]) for column in columns}


def _foreign_keys(
    connection: Any, table_name: str
) -> set[tuple[tuple[str, ...], str, str | None]]:
    return {
        (
            tuple(item["constrained_columns"]),
            str(item["referred_table"]),
            item.get("options", {}).get("ondelete"),
        )
        for item in inspect(connection).get_foreign_keys(table_name)
    }


def _primary_key_name(connection: Any, table_name: str) -> str | None:
    return inspect(connection).get_pk_constraint(table_name).get("name")


def test_r6_3_3_orm_declares_the_eight_notification_tables() -> None:
    models = (
        NotificationPolicyStateModel,
        NotificationFactModel,
        NotificationThreadModel,
        NotificationRouteModel,
        NotificationSourceBindingModel,
        NotificationDeliveryIntentModel,
        NotificationDeliveryAttemptModel,
        NotificationReconciliationCursorModel,
    )
    assert {model.__tablename__ for model in models} == EXPECTED_TABLES


def test_r6_3_3_schema_has_eight_tenant_safe_notification_tables(
    database: Database,
) -> None:
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        assert EXPECTED_TABLES <= set(inspector.get_table_names())
        for table_name, expected_columns in EXPECTED_COLUMNS.items():
            assert expected_columns <= _column_names(
                inspector.get_columns(table_name)
            )

        for table_name in EXPECTED_TABLES:
            assert {
                "raw_payload",
                "provider_raw_response",
                "provider_error_body",
                "error_body",
                "chat_id",
                "username",
            }.isdisjoint(_column_names(inspector.get_columns(table_name)))

        for table_name in EXPECTED_TABLES - {"notification_delivery_attempts"}:
            assert all(
                item[2] == "RESTRICT"
                for item in _foreign_keys(connection, table_name)
                if item[0] == ("workspace_id",)
            )


def test_r6_3_3_schema_has_identity_constraints_and_operational_indexes(
    database: Database,
) -> None:
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        uniques = {
            table: {
                item["name"]
                for item in inspector.get_unique_constraints(table)
                if item["name"] is not None
            }
            for table in EXPECTED_TABLES
        }
        assert _primary_key_name(connection, "notification_policy_states") == (
            "pk_notification_policy_states_scope"
        )
        assert {
            "uq_notification_facts_identity",
            "uq_notification_facts_semantic",
        } <= uniques["notification_facts"]
        assert {
            "uq_notification_threads_identity",
            "uq_notification_threads_key",
        } <= uniques["notification_threads"]
        assert "uq_notification_routes_identity" in uniques["notification_routes"]
        assert _primary_key_name(connection, "notification_source_bindings") == (
            "pk_notification_source_bindings_identity"
        )
        assert "uq_notification_delivery_intents_identity" in uniques[
            "notification_delivery_intents"
        ]
        assert _primary_key_name(
            connection, "notification_reconciliation_cursors"
        ) == "pk_notification_reconciliation_cursors_identity"

        indexes = {
            table: {item["name"] for item in inspector.get_indexes(table)}
            for table in EXPECTED_TABLES
        }
        assert {
            "ix_notification_policy_states_due",
            "ix_notification_facts_history",
            "ix_notification_threads_inbox",
            "ix_notification_routes_active",
            "ix_notification_source_bindings_lookup",
            "ix_notification_delivery_intents_due",
            "ix_notification_delivery_intents_claimed",
            "ix_notification_delivery_attempts_claims",
            "ix_notification_reconciliation_cursors_processing",
        } <= set().union(*indexes.values())

        assert (
            ("channel_module_instance_id", "workspace_id"),
            "module_instances",
            "RESTRICT",
        ) in _foreign_keys(connection, "notification_routes")
        assert (
            ("notification_fact_id", "workspace_id"),
            "notification_facts",
            "RESTRICT",
        ) in _foreign_keys(connection, "notification_delivery_intents")


def test_r6_3_3_schema_has_named_status_and_timestamp_checks(
    database: Database,
) -> None:
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        checks = {
            table: {
                item["name"]
                for item in inspector.get_check_constraints(table)
                if item["name"] is not None
            }
            for table in EXPECTED_TABLES
        }
        assert {
            "ck_notification_policy_states_versions",
            "ck_notification_policy_states_pending_window",
        } <= checks["notification_policy_states"]
        assert {
            "ck_notification_facts_priority",
            "ck_notification_facts_transition",
            "ck_notification_facts_hashes",
            "ck_notification_facts_versions",
        } <= checks["notification_facts"]
        assert {
            "ck_notification_threads_business_status",
            "ck_notification_threads_organization_status",
            "ck_notification_threads_unread_count",
            "ck_notification_threads_versions",
            "ck_notification_threads_timestamps",
        } <= checks["notification_threads"]
        assert {
            "ck_notification_routes_status",
            "ck_notification_routes_source_ref",
            "ck_notification_routes_version",
        } <= checks["notification_routes"]
        assert "ck_notification_source_bindings_resource" in checks[
            "notification_source_bindings"
        ]
        assert {
            "ck_notification_delivery_intents_status",
            "ck_notification_delivery_intents_attempts",
            "ck_notification_delivery_intents_claim",
            "ck_notification_delivery_intents_terminal_timestamps",
        } <= checks["notification_delivery_intents"]
        assert {
            "ck_notification_delivery_attempts_settlement",
            "ck_notification_delivery_attempts_error",
        } <= checks["notification_delivery_attempts"]
        assert {
            "ck_notification_reconciliation_cursors_versions",
            "ck_notification_reconciliation_cursors_lease",
        } <= checks["notification_reconciliation_cursors"]


def test_r6_3_3_schema_survives_current_revision(database: Database) -> None:
    with database.engine.connect() as connection:
        version = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert version == "0027_r6_3_4_result_input_context"


def test_r6_3_3_migration_downgrade_is_forward_only() -> None:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "0025_r6_3_3_notification_gateway.py"
    )
    spec = importlib.util.spec_from_file_location("r6_3_3_migration", migration_path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with pytest.raises(RuntimeError, match="forward-only"):
        migration.downgrade()
