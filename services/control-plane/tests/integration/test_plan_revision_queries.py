from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.models import PlanningRequestModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.integration.test_revision_confirmation import _seed_revision_confirmation

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as test_client:
        yield test_client


def test_revision_queries_expose_base_diff_paths_policy_and_active_head(
    client: TestClient,
    database,
    workspace_id: UUID,
) -> None:
    seed = _seed_revision_confirmation(database, workspace_id)
    base = f"/api/v1/workspaces/{workspace_id}"

    detail = client.get(f"{base}/planning-requests/{seed['request'].id}")
    diff = client.get(f"{base}/planning-requests/{seed['request'].id}/diff")
    active_plan = client.get(f"{base}/tasks/{seed['root_task'].id}/active-plan")

    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert detail_body["revision_base"]["plan_snapshot_id"] == str(seed["snapshot"].id)
    assert detail_body["revision_base"]["plan_generation"] == 1
    assert detail_body["revision_freshness"]["base_fresh"] is True
    assert detail_body["revision_freshness"]["stale_reasons"] == []

    assert diff.status_code == 200, diff.text
    diff_body = diff.json()
    assert diff_body["planning_request_id"] == str(seed["request"].id)
    assert diff_body["proposal_revision_id"] == str(seed["proposal"].id)
    assert diff_body["diff_hash"] == seed["proposal"].derived_diff.diff_hash
    assert diff_body["base"]["plan_snapshot_id"] == str(seed["snapshot"].id)
    assert diff_body["root_changes"][0]["field_path"] == "goal_summary"
    assert diff_body["subtask_changes"][0]["path"] == (
        "Task/Running control room > Subtask/Implementation"
    )
    assert diff_body["worker_item_changes"][0]["path"] == (
        "Task/Running control room > Subtask/Implementation > WorkerItem/Backend worker"
    )
    assert diff_body["impact_counts"]["patch"] == 1
    assert diff_body["transition_preview"][0]["allowed_actions"] == [
        "patch",
        "continue",
    ]
    assert diff_body["accepted_transition_policy"] == {
        "activate_immediately": True,
        "budget_level": "standard",
        "failure_policy": "block_affected_branch",
        "manual_gate": False,
        "revision_operation": "revise",
    }
    assert diff_body["collaboration"] == {
        "mode": "disabled",
        "default_strategy": None,
        "subtask_overrides": [],
        "provider": None,
        "runtime_interface": None,
        "runtime_config_version": None,
    }
    assert diff_body["collaboration_policy_changes"] == []
    assert "patch_body" not in diff_body

    assert active_plan.status_code == 200, active_plan.text
    active_body = active_plan.json()
    assert active_body["active_snapshot_id"] == str(seed["snapshot"].id)
    assert active_body["active_generation"] == 1
    assert active_body["snapshot"]["id"] == str(seed["snapshot"].id)
    assert active_body["snapshot"]["root_task_id"] == str(seed["root_task"].id)


def test_stale_revision_queries_are_read_only_and_report_reasons(
    client: TestClient,
    database,
    workspace_id: UUID,
) -> None:
    seed = _seed_revision_confirmation(database, workspace_id)
    stale_snapshot = replace(
        seed["snapshot"],
        id=UUID("10000000-0000-0000-0000-000000000002"),
        generation=2,
        parent_snapshot_id=seed["snapshot"].id,
        source_ref="integration:unrelated-generation",
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.plans.add_snapshot_and_advance_head(
            stale_snapshot,
            expected_head=uow.plans.get_head(
                seed["root_task"].id, workspace_id, for_update=True
            ),
        )

    with database.session() as session:
        before = session.execute(
            select(PlanningRequestModel.status, PlanningRequestModel.version).where(
                PlanningRequestModel.id == seed["request"].id,
                PlanningRequestModel.workspace_id == workspace_id,
            )
        ).one()

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/planning-requests/{seed['request'].id}"
    )

    assert response.status_code == 200, response.text
    freshness = response.json()["revision_freshness"]
    assert freshness["base_fresh"] is False
    assert "active_plan_changed" in freshness["stale_reasons"]

    with database.session() as session:
        after = session.execute(
            select(PlanningRequestModel.status, PlanningRequestModel.version).where(
                PlanningRequestModel.id == seed["request"].id,
                PlanningRequestModel.workspace_id == workspace_id,
            )
        ).one()
    assert after == before


def test_revision_query_endpoints_enforce_workspace_isolation(
    client: TestClient,
    database,
    workspace_id: UUID,
) -> None:
    seed = _seed_revision_confirmation(database, workspace_id)
    other_workspace = UUID("00000000-0000-0000-0000-000000000099")

    diff = client.get(
        f"/api/v1/workspaces/{other_workspace}/planning-requests/"
        f"{seed['request'].id}/diff"
    )
    active_plan = client.get(
        f"/api/v1/workspaces/{other_workspace}/tasks/{seed['root_task'].id}/active-plan"
    )

    assert diff.status_code == 404
    assert diff.json()["error"]["code"] == "not_found"
    assert active_plan.status_code == 404
    assert active_plan.json()["error"]["code"] == "not_found"
