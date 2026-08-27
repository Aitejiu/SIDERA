from typing import Annotated

from fastapi import APIRouter, Depends, Response

from sidera_control_plane.api.dependencies import get_command_service, get_settings
from sidera_control_plane.api.schemas import CommandRequest, CommandResponse
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.errors import ValidationError

router = APIRouter(prefix="/api/v1")


@router.post("/commands", response_model=CommandResponse, status_code=201)
def execute_command(
    request: CommandRequest,
    response: Response,
    service: Annotated[CommandService, Depends(get_command_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CommandResponse:
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
