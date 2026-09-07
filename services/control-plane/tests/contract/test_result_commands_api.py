from __future__ import annotations

from sidera_control_plane.api.app import create_app
from sidera_control_plane.api.schemas import CommandRequest
from sidera_control_plane.config import Settings


def test_result_query_paths_and_response_schema_names_are_public() -> None:
    app = create_app(Settings(database_url="postgresql+psycopg://unused/unused"))
    document = app.openapi()
    paths = document["paths"]
    schemas = document["components"]["schemas"]

    assert "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/results" in paths
    assert (
        "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/results/{candidate_id}"
        in paths
    )
    assert (
        "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/results/{candidate_id}/verifications"
        in paths
    )
    assert (
        "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/results/{candidate_id}/rework"
        in paths
    )
    assert "TaskResultOverviewResponse" in schemas
    assert "TaskResultDetailResponse" in schemas


def test_result_acceptance_uses_the_existing_complete_task_command_contract() -> None:
    request = CommandRequest.model_validate(
        {
            "command_id": "00000000-0000-0000-0000-000000000001",
            "workspace_id": "00000000-0000-0000-0000-000000000002",
            "command_type": "CompleteTask",
            "target_type": "task",
            "actor": {"type": "local", "id": "operator"},
            "target_id": "00000000-0000-0000-0000-000000000003",
            "expected_version": 2,
            "idempotency_key": "result-contract-1",
            "correlation_id": "00000000-0000-0000-0000-000000000004",
            "payload": {
                "candidate_id": "00000000-0000-0000-0000-000000000005",
                "candidate_hash": "a" * 64,
                "acceptance_basis_hash": "b" * 64,
                "expected_result_version": 3,
                "criteria": [
                    {
                        "criterion_key": "criterion:one",
                        "decision": "accept_passed",
                        "reason": None,
                    }
                ],
            },
        }
    )

    assert request.command_type == "CompleteTask"
    assert request.payload["expected_result_version"] == 3
