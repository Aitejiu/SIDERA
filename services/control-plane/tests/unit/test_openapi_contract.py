import json
from pathlib import Path

from sidera_control_plane.api.openapi_contract import export_openapi

EXPECTED_PATHS = {
    "/api/v1/workspaces/{workspace_id}/commands/{command_id}",
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
    "/api/v1/tasks/{root_task_id}/subtasks/{subtask_key}",
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/workspace",
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/activity",
    "/api/v1/workers",
    "/api/v1/workers/{worker_id}",
    "/api/v1/executions",
    "/api/v1/executions/{execution_id}",
    "/api/v1/modules",
}

EXPECTED_TYPED_RESPONSES = {
    "/api/v1/workspaces/{workspace_id}/commands/{command_id}": "CommandStatusResponse",
    "/api/v1/tasks/{root_task_id}/graph": "TaskConsoleGraphProjectionResponse",
    "/api/v1/tasks/{root_task_id}/work-items/{work_item_key}": "TaskDetailProjectionResponse",
    "/api/v1/tasks/{root_task_id}/activity": "TaskConsoleActivityProjectionResponse",
    "/api/v1/tasks/{root_task_id}/subtasks/{subtask_key}": "SubtaskWorkspaceProjectionResponse",
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/workspace": "WorkerWorkspaceProjectionResponse",
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/activity": "WorkerItemActivityPageResponse",
    "/api/v1/workers/{worker_id}": "WorkerResponse",
    "/api/v1/executions/{execution_id}": "ExecutionResponse",
}

EXPECTED_PLANNING_COMMAND_PAYLOADS = {
    "RegisterPlannerModule": "RegisterPlannerModulePayload",
    "RegisterPlanningProfile": "RegisterPlanningProfilePayload",
    "SubmitPlanningRequest": "SubmitPlanningRequestPayload",
    "CancelPlanningRequest": "CancelPlanningRequestPayload",
    "AnswerPlanningClarification": "AnswerPlanningClarificationPayload",
    "RequestProposalRevision": "RequestProposalRevisionPayload",
    "RejectPlanningProposal": "RejectPlanningProposalPayload",
    "RetryBlockedPlanningRun": "RetryBlockedPlanningRunPayload",
    "ConfirmPlanningProposal": "ConfirmPlanningProposalPayload",
    "ConfigureWorkerRuntime": "ConfigureWorkerRuntimePayload",
    "SetModuleLifecycle": "SetModuleLifecyclePayload",
    "PinExecutionWorker": "PinExecutionWorkerPayload",
    "ClearExecutionWorkerPin": "ClearExecutionWorkerPinPayload",
}

EXPECTED_PLANNING_RESPONSE_SCHEMAS = {
    "PlanningProfileResponse",
    "PlanningBriefResponse",
    "PlanningRunResponse",
    "PlanningClarificationResponse",
    "PlanningRequestSummaryResponse",
    "PlanningRequestDetailResponse",
    "PlanningProposalResponse",
    "PlanningMaterializationResponse",
}

EXPECTED_RUNTIME_RESPONSE_SCHEMAS = {
    "WorkerResponse",
    "ExecutionResponse",
    "ModuleSummaryResponse",
    "WorkerSummaryResponse",
    "ExecutionBindingSummaryResponse",
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

    assert first["components"]["schemas"]["ExecutionContextAddressPayload"][
        "required"
    ] == [
        "worker_item_id",
        "plan_generation",
        "context_version",
        "execution_id",
        "session_id",
        "session_version",
        "worker_id",
    ]
    graph_node_properties = first["components"]["schemas"][
        "SubtaskNodeProjectionResponse"
    ]["properties"]
    assert not {
        "work_item_key",
        "execution_id",
        "worker_display_name",
    } & graph_node_properties.keys()

    schemas = first["components"]["schemas"]
    payload_refs = first["x-sidera-command-payloads"]
    assert payload_refs == {
        command: {"$ref": f"#/components/schemas/{schema_name}"}
        for command, schema_name in EXPECTED_PLANNING_COMMAND_PAYLOADS.items()
    }
    for schema_name in EXPECTED_PLANNING_COMMAND_PAYLOADS.values():
        assert schema_name in schemas
        assert schemas[schema_name]["type"] == "object"
        assert schemas[schema_name]["additionalProperties"] is False

    assert EXPECTED_PLANNING_RESPONSE_SCHEMAS <= schemas.keys()
    for schema_name in EXPECTED_PLANNING_RESPONSE_SCHEMAS:
        assert schemas[schema_name]["type"] == "object"
        assert schemas[schema_name]["additionalProperties"] is False

    assert EXPECTED_RUNTIME_RESPONSE_SCHEMAS <= schemas.keys()
    assert (
        first["paths"]["/api/v1/modules"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]["type"]
        == "array"
    )

    command_request = schemas["CommandRequest"]
    assert command_request["properties"]["payload"] == {
        "additionalProperties": True,
        "title": "Payload",
        "type": "object",
    }
    command_response = schemas["CommandResponse"]
    assert "data" in command_response["properties"]
    assert command_response["properties"]["data"]["type"] == "object"
    command_status = schemas["CommandStatusResponse"]
    assert command_status["additionalProperties"] is False
    assert command_status["properties"]["status"]["enum"] == [
        "pending",
        "applied",
        "rejected",
    ]
    planning_request = schemas["PlanningRequestSummaryResponse"]
    assert {"source_type", "source_ref"} <= planning_request["properties"].keys()
    assert "source_type" in planning_request["required"]
    assert "source_ref" in planning_request["required"]

    error_response = schemas["ErrorResponse"]
    assert error_response["properties"]["error"] == {
        "$ref": "#/components/schemas/ErrorBody"
    }
    error_body = schemas["ErrorBody"]
    assert error_body["properties"]["code"]["type"] == "string"
    assert error_body["properties"]["message"]["type"] == "string"
    assert error_body["properties"]["details"]["type"] == "object"

    assert json.loads(first_bytes) == first


def test_committed_openapi_matches_current_export(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"
    exported = export_openapi(output)
    committed_path = Path(__file__).resolve().parents[4] / "apps/web/openapi.json"

    assert json.loads(committed_path.read_text(encoding="utf-8")) == exported
