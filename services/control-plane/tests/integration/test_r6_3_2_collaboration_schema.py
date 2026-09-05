import pytest
from sqlalchemy import inspect, text

from sidera_control_plane.persistence.database import Database

pytestmark = pytest.mark.integration


def test_r6_3_2_schema_adds_governance_ledger_and_deferred_status(
    database: Database,
) -> None:
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        expected = {
            "collaboration_governance_holds": {
                "id", "workspace_id", "root_task_id", "run_id", "plan_snapshot_id",
                "plan_generation", "plan_content_hash", "worker_item_id", "status",
                "created_by_actor_id", "created_via", "reason", "resource_version",
                "created_at", "resolved_at",
            },
            "collaboration_release_proposals": {
                "id", "workspace_id", "hold_id", "run_id", "projection_snapshot_id",
                "projection_hash", "decision_id", "intent_id", "execution_request_id",
                "target_request_version", "input_fingerprint_version", "input_fingerprint",
                "status", "reason_code", "summary", "resource_version", "created_at",
                "resolved_at",
            },
            "collaboration_governance_resolutions": {
                "id", "workspace_id", "proposal_id", "action", "result", "actor_id",
                "transport", "reason", "command_id", "created_at",
            },
        }
        for table, columns in expected.items():
            assert columns <= {column["name"] for column in inspector.get_columns(table)}

        version = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        assert version == "0026_r6_3_4_result_inputs"


def test_r6_3_2_schema_has_named_governance_constraints(database: Database) -> None:
    with database.engine.connect() as connection:
        for table, names in {
            "collaboration_governance_holds": {
                "uq_collaboration_governance_holds_identity",
                "ck_collaboration_governance_holds_status",
            },
            "collaboration_release_proposals": {
                "uq_collaboration_release_proposals_identity",
                "ck_collaboration_release_proposals_status",
            },
            "collaboration_governance_resolutions": {
                "uq_collaboration_governance_resolutions_identity",
            },
        }.items():
            actual = {
                item["name"]
                for item in inspect(connection).get_unique_constraints(table)
            }
            actual |= {
                item["name"]
                for item in inspect(connection).get_check_constraints(table)
            }
            assert names <= actual
