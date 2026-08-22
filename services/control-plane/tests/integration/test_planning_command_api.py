from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ExecutionRequestModel,
    PlanSnapshotModel,
    TaskModel,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_command_response_keeps_legacy_fields_and_adds_data(
    client: TestClient, workspace_id: UUID
) -> None:
    response = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "RegisterPlannerModule",
            "target_type": "workspace",
            "actor": {"type": "local", "id": "operator"},
            "target_id": None,
            "expected_version": None,
            "idempotency_key": "planning-module-api-1",
            "correlation_id": str(uuid4()),
            "payload": {
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
                "instance_name": "api-planner",
                "config_namespace": "planning",
                "config": {"planning.mode": "test"},
                "secret_refs": [],
            },
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert {"command_id", "aggregate_id", "aggregate_version", "event_ids", "data"} <= body.keys()
    assert set(body["data"]) == {"module_definition_id", "module_instance_id"}


def _command(
    workspace_id: UUID,
    command_type: str,
    target_type: str,
    payload: dict[str, object],
    *,
    idempotency_key: str,
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


def test_planning_commands_persist_one_request_lifecycle_without_task_materialization(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    module_request = _command(
        workspace_id,
        "RegisterPlannerModule",
        "workspace",
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
            "instance_name": "lifecycle-planner",
            "config_namespace": "planning",
            "config": {"planning.mode": "test"},
            "secret_refs": [],
        },
        idempotency_key="planning-lifecycle-module",
    )
    module_created = client.post("/api/v1/commands", json=module_request)
    assert module_created.status_code == 201, module_created.text
    module_data = module_created.json()["data"]

    module_replay = client.post(
        "/api/v1/commands",
        json={**module_request, "command_id": str(uuid4()), "correlation_id": str(uuid4())},
    )
    assert module_replay.status_code == 200, module_replay.text
    assert module_replay.json() == module_created.json()

    profile_request = _command(
        workspace_id,
        "RegisterPlanningProfile",
        "workspace",
        {
            "profile_key": "default",
            "version": 1,
            "display_name": "Default planner",
            "planner_module_instance_id": module_data["module_instance_id"],
            "planner_module_config_version": 1,
            "planner_interface_version": "sidera.planner.v1",
            "prompt_policy_version": "prompt.v1",
            "system_policy": {"approval": {"required": True}},
            "proposal_schema_version": 2,
            "allowed_capabilities": ["code"],
            "adapter_preferences": ["opencode"],
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
        idempotency_key="planning-lifecycle-profile",
    )
    profile_created = client.post("/api/v1/commands", json=profile_request)
    assert profile_created.status_code == 201, profile_created.text
    profile_data = profile_created.json()["data"]

    submit_request = _command(
        workspace_id,
        "SubmitPlanningRequest",
        "workspace",
        {
            "profile_id": profile_data["planning_profile_id"],
            "profile_version": 1,
            "objective": "Implement planning",
            "constraints": ["Keep it reviewable"],
            "acceptance_criteria": ["The request is queued"],
            "context_refs": ["context:test"],
            "requested_outputs": ["proposal"],
            "source_type": "web",
            "source_ref": "conversation:test",
        },
        idempotency_key="planning-lifecycle-submit",
    )
    submitted = client.post("/api/v1/commands", json=submit_request)
    assert submitted.status_code == 201, submitted.text
    submit_data = submitted.json()["data"]
    assert submit_data["brief_revision"] == 1

    cancelled = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "CancelPlanningRequest",
            "planning_request",
            {},
            idempotency_key="planning-lifecycle-cancel",
            target_id=UUID(submit_data["planning_request_id"]),
            expected_version=1,
        ),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancelled"

    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(TaskModel)) == 0
        assert session.scalar(select(func.count()).select_from(PlanSnapshotModel)) == 0
        assert session.scalar(select(func.count()).select_from(ExecutionRequestModel)) == 0
