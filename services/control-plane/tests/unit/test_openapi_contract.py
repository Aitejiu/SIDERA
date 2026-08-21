import json
from pathlib import Path

from sidera_control_plane.api.openapi_contract import export_openapi

EXPECTED_PATHS = {
    "/api/v1/workspaces/{workspace_id}/task-overviews",
    "/api/v1/tasks/{root_task_id}/overview",
    "/api/v1/tasks/{root_task_id}/graph",
    "/api/v1/tasks/{root_task_id}/work-items/{work_item_key}",
    "/api/v1/tasks/{root_task_id}/activity",
    "/api/v1/tasks/{root_task_id}/plan-diff",
    "/api/v1/workspaces/{workspace_id}/capabilities",
    "/api/v1/workspaces/{workspace_id}/projection-changes",
    "/api/v1/tasks/{root_task_id}/executions/{execution_id}/sessions/{session_id}/trace",
    "/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/descriptor",
    "/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/content",
    "/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/download-token",
}

EXPECTED_TYPED_RESPONSES = {
    "/api/v1/tasks/{root_task_id}/graph": "TaskGraphProjectionResponse",
    "/api/v1/tasks/{root_task_id}/work-items/{work_item_key}": "TaskDetailProjectionResponse",
    "/api/v1/tasks/{root_task_id}/activity": "TaskActivityProjectionResponse",
}


def test_openapi_export_is_canonical_and_contains_r4_contract(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "openapi.json"

    first = export_openapi(output)
    first_bytes = output.read_bytes()
    second = export_openapi(output)

    assert first == second
    assert first_bytes == output.read_bytes()
    assert first_bytes.endswith(b"\n")
    assert set(first["paths"]) >= EXPECTED_PATHS
    assert "Model" not in first_bytes.decode("utf-8")

    for path, schema_name in EXPECTED_TYPED_RESPONSES.items():
        response_schema = first["paths"][path]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert response_schema == {"$ref": f"#/components/schemas/{schema_name}"}
        assert schema_name in first["components"]["schemas"]

    for path_item in first["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete", "options"}:
                continue
            assert operation["operationId"]

    assert json.loads(first_bytes) == first
