from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from sqlalchemy import inspect, text

from sidera_control_plane.persistence.database import Database

pytestmark = pytest.mark.integration


EXPECTED_TABLES = {
    "work_contexts",
    "work_context_revisions",
    "context_assembly_requests",
    "context_packages",
    "context_package_knowledge_refs",
    "context_package_acknowledgements",
    "context_package_materialization_audits",
    "knowledge_candidate_submissions",
}


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


def _foreign_keys(
    connection, table_name: str
) -> set[tuple[tuple[str, ...], str, str | None]]:
    return {
        (
            tuple(item["constrained_columns"]),
            item["referred_table"],
            item.get("options", {}).get("ondelete"),
        )
        for item in inspect(connection).get_foreign_keys(table_name)
    }


def _column_names(columns: Sequence[Mapping[str, Any]]) -> set[str]:
    return {str(column["name"]) for column in columns}


def test_r5_3_schema_creates_context_package_and_candidate_tables(
    database: Database,
) -> None:
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        assert EXPECTED_TABLES <= set(inspector.get_table_names())

        expected_columns = {
            "work_contexts": {
                "id",
                "workspace_id",
                "planning_request_id",
                "root_task_id",
                "status",
                "current_revision_id",
                "current_revision_number",
                "version",
                "created_at",
                "updated_at",
            },
            "work_context_revisions": {
                "id",
                "work_context_id",
                "workspace_id",
                "revision_number",
                "brief_planning_request_id",
                "brief_revision",
                "proposal_revision_id",
                "plan_snapshot_id",
                "reason",
                "snapshot",
                "content_hash",
                "created_by",
                "created_at",
            },
            "context_assembly_requests": {
                "id",
                "workspace_id",
                "purpose",
                "consumer_type",
                "consumer_id",
                "worker_item_id",
                "work_context_revision_id",
                "context_version",
                "knowledge_search_scope",
                "knowledge_filter_refs",
                "fact_scope_refs",
                "budget",
                "expires_at",
                "status",
                "lease_owner",
                "lease_expires_at",
                "attempt",
                "next_attempt_at",
                "error_code",
                "request_fingerprint",
                "idempotency_key",
                "version",
                "created_at",
                "updated_at",
            },
            "context_packages": {
                "id",
                "workspace_id",
                "assembly_request_id",
                "purpose",
                "consumer_type",
                "consumer_id",
                "worker_item_id",
                "work_context_revision_id",
                "context_version",
                "control_plane_fact_snapshot",
                "artifact_refs",
                "evidence_refs",
                "budget",
                "expires_at",
                "package_hash",
                "status",
                "revoked_at",
                "revocation_reason",
                "revocation_scope",
                "idempotency_key",
                "created_at",
                "materialized_at",
            },
            "context_package_knowledge_refs": {
                "package_id",
                "ordinal",
                "page_id",
                "revision_id",
                "revision_hash",
                "section_key",
                "section_hash",
                "source_ids",
                "selection_reason",
                "trust_level",
            },
            "context_package_acknowledgements": {
                "package_id",
                "consumer_type",
                "consumer_id",
                "context_version",
                "acknowledged_package_hash",
                "adapter_module_instance_id",
                "adapter_config_version",
                "acknowledged_at",
                "created_at",
            },
            "context_package_materialization_audits": {
                "id",
                "package_id",
                "consumer_type",
                "consumer_id",
                "context_version",
                "attempt",
                "outcome",
                "section_count",
                "byte_count",
                "input_token_count",
                "failure_code",
                "occurred_at",
            },
            "knowledge_candidate_submissions": {
                "id",
                "workspace_id",
                "producer_type",
                "producer_id",
                "planning_run_id",
                "execution_id",
                "worker_item_id",
                "task_id",
                "subtask_task_id",
                "artifact_refs",
                "evidence_refs",
                "proposal_hash",
                "content_ref",
                "content_bytes",
                "knowledge_command_id",
                "status",
                "attempt",
                "lease_owner",
                "lease_expires_at",
                "next_attempt_at",
                "terminal_candidate_id",
                "error_code",
                "version",
                "created_at",
                "updated_at",
            },
        }
        for table_name, columns in expected_columns.items():
            assert columns <= _column_names(inspector.get_columns(table_name))

        forbidden = {"markdown", "body", "knowledge_snapshot"}
        for table_name in EXPECTED_TABLES:
            assert not forbidden & _column_names(inspector.get_columns(table_name))


def test_r5_3_schema_has_immutable_refs_tenant_safe_fks_and_operational_checks(
    database: Database,
) -> None:
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        context_uniques = {
            item["name"] for item in inspector.get_unique_constraints("work_contexts")
        }
        package_uniques = {
            item["name"]
            for item in inspector.get_unique_constraints("context_packages")
        }
        assembly_uniques = {
            item["name"]
            for item in inspector.get_unique_constraints("context_assembly_requests")
        }
        revision_uniques = {
            item["name"]
            for item in inspector.get_unique_constraints("work_context_revisions")
        }

        assert {
            "uq_work_contexts_planning_request",
            "uq_work_contexts_identity",
        } <= context_uniques
        assert "uq_context_packages_purpose_consumer" in package_uniques
        assert "uq_context_assembly_requests_purpose_consumer" in assembly_uniques
        assert "uq_work_context_revisions_context_revision" in revision_uniques

        assert (
            ("workspace_id",),
            "workspaces",
            "RESTRICT",
        ) in _foreign_keys(connection, "work_contexts")
        assert (
            ("planning_request_id", "workspace_id"),
            "planning_requests",
            "RESTRICT",
        ) in _foreign_keys(connection, "work_contexts")
        assert (
            ("root_task_id", "workspace_id"),
            "tasks",
            "RESTRICT",
        ) in _foreign_keys(connection, "work_contexts")
        assert (
            ("brief_planning_request_id", "workspace_id", "brief_revision"),
            "planning_brief_revisions",
            "RESTRICT",
        ) in _foreign_keys(connection, "work_context_revisions")
        assert (
            ("proposal_revision_id", "workspace_id"),
            "planning_proposal_revisions",
            "RESTRICT",
        ) in _foreign_keys(connection, "work_context_revisions")
        assert (
            ("plan_snapshot_id", "workspace_id"),
            "task_plan_snapshots",
            "RESTRICT",
        ) in _foreign_keys(connection, "work_context_revisions")

        for table_name in (
            "context_assembly_requests",
            "context_packages",
            "context_package_knowledge_refs",
            "context_package_acknowledgements",
            "context_package_materialization_audits",
            "knowledge_candidate_submissions",
        ):
            assert all(
                item[2] == "RESTRICT"
                for item in _foreign_keys(connection, table_name)
            )

        checks = {
            table_name: {
                item["name"]
                for item in inspector.get_check_constraints(table_name)
            }
            for table_name in EXPECTED_TABLES
        }
        assert {
            "ck_work_contexts_status",
            "ck_work_contexts_version",
            "ck_work_contexts_current_revision_number",
        } <= checks["work_contexts"]
        assert {
            "ck_work_context_revisions_snapshot_object",
            "ck_work_context_revisions_content_hash",
            "ck_work_context_revisions_reason",
        } <= checks["work_context_revisions"]
        assert {
            "ck_context_assembly_requests_status",
            "ck_context_assembly_requests_budget_object",
            "ck_context_assembly_requests_attempt",
            "ck_context_assembly_requests_fingerprint",
        } <= checks["context_assembly_requests"]
        assert {
            "ck_context_packages_status",
            "ck_context_packages_manifest_hash",
            "ck_context_packages_fact_snapshot_object",
            "ck_context_packages_budget_object",
            "ck_context_packages_refs_array",
        } <= checks["context_packages"]
        assert {
            "ck_knowledge_candidate_submissions_status",
            "ck_knowledge_candidate_submissions_proposal_hash",
            "ck_knowledge_candidate_submissions_artifacts_array",
            "ck_knowledge_candidate_submissions_evidence_array",
        } <= checks["knowledge_candidate_submissions"]


def test_r5_3_schema_uses_one_head(database: Database) -> None:
    with database.engine.connect() as connection:
        version = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert version == "0018_r6_1_execution_request_lineage"
