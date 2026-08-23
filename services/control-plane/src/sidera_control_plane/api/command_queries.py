from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from sidera_control_plane.api.dependencies import get_command_query_service
from sidera_control_plane.api.schemas import CommandStatusResponse
from sidera_control_plane.application.command_queries import CommandQueryService

router = APIRouter(prefix="/api/v1/workspaces")


@router.get(
    "/{workspace_id}/commands/{command_id}",
    response_model=CommandStatusResponse,
)
def get_command_status(
    workspace_id: UUID,
    command_id: UUID,
    service: Annotated[CommandQueryService, Depends(get_command_query_service)],
) -> CommandStatusResponse:
    return CommandStatusResponse.from_view(
        service.get_status(workspace_id, command_id)
    )
