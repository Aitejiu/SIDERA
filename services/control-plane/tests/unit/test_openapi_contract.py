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
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/result-inputs",
    "/api/v1/workspaces/{workspace_id}/collaboration/input-selections/{selection_id}",
    "/api/v1/workers",
    "/api/v1/workers/{worker_id}",
    "/api/v1/executions",
    "/api/v1/executions/{execution_id}",
    "/api/v1/modules",
    "/api/v1/workspaces/{workspace_id}/planning-requests/{planning_request_id}/work-context",
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/work-context",
    "/api/v1/workspaces/{workspace_id}/work-contexts/{work_context_id}/revisions/{revision_id}",
    "/api/v1/workspaces/{workspace_id}/context-packages/{package_id}",
    "/api/v1/workspaces/{workspace_id}/planning-runs/{run_id}/context-package",
    "/api/v1/workspaces/{workspace_id}/executions/{execution_id}/context-package",
    "/api/v1/workspaces/{workspace_id}/planning-runs/{run_id}/knowledge-submissions",
    "/api/v1/workspaces/{workspace_id}/tasks/{task_id}/knowledge-submissions",
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/knowledge-submissions",
    "/api/v1/workspaces/{workspace_id}/context-packages/{package_id}/materialize",
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/plan-transitions",
    "/api/v1/workspaces/{workspace_id}/plan-transitions/{plan_transition_id}",
    "/api/v1/workspaces/{workspace_id}/worker-transitions/{worker_transition_id}",
    "/api/v1/workspaces/{workspace_id}/worker-transitions/{worker_transition_id}/reconcile",
    "/api/v1/workspaces/{workspace_id}/worker-transitions/{worker_transition_id}/confirm-stopped",
    "/api/v1/workspaces/{workspace_id}/worker-transitions/{worker_transition_id}/recover",
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/handoff-candidates",
    "/api/v1/workspaces/{workspace_id}/handoff-proposals/{proposal_id}",
    "/api/v1/workspaces/{workspace_id}/handoff-transitions/{transition_id}",
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/handoff",
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/collaboration",
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/collaboration/decisions",
    "/api/v1/workspaces/{workspace_id}/collaboration-governance/worker-items/{worker_item_id}",
    "/api/v1/workspaces/{workspace_id}/collaboration-governance/proposals/{proposal_id}",
    "/api/v1/workspaces/{workspace_id}/collaboration-metrics",
    "/api/v1/workspaces/{workspace_id}/notification-metrics",
    "/api/v1/workspaces/{workspace_id}/notification-readiness",
    "/api/v1/workspaces/{workspace_id}/notification-summary",
    "/api/v1/workspaces/{workspace_id}/notification-threads",
    "/api/v1/workspaces/{workspace_id}/notification-threads/{thread_id}",
    "/api/v1/workspaces/{workspace_id}/notification-deliveries/claims",
    "/api/v1/workspaces/{workspace_id}/notification-deliveries/{delivery_id}/ack",
    "/api/v1/workspaces/{workspace_id}/notification-deliveries/{delivery_id}/nack",
    "/api/v1/workspaces/{workspace_id}/notification-deliveries/{delivery_id}/attempts",
    "/api/v1/workspaces/{workspace_id}/notification-routes/{route_id}/threads",
}

EXPECTED_TYPED_RESPONSES = {
    "/api/v1/workspaces/{workspace_id}/commands/{command_id}": "CommandStatusResponse",
    "/api/v1/tasks/{root_task_id}/graph": "TaskConsoleGraphProjectionResponse",
    "/api/v1/tasks/{root_task_id}/work-items/{work_item_key}": "TaskDetailProjectionResponse",
    "/api/v1/tasks/{root_task_id}/activity": "TaskConsoleActivityProjectionResponse",
    "/api/v1/tasks/{root_task_id}/subtasks/{subtask_key}": "SubtaskWorkspaceProjectionResponse",
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/workspace": "WorkerWorkspaceProjectionResponse",
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/activity": "WorkerItemActivityPageResponse",
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/result-inputs": "WorkerResultInputsResponse",
    "/api/v1/workspaces/{workspace_id}/collaboration/input-selections/{selection_id}": "InputSelectionResponse",
    "/api/v1/workers/{worker_id}": "WorkerResponse",
    "/api/v1/executions/{execution_id}": "ExecutionResponse",
    "/api/v1/workspaces/{workspace_id}/planning-requests/{planning_request_id}/work-context": "WorkContextDetailResponse",
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/work-context": "WorkContextDetailResponse",
    "/api/v1/workspaces/{workspace_id}/work-contexts/{work_context_id}/revisions/{revision_id}": "WorkContextRevisionResponse",
    "/api/v1/workspaces/{workspace_id}/context-packages/{package_id}": "ContextPackageResponse",
    "/api/v1/workspaces/{workspace_id}/planning-runs/{run_id}/context-package": "ContextPackageResponse",
    "/api/v1/workspaces/{workspace_id}/executions/{execution_id}/context-package": "ContextPackageResponse",
    "/api/v1/workspaces/{workspace_id}/planning-runs/{run_id}/knowledge-submissions": "KnowledgeSubmissionPageResponse",
    "/api/v1/workspaces/{workspace_id}/tasks/{task_id}/knowledge-submissions": "KnowledgeSubmissionPageResponse",
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/knowledge-submissions": "KnowledgeSubmissionPageResponse",
    "/api/v1/workspaces/{workspace_id}/plan-transitions/{plan_transition_id}": "PlanTransitionResponse",
    "/api/v1/workspaces/{workspace_id}/worker-transitions/{worker_transition_id}": "WorkerTransitionResponse",
    "/api/v1/workspaces/{workspace_id}/handoff-proposals/{proposal_id}": "HandoffProposalResponse",
    "/api/v1/workspaces/{workspace_id}/handoff-transitions/{transition_id}": "HandoffTransitionResponse",
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/handoff": "ActiveHandoffProjectionResponse",
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/handoff-candidates": "HandoffCandidateListResponse",
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/collaboration": "CollaborationDetailResponse",
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/collaboration/decisions": "CollaborationDecisionPageResponse",
    "/api/v1/workspaces/{workspace_id}/collaboration-governance/worker-items/{worker_item_id}": "CollaborationGovernanceWorkerItemResponse",
    "/api/v1/workspaces/{workspace_id}/collaboration-governance/proposals/{proposal_id}": "CollaborationGovernanceProposalResponse",
    "/api/v1/workspaces/{workspace_id}/collaboration-metrics": "CollaborationMetricsResponse",
    "/api/v1/workspaces/{workspace_id}/notification-metrics": "NotificationMetricsResponse",
    "/api/v1/workspaces/{workspace_id}/notification-readiness": "NotificationReadinessResponse",
}

EXPECTED_TYPED_POST_RESPONSES = {
    "/api/v1/workspaces/{workspace_id}/context-packages/{package_id}/materialize": "ContextMaterializationResponse",
    "/api/v1/workspaces/{workspace_id}/worker-transitions/{worker_transition_id}/reconcile": "CommandResponse",
    "/api/v1/workspaces/{workspace_id}/worker-transitions/{worker_transition_id}/confirm-stopped": "CommandResponse",
    "/api/v1/workspaces/{workspace_id}/worker-transitions/{worker_transition_id}/recover": "CommandResponse",
    "/api/v1/workspaces/{workspace_id}/notification-deliveries/claims": "NotificationDeliveryClaimResponse",
    "/api/v1/workspaces/{workspace_id}/notification-deliveries/{delivery_id}/ack": "NotificationDeliverySettlementResponse",
    "/api/v1/workspaces/{workspace_id}/notification-deliveries/{delivery_id}/nack": "NotificationDeliverySettlementResponse",
}

EXPECTED_PLANNING_COMMAND_PAYLOADS = {
    "RegisterPlannerModule": "RegisterPlannerModulePayload",
    "RegisterCollaborationRuntimeModule": "RegisterCollaborationRuntimeModulePayload",
    "RegisterPlanningProfile": "RegisterPlanningProfilePayload",
    "SubmitPlanningRequest": "SubmitPlanningRequestPayload",
    "SubmitPlanRevision": "SubmitPlanRevisionPayload",
    "CancelPlanningRequest": "CancelPlanningRequestPayload",
    "AnswerPlanningClarification": "AnswerPlanningClarificationPayload",
    "RequestProposalRevision": "RequestProposalRevisionPayload",
    "RejectPlanningProposal": "RejectPlanningProposalPayload",
    "RetryBlockedPlanningRun": "RetryBlockedPlanningRunPayload",
    "ConfirmPlanningProposal": "ConfirmPlanningProposalPayload",
    "ConfirmPlanRevision": "ConfirmPlanRevisionPayload",
    "ReviseWorkContext": "ReviseWorkContextPayload",
    "AttachKnowledgeRevision": "AttachKnowledgeRevisionPayload",
    "ArchiveWorkContext": "ArchiveWorkContextPayload",
    "ConfigureWorkerRuntime": "ConfigureWorkerRuntimePayload",
    "SetModuleLifecycle": "SetModuleLifecyclePayload",
    "PinExecutionWorker": "PinExecutionWorkerPayload",
    "ClearExecutionWorkerPin": "ClearExecutionWorkerPinPayload",
    "RetryContextAssembly": "RetryContextAssemblyPayload",
    "RevokeContextPackage": "RevokeContextPackagePayload",
    "ReconcileWorkerTransition": "ReconcileWorkerTransitionPayload",
    "ConfirmWorkerTransitionStopped": "ConfirmWorkerTransitionStoppedPayload",
    "RecoverWorkerTransition": "RecoverWorkerTransitionPayload",
    "PrepareWorkerHandoff": "PrepareWorkerHandoffPayload",
    "ConfirmWorkerHandoff": "ConfirmWorkerHandoffPayload",
    "CancelWorkerHandoff": "CancelWorkerHandoffPayload",
    "RecoverWorkerHandoff": "RecoverWorkerHandoffPayload",
    "PauseCollaborationRelease": "PauseCollaborationReleasePayload",
    "ApproveCollaborationReleaseProposal": "ApproveCollaborationReleaseProposalPayload",
    "RejectCollaborationReleaseProposal": "RejectCollaborationReleaseProposalPayload",
    "ResumeAutomaticCollaboration": "ResumeAutomaticCollaborationPayload",
    "MarkNotificationThreadRead": "MarkNotificationThreadReadPayload",
    "ArchiveNotificationThread": "ArchiveNotificationThreadPayload",
    "RequeueNotificationDelivery": "RequeueNotificationDeliveryPayload",
    "RequestTaskRework": "RequestTaskReworkPayload",
    "AuthorizeAdditionalRework": "AuthorizeAdditionalReworkPayload",
    "RetryResultVerification": "RetryResultVerificationPayload",
    "ReintegrateTaskResult": "ReintegrateTaskResultPayload",
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

EXPECTED_WORK_CONTEXT_RESPONSE_SCHEMAS = {
    "WorkContextDetailResponse",
    "WorkContextRevisionResponse",
    "WorkContextSnapshotResponse",
}

EXPECTED_CONTEXT_PACKAGE_RESPONSE_SCHEMAS = {
    "ContextPackageResponse",
    "KnowledgeSubmissionPageResponse",
    "KnowledgeSubmissionResponse",
    "ContextMaterializationResponse",
}

EXPECTED_NOTIFICATION_RESPONSE_SCHEMAS = {
    "NotificationSummaryResponse",
    "NotificationThreadResponse",
    "NotificationThreadPageResponse",
    "NotificationThreadDetailResponse",
    "NotificationFactResponse",
    "NotificationFactPageResponse",
    "NotificationDeliveryResponse",
    "NotificationAttemptResponse",
    "NotificationAttemptPageResponse",
    "NotificationMetricSampleResponse",
    "NotificationMetricsResponse",
    "NotificationReadinessCheckResponse",
    "NotificationReadinessResponse",
}

EXPECTED_NOTIFICATION_WORKFLOW_SCHEMAS = {
    "NotificationDeliveryClaimRequest",
    "NotificationDeliveryClaimResponse",
    "NotificationDeliveryEnvelope",
    "NotificationDeliveryAckRequest",
    "NotificationDeliveryNackRequest",
    "NotificationDeliverySettlementResponse",
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

    for path, schema_name in EXPECTED_TYPED_POST_RESPONSES.items():
        response_schema = first["paths"][path]["post"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]
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

    approval_schema = schemas["ApproveCollaborationReleaseProposalPayload"]
    assert {
        "expected_input_selection_id",
        "expected_input_selection_hash",
    } <= approval_schema["properties"].keys()
    assert {
        "PlanningInputGroupResponse",
        "PlanningInputGroupCandidateResponse",
        "PlanningInputGroupRequirementsResponse",
    } <= schemas.keys()
    assert schemas["PlanningCollaborationResponse"]["properties"]["input_groups"] == {
        "items": {"$ref": "#/components/schemas/PlanningInputGroupResponse"},
        "title": "Input Groups",
        "type": "array",
    }

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
    assert EXPECTED_WORK_CONTEXT_RESPONSE_SCHEMAS <= schemas.keys()
    assert EXPECTED_CONTEXT_PACKAGE_RESPONSE_SCHEMAS <= schemas.keys()
    assert EXPECTED_NOTIFICATION_RESPONSE_SCHEMAS <= schemas.keys()
    assert EXPECTED_NOTIFICATION_WORKFLOW_SCHEMAS <= schemas.keys()
    for schema_name in EXPECTED_NOTIFICATION_RESPONSE_SCHEMAS:
        assert schemas[schema_name]["additionalProperties"] is False
    for schema_name in EXPECTED_NOTIFICATION_WORKFLOW_SCHEMAS:
        assert schemas[schema_name]["additionalProperties"] is False
    assert schemas["NotificationDeliveryClaimRequest"]["required"] == [
        "module_instance_id",
        "supported_contract_versions",
        "batch_size",
        "lease_seconds",
    ]
    assert schemas["NotificationDeliveryEnvelope"]["required"] == [
        "delivery_id",
        "notification_id",
        "thread_id",
        "contract_version",
        "claim_token",
        "fencing_token",
        "idempotency_key",
        "channel_type",
        "source_ref",
        "kind",
        "priority",
        "root_task_title",
        "subtask_path",
        "worker_item_title",
        "title",
        "summary",
        "next_action",
        "deep_link",
        "correlation_id",
        "notification_ref",
        "created_at",
    ]
    notification_schemas = " ".join(
        json.dumps(schemas[name], sort_keys=True)
        for name in EXPECTED_NOTIFICATION_RESPONSE_SCHEMAS
    )
    assert "source_ref" not in notification_schemas
    assert "claim_token" not in notification_schemas
    assert "provider_message_ref" not in notification_schemas

    command_request = schemas["CommandRequest"]
    assert command_request["properties"]["payload"] == {
        "additionalProperties": True,
        "title": "Payload",
        "type": "object",
    }
    assert {
        "notification_delivery",
        "notification_route",
        "notification_thread",
    } <= set(command_request["properties"]["target_type"]["enum"])
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
