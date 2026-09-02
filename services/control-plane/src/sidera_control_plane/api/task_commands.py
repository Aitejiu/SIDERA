from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from sidera_control_plane.api.dependencies import get_command_service, get_settings
from sidera_control_plane.api.schemas import CommandRequest, CommandResponse
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.handoff_commands import HandoffCommandError
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.errors import ValidationError

router = APIRouter(prefix="/api/v1")

_HANDOFF_COMMAND_TYPES = frozenset(
    {
        "PrepareWorkerHandoff",
        "ConfirmWorkerHandoff",
        "CancelWorkerHandoff",
        "RecoverWorkerHandoff",
    }
)


@router.post("/commands", response_model=CommandResponse, status_code=201)
def execute_command(
    request: CommandRequest,
    response: Response,
    service: Annotated[CommandService, Depends(get_command_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CommandResponse:
    return _execute_command(request, response, service, settings)


@router.post(
    "/workspaces/{workspace_id}/worker-transitions/{worker_transition_id}/reconcile",
    response_model=CommandResponse,
    operation_id="reconcile_worker_transition",
)
def reconcile_worker_transition(
    workspace_id: UUID,
    worker_transition_id: UUID,
    request: CommandRequest,
    response: Response,
    service: Annotated[CommandService, Depends(get_command_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CommandResponse:
    _require_transition_route(
        request,
        workspace_id=workspace_id,
        worker_transition_id=worker_transition_id,
        command_type="ReconcileWorkerTransition",
    )
    return _execute_command(request, response, service, settings)


@router.post(
    "/workspaces/{workspace_id}/worker-transitions/{worker_transition_id}/confirm-stopped",
    response_model=CommandResponse,
    operation_id="confirm_worker_transition_stopped",
)
def confirm_worker_transition_stopped(
    workspace_id: UUID,
    worker_transition_id: UUID,
    request: CommandRequest,
    response: Response,
    service: Annotated[CommandService, Depends(get_command_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CommandResponse:
    _require_transition_route(
        request,
        workspace_id=workspace_id,
        worker_transition_id=worker_transition_id,
        command_type="ConfirmWorkerTransitionStopped",
    )
    return _execute_command(request, response, service, settings)


@router.post(
    "/workspaces/{workspace_id}/worker-transitions/{worker_transition_id}/recover",
    response_model=CommandResponse,
    operation_id="recover_worker_transition",
)
def recover_worker_transition(
    workspace_id: UUID,
    worker_transition_id: UUID,
    request: CommandRequest,
    response: Response,
    service: Annotated[CommandService, Depends(get_command_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CommandResponse:
    _require_transition_route(
        request,
        workspace_id=workspace_id,
        worker_transition_id=worker_transition_id,
        command_type="RecoverWorkerTransition",
    )
    return _execute_command(request, response, service, settings)


def _execute_command(
    request: CommandRequest,
    response: Response,
    service: CommandService,
    settings: Settings,
) -> CommandResponse:
    if request.command_type in _HANDOFF_COMMAND_TYPES and (
        request.actor.type != settings.local_actor_type
        or request.actor.id != settings.local_actor_id
    ):
        raise HandoffCommandError(
            "handoff_actor_forbidden",
            "handoff commands require the configured local actor",
            details={"actor_type": settings.local_actor_type},
        )
    if (
        request.actor.type == settings.local_actor_type
        and request.actor.id != settings.local_actor_id
    ):
        raise ValidationError(
            "local actor identity does not match configured local identity",
            details={"actor_type": settings.local_actor_type},
        )
    result = service.handle(request.to_envelope())
    is_new_create = (
        request.command_type
        in {
            "CreateTask",
            "RegisterWorker",
            "CreateExecution",
            "RetryExecution",
            "RegisterPlannerModule",
            "RegisterCollaborationRuntimeModule",
            "RegisterPlanningProfile",
            "SubmitPlanningRequest",
            "SubmitPlanRevision",
            "ConfirmPlanningProposal",
            "ConfirmPlanRevision",
        }
        and result.command_id == request.command_id
    )
    is_new_create = is_new_create or (
        request.command_type == "ConfigureWorkerRuntime"
        and request.expected_version is None
        and result.command_id == request.command_id
    )
    response.status_code = (
        409
        if result.data.get("status") == "conflicted"
        else 201
        if is_new_create
        else 200
    )
    return CommandResponse.from_result(result)


def _require_transition_route(
    request: CommandRequest,
    *,
    workspace_id: UUID,
    worker_transition_id: UUID,
    command_type: str,
) -> None:
    if request.workspace_id != workspace_id:
        raise ValidationError(
            "worker transition route workspace does not match command workspace",
            details={"workspace_id": str(workspace_id)},
        )
    if request.command_type != command_type:
        raise ValidationError(
            "worker transition route does not match command type",
            details={"command_type": command_type},
        )
    if request.target_type != "worker_transition":
        raise ValidationError(
            "worker transition route requires target_type=worker_transition"
        )
    if request.target_id != worker_transition_id:
        raise ValidationError(
            "worker transition route target does not match command target",
            details={"worker_transition_id": str(worker_transition_id)},
        )
