from __future__ import annotations

from typing import Any

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings


QUERY_CONTRACTS = {
    "/api/v1/workspaces/{workspace_id}/task-overviews": "TaskConsoleOverviewProjectionResponse",
    "/api/v1/tasks/{root_task_id}/overview": "TaskConsoleOverviewProjectionResponse",
    "/api/v1/tasks/{root_task_id}/graph": "TaskConsoleGraphProjectionResponse",
    "/api/v1/tasks/{root_task_id}/subtasks/{subtask_key}": "SubtaskWorkspaceProjectionResponse",
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/workspace": "WorkerWorkspaceProjectionResponse",
    "/api/v1/tasks/{root_task_id}/executions/{execution_id}/sessions/{session_id}/trace": "WorkerTraceResponse",
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/results": "TaskResultOverviewResponse",
    "/api/v1/workspaces/{workspace_id}/notification-threads": "NotificationThreadPageResponse",
    "/api/v1/workspaces/{workspace_id}/context-packages/{package_id}": "ContextPackageResponse",
}


def _openapi() -> dict[str, Any]:
    app = create_app(
        Settings(
            database_url="postgresql+psycopg://sidera@127.0.0.1:55432/sidera_test"
        )
    )
    return app.openapi()


def _schema_name(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    reference = value.get("$ref")
    if not isinstance(reference, str):
        items = value.get("items")
        return _schema_name(items)
    return reference.rsplit("/", maxsplit=1)[-1]


def test_task_first_query_contracts_are_typed_and_workspace_scoped() -> None:
    paths = _openapi()["paths"]
    for path, response_name in QUERY_CONTRACTS.items():
        operation = paths[path]["get"]
        response = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert _schema_name(response) == response_name, path
        parameters = operation.get("parameters", [])
        names = {item["name"] for item in parameters}
        if "{workspace_id}" not in path:
            assert "workspace_id" in names, path


def test_commands_and_context_materialization_have_explicit_write_contracts() -> None:
    paths = _openapi()["paths"]
    command = paths["/api/v1/commands"]["post"]
    assert command["requestBody"]["required"] is True
    assert _schema_name(command["responses"]["201"]["content"]["application/json"]["schema"]) == "CommandResponse"
    materialize = paths["/api/v1/workspaces/{workspace_id}/context-packages/{package_id}/materialize"]["post"]
    assert _schema_name(materialize["responses"]["200"]["content"]["application/json"]["schema"]) == "ContextMaterializationResponse"


def test_module_and_notification_contracts_keep_pagination_and_scope_visible() -> None:
    paths = _openapi()["paths"]
    modules = paths["/api/v1/modules"]["get"]
    assert "workspace_id" in {item["name"] for item in modules["parameters"]}
    assert _schema_name(modules["responses"]["200"]["content"]["application/json"]["schema"]) == "ModuleSummaryResponse"
    notifications = paths["/api/v1/workspaces/{workspace_id}/notification-threads"]["get"]
    names = {item["name"] for item in notifications["parameters"]}
    assert {"category", "cursor", "limit", "root_task_id", "subtask_id"}.issubset(names)
