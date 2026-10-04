import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from sidera_control_plane.api.app import create_app
from sidera_control_plane.api.schemas import (
    AnswerPlanningClarificationPayload,
    ApproveCollaborationReleaseProposalPayload,
    ArchiveNotificationThreadPayload,
    ArchiveWorkContextPayload,
    AttachKnowledgeRevisionPayload,
    AuthorizeAdditionalReworkPayload,
    CancelPlanningRequestPayload,
    CancelWorkerHandoffPayload,
    ClearExecutionWorkerPinPayload,
    CollaborationMetricsResponse,
    ConfigureWorkerRuntimePayload,
    ConfirmPlanningProposalPayload,
    ConfirmPlanRevisionPayload,
    ConfirmProfileHandoffPayload,
    ConfirmWorkerHandoffPayload,
    ConfirmWorkerTransitionStoppedPayload,
    ErrorBody,
    ErrorResponse,
    ExecutionContextAddressPayload,
    MarkNotificationThreadReadPayload,
    NotificationDeliveryAckRequest,
    NotificationDeliveryClaimRequest,
    NotificationDeliveryClaimResponse,
    NotificationDeliveryEnvelope,
    NotificationDeliveryNackRequest,
    NotificationDeliverySettlementResponse,
    NotificationMetricSampleResponse,
    NotificationMetricsResponse,
    NotificationReadinessCheckResponse,
    NotificationReadinessResponse,
    PauseCollaborationReleasePayload,
    PinExecutionWorkerPayload,
    PrepareProfileHandoffPayload,
    PrepareWorkerHandoffPayload,
    ReconcileWorkerTransitionPayload,
    RecoverWorkerHandoffPayload,
    RecoverWorkerTransitionPayload,
    RegisterCollaborationRuntimeModulePayload,
    RegisterPlannerModulePayload,
    RegisterPlanningProfilePayload,
    ReintegrateTaskResultPayload,
    RejectCollaborationReleaseProposalPayload,
    RejectPlanningProposalPayload,
    RequestProposalRevisionPayload,
    RequestTaskReworkPayload,
    RequeueNotificationDeliveryPayload,
    ResumeAutomaticCollaborationPayload,
    RetryBlockedPlanningRunPayload,
    RetryContextAssemblyPayload,
    RetryResultVerificationPayload,
    ReviseWorkContextPayload,
    RevokeContextPackagePayload,
    SendExecutionInputPayload,
    SetAgentAssigneePayload,
    SetModuleLifecyclePayload,
    SubmitPlanningRequestPayload,
    SubmitPlanRevisionPayload,
    SubmitTaskGoalPayload,
    UpdateUnstartedAgentBindingPayload,
)
from sidera_control_plane.config import Settings

_PLANNING_COMMAND_PAYLOADS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("RegisterPlannerModule", RegisterPlannerModulePayload),
    (
        "RegisterCollaborationRuntimeModule",
        RegisterCollaborationRuntimeModulePayload,
    ),
    ("RegisterPlanningProfile", RegisterPlanningProfilePayload),
    ("SubmitPlanningRequest", SubmitPlanningRequestPayload),
    ("SubmitTaskGoal", SubmitTaskGoalPayload),
    ("SubmitPlanRevision", SubmitPlanRevisionPayload),
    ("CancelPlanningRequest", CancelPlanningRequestPayload),
    ("AnswerPlanningClarification", AnswerPlanningClarificationPayload),
    ("RequestProposalRevision", RequestProposalRevisionPayload),
    ("RejectPlanningProposal", RejectPlanningProposalPayload),
    ("RetryBlockedPlanningRun", RetryBlockedPlanningRunPayload),
    ("ConfirmPlanningProposal", ConfirmPlanningProposalPayload),
    ("ConfirmPlanRevision", ConfirmPlanRevisionPayload),
    ("ReviseWorkContext", ReviseWorkContextPayload),
    ("AttachKnowledgeRevision", AttachKnowledgeRevisionPayload),
    ("ArchiveWorkContext", ArchiveWorkContextPayload),
    ("RetryContextAssembly", RetryContextAssemblyPayload),
    ("RevokeContextPackage", RevokeContextPackagePayload),
)

_RUNTIME_COMMAND_PAYLOADS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("SendExecutionInput", SendExecutionInputPayload),
    ("SetSubtaskAssignee", SetAgentAssigneePayload),
    ("SetWorkerItemAssignee", SetAgentAssigneePayload),
    ("UpdateUnstartedAgentBinding", UpdateUnstartedAgentBindingPayload),
    ("ConfigureWorkerRuntime", ConfigureWorkerRuntimePayload),
    ("SetModuleLifecycle", SetModuleLifecyclePayload),
    ("PinExecutionWorker", PinExecutionWorkerPayload),
    ("ClearExecutionWorkerPin", ClearExecutionWorkerPinPayload),
)

_TRANSITION_COMMAND_PAYLOADS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("ReconcileWorkerTransition", ReconcileWorkerTransitionPayload),
    ("ConfirmWorkerTransitionStopped", ConfirmWorkerTransitionStoppedPayload),
    ("RecoverWorkerTransition", RecoverWorkerTransitionPayload),
)

_HANDOFF_COMMAND_PAYLOADS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("PrepareProfileHandoff", PrepareProfileHandoffPayload),
    ("ConfirmProfileHandoff", ConfirmProfileHandoffPayload),
    ("PrepareWorkerHandoff", PrepareWorkerHandoffPayload),
    ("ConfirmWorkerHandoff", ConfirmWorkerHandoffPayload),
    ("CancelWorkerHandoff", CancelWorkerHandoffPayload),
    ("RecoverWorkerHandoff", RecoverWorkerHandoffPayload),
)

_GOVERNANCE_COMMAND_PAYLOADS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("PauseCollaborationRelease", PauseCollaborationReleasePayload),
    (
        "ApproveCollaborationReleaseProposal",
        ApproveCollaborationReleaseProposalPayload,
    ),
    (
        "RejectCollaborationReleaseProposal",
        RejectCollaborationReleaseProposalPayload,
    ),
    ("ResumeAutomaticCollaboration", ResumeAutomaticCollaborationPayload),
)

_NOTIFICATION_COMMAND_PAYLOADS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("MarkNotificationThreadRead", MarkNotificationThreadReadPayload),
    ("ArchiveNotificationThread", ArchiveNotificationThreadPayload),
    ("RequeueNotificationDelivery", RequeueNotificationDeliveryPayload),
)

_RESULT_COMMAND_PAYLOADS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("RequestTaskRework", RequestTaskReworkPayload),
    ("AuthorizeAdditionalRework", AuthorizeAdditionalReworkPayload),
    ("RetryResultVerification", RetryResultVerificationPayload),
    ("ReintegrateTaskResult", ReintegrateTaskResultPayload),
)

_COMMAND_PAYLOADS = (
    _PLANNING_COMMAND_PAYLOADS
    + _RUNTIME_COMMAND_PAYLOADS
    + _TRANSITION_COMMAND_PAYLOADS
    + _HANDOFF_COMMAND_PAYLOADS
    + _GOVERNANCE_COMMAND_PAYLOADS
    + _NOTIFICATION_COMMAND_PAYLOADS
    + _RESULT_COMMAND_PAYLOADS
)

_NOTIFICATION_WORKFLOW_SCHEMAS: tuple[type[BaseModel], ...] = (
    NotificationDeliveryClaimRequest,
    NotificationDeliveryClaimResponse,
    NotificationDeliveryEnvelope,
    NotificationDeliveryAckRequest,
    NotificationDeliveryNackRequest,
    NotificationDeliverySettlementResponse,
    NotificationMetricSampleResponse,
    NotificationMetricsResponse,
    NotificationReadinessCheckResponse,
    NotificationReadinessResponse,
)


def export_openapi(output: Path) -> dict[str, Any]:
    settings = Settings(
        database_url=("postgresql+psycopg://openapi:openapi@127.0.0.1:1/openapi")
    )
    document = create_app(settings).openapi()
    schemas = document.setdefault("components", {}).setdefault("schemas", {})
    _add_component_schema(schemas, ExecutionContextAddressPayload)
    _add_component_schema(schemas, ErrorBody)
    _add_component_schema(schemas, ErrorResponse)
    _add_component_schema(schemas, CollaborationMetricsResponse)
    for _command_name, model in _COMMAND_PAYLOADS:
        _add_component_schema(schemas, model)
    for model in _NOTIFICATION_WORKFLOW_SCHEMAS:
        _add_component_schema(schemas, model)
    document["x-sidera-command-payloads"] = {
        command_name: {
            "$ref": f"#/components/schemas/{model.__name__}",
        }
        for command_name, model in _COMMAND_PAYLOADS
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    return document


def _add_component_schema(schemas: dict[str, Any], model: type[BaseModel]) -> None:
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    definitions = schema.pop("$defs", {})
    for name, definition in definitions.items():
        schemas.setdefault(name, definition)
    schemas[model.__name__] = schema


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the SIDERA OpenAPI contract")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export_openapi(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
