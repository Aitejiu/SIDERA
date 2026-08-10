from typing import Annotated

from fastapi import APIRouter, Depends, Response

from sidera_control_plane.api.dependencies import get_command_service
from sidera_control_plane.api.schemas import CommandRequest, CommandResponse
from sidera_control_plane.application.command_service import TaskCommandService

router = APIRouter(prefix="/api")


@router.post("/commands", response_model=CommandResponse, status_code=201)
def execute_command(
    request: CommandRequest,
    response: Response,
    service: Annotated[TaskCommandService, Depends(get_command_service)],
) -> CommandResponse:
    result = service.handle(request.to_envelope())
    is_new_create = (
        request.command_type == "CreateTask" and result.command_id == request.command_id
    )
    response.status_code = 201 if is_new_create else 200
    return CommandResponse.from_result(result)
