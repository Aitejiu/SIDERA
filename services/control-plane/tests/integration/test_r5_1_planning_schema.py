from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ModuleDefinitionModel,
    ModuleInstanceModel,
    PlanningBriefRevisionModel,
    PlanningProfileModel,
    PlanningRequestModel,
    WorkspaceModel,
)

pytestmark = pytest.mark.integration

WORKSPACE_A = UUID("00000000-0000-0000-0000-000000000001")
WORKSPACE_B = UUID("00000000-0000-0000-0000-000000000002")


def test_r5_1_planning_schema_has_workspace_constraints_and_active_run_gate(
    database: Database,
) -> None:
    expected_tables = {
        "planning_profiles",
        "planning_requests",
        "planning_brief_revisions",
        "planning_runs",
        "planning_run_leases",
        "planning_clarifications",
        "planning_proposal_revisions",
    }
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        assert expected_tables <= set(inspector.get_table_names())

        profile_uniques = {
            item["name"] for item in inspector.get_unique_constraints("planning_profiles")
        }
        run_uniques = {
            item["name"] for item in inspector.get_unique_constraints("planning_runs")
        }
        lease_indexes = {
            item["name"] for item in inspector.get_indexes("planning_run_leases")
        }
        request_checks = {
            item["name"] for item in inspector.get_check_constraints("planning_requests")
        }
        proposal_checks = {
            item["name"]
            for item in inspector.get_check_constraints("planning_proposal_revisions")
        }
        request_fks = {
            item["name"]: tuple(item["constrained_columns"])
            for item in inspector.get_foreign_keys("planning_requests")
        }

    assert "uq_planning_profiles_workspace_key_version" in profile_uniques
    assert "uq_planning_runs_operation_id" in run_uniques
    assert "uq_planning_runs_request_attempt" in run_uniques
    assert "uq_planning_run_leases_active_run" in lease_indexes
    assert "ck_planning_requests_status" in request_checks
    assert "ck_planning_requests_profile_snapshot_object" in request_checks
    assert "ck_planning_proposals_content_hash" in proposal_checks
    assert request_fks["fk_planning_requests_current_brief"] == (
        "id",
        "workspace_id",
        "current_brief_revision",
    )
    assert request_fks["fk_planning_requests_current_proposal"] == (
        "id",
        "workspace_id",
        "current_proposal_revision",
    )


def test_r5_1_planning_brief_cannot_cross_workspace_request(
    database: Database,
    workspace_id: UUID,
) -> None:
    profile_id = uuid4()
    module_definition_id = uuid4()
    module_instance_id = uuid4()
    request_id = uuid4()

    assert workspace_id == WORKSPACE_A
    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=WORKSPACE_B,
                name="Second Workspace",
                status="active",
                version=1,
            )
        )
        session.add(
            ModuleDefinitionModel(
                id=module_definition_id,
                workspace_id=WORKSPACE_A,
                module_type="planner_adapter",
                provider="test",
                interface_version="sidera.planner.v1",
                config_schema_version=1,
                capabilities={},
                lifecycle="active",
            )
        )
        session.add(
            ModuleInstanceModel(
                id=module_instance_id,
                workspace_id=WORKSPACE_A,
                definition_id=module_definition_id,
                instance_name="planner",
                config_version=1,
                config_namespace="planning",
                config={},
                secret_refs=[],
                lifecycle="active",
            )
        )
        session.add(
            PlanningProfileModel(
                id=profile_id,
                workspace_id=WORKSPACE_A,
                profile_key="default",
                version=1,
                display_name="Default planner",
                planner_module_instance_id=module_instance_id,
                planner_module_config_version=1,
                planner_interface_version="sidera.planner.v1",
                status="active",
                user_selectable=True,
                policy={"proposal_schema_version": 2},
            )
        )
        session.add(
            PlanningRequestModel(
                id=request_id,
                workspace_id=WORKSPACE_A,
                kind="create",
                status="submitted",
                current_brief_revision=None,
                current_proposal_revision=None,
                current_proposal_content_hash=None,
                planning_profile_id=profile_id,
                planning_profile_version=1,
                profile_snapshot={"profile_version": 1},
                source_type="test",
                source_ref="test:r5.1",
                actor="integration",
                correlation_id=uuid4(),
                version=1,
            )
        )
        session.commit()

    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            PlanningBriefRevisionModel(
                planning_request_id=request_id,
                revision=1,
                workspace_id=WORKSPACE_B,
                payload={"objective": "must be rejected"},
                created_by="integration",
            )
        )
        session.commit()
