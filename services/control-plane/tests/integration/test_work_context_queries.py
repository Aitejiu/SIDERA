from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import TaskModel, WorkContextModel

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _command(
    workspace_id: UUID,
    command_type: str,
    payload: dict[str, object],
    *,
    idempotency_key: str,
    target_type: str = "workspace",
    target_id: UUID | None = None,
    expected_version: int | None = None,
) -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": target_type,
        "actor": {"type": "local", "id": "operator"},
        "target_id": str(target_id) if target_id is not None else None,
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def _create_context(
    client: TestClient, workspace_id: UUID, suffix: str
) -> dict[str, object]:
    module = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "RegisterPlannerModule",
            {
                "provider": "fake",
                "interface_version": "sidera.planner.v1",
                "config_schema_version": 1,
                "capabilities": {
                    "structured_output": "json_schema",
                    "clarification": True,
                    "idempotency": "operation-id",
                    "cancellation": "run",
                    "usage_reporting": "tokens",
                    "max_context_bytes": 128000,
                    "stability": "stable",
                    "interface_version": "sidera.planner.v1",
                    "proposal_schema_versions": [2],
                },
                "instance_name": f"query-{suffix}",
                "config_namespace": "planning",
                "config": {},
                "secret_refs": [],
            },
            idempotency_key=f"query-module-{suffix}",
        ),
    )
    assert module.status_code == 201, module.text
    profile = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "RegisterPlanningProfile",
            {
                "profile_key": f"query-{suffix}",
                "version": 1,
                "display_name": "Query planner",
                "planner_module_instance_id": module.json()["data"][
                    "module_instance_id"
                ],
                "planner_module_config_version": 1,
                "planner_interface_version": "sidera.planner.v1",
                "prompt_policy_version": "prompt.v1",
                "system_policy": {},
                "proposal_schema_version": 2,
                "allowed_capabilities": ["code"],
                "adapter_preferences": ["fake"],
                "adapter_forbidden": [],
                "budget": {
                    "max_context_bytes": 64000,
                    "max_tokens": 10000,
                    "max_cost_microunits": 1000000,
                    "max_runtime_seconds": 600,
                    "max_revisions": 3,
                    "max_nodes": 20,
                    "max_concurrency": 2,
                },
                "retry_policy": {
                    "max_retries": 1,
                    "backoff_seconds": [5],
                    "unknown_outcome_policy": "block",
                    "invalid_output_policy": "retry",
                },
                "default_priority": 50,
                "execution_max_attempts": 2,
                "execution_budget_level": "standard",
                "user_selectable": True,
                "planner_secret_refs": [],
            },
            idempotency_key=f"query-profile-{suffix}",
        ),
    )
    assert profile.status_code == 201, profile.text
    submitted = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "SubmitPlanningRequest",
            {
                "profile_id": profile.json()["data"]["planning_profile_id"],
                "profile_version": 1,
                "objective": "Expose immutable work context history",
                "constraints": ["No raw conversation body"],
                "acceptance_criteria": ["History is cursorable"],
                "context_refs": [],
                "requested_outputs": ["typed context detail"],
                "source_type": "web",
                "source_ref": "query-test",
            },
            idempotency_key=f"query-submit-{suffix}",
        ),
    )
    assert submitted.status_code == 201, submitted.text
    return submitted.json()["data"]


def test_work_context_detail_and_revision_queries_are_cursorable(
    client: TestClient, workspace_id: UUID
) -> None:
    data = _create_context(client, workspace_id, "timeline")
    context_id = UUID(data["work_context_id"])
    first_detail = client.get(
        f"/api/v1/workspaces/{workspace_id}/planning-requests/"
        f"{data['planning_request_id']}/work-context"
    )

    assert first_detail.status_code == 200, first_detail.text
    first_payload = first_detail.json()
    assert first_payload["id"] == str(context_id)
    assert first_payload["status"] == "active"
    assert first_payload["current_revision"]["revision_number"] == 1
    assert first_payload["current_revision"]["snapshot"]["objective"] == (
        "Expose immutable work context history"
    )
    assert first_payload["current_revision"]["snapshot"]["knowledge_refs"] == []
    assert first_payload["timeline"][0]["revision_number"] == 1

    revised = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "ReviseWorkContext",
            {
                "base_revision": 1,
                "base_content_hash": data["work_context_content_hash"],
                "reason": "user_intervention",
                "patch": {"objective": "Expose cursorable context history"},
            },
            idempotency_key="query-revision",
            target_type="work_context",
            target_id=context_id,
            expected_version=1,
        ),
    )
    assert revised.status_code == 200, revised.text

    page = client.get(
        f"/api/v1/workspaces/{workspace_id}/planning-requests/"
        f"{data['planning_request_id']}/work-context",
        params={"limit": 1},
    )
    assert page.status_code == 200, page.text
    page_payload = page.json()
    assert [item["revision_number"] for item in page_payload["timeline"]] == [1]
    assert page_payload["next_cursor"]

    next_page = client.get(
        f"/api/v1/workspaces/{workspace_id}/planning-requests/"
        f"{data['planning_request_id']}/work-context",
        params={"limit": 1, "cursor": page_payload["next_cursor"]},
    )
    assert next_page.status_code == 200, next_page.text
    assert [item["revision_number"] for item in next_page.json()["timeline"]] == [2]
    assert next_page.json()["next_cursor"] is None
    assert next_page.json()["current_revision"]["snapshot"]["objective"] == (
        "Expose cursorable context history"
    )


def test_work_context_root_and_revision_routes_are_workspace_scoped(
    client: TestClient, database: Database, workspace_id: UUID
) -> None:
    data = _create_context(client, workspace_id, "root")
    context_id = UUID(data["work_context_id"])
    root_task_id = uuid4()
    with database.session() as session:
        session.add(
            TaskModel(
                id=root_task_id,
                workspace_id=workspace_id,
                title="Query root task",
                description="Query root task",
                acceptance_criteria=["context is linked"],
                status="running",
                priority=50,
                requested_by="operator",
                version=1,
            )
        )
        session.execute(
            update(WorkContextModel)
            .where(WorkContextModel.id == context_id)
            .values(root_task_id=root_task_id)
        )
        session.commit()

    root_response = client.get(
        f"/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/work-context"
    )
    assert root_response.status_code == 200, root_response.text
    revision_id = root_response.json()["current_revision"]["id"]
    revision_response = client.get(
        f"/api/v1/workspaces/{workspace_id}/work-contexts/{context_id}/"
        f"revisions/{revision_id}"
    )
    assert revision_response.status_code == 200, revision_response.text
    snapshot = revision_response.json()["snapshot"]
    assert "source_ref" not in snapshot
    assert "body" not in snapshot
    assert snapshot["knowledge_refs"] == []

    other_workspace = uuid4()
    assert (
        client.get(
            f"/api/v1/workspaces/{other_workspace}/work-contexts/{context_id}/"
            f"revisions/{revision_id}"
        ).status_code
        == 404
    )
