from dataclasses import asdict
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder

from sidera_control_plane.api.dependencies import (
    get_subtask_workspace_query_service,
)
from sidera_control_plane.api.schemas import SubtaskWorkspaceProjectionResponse
from sidera_control_plane.application.subtask_workspace_queries import (
    SubtaskWorkspaceQueryService,
)

router = APIRouter()


@router.get(
    "/api/v1/tasks/{root_task_id}/subtasks/{subtask_key}",
    response_model=SubtaskWorkspaceProjectionResponse,
    operation_id="get_subtask_workspace",
)
def get_subtask_workspace(
    root_task_id: UUID,
    subtask_key: str,
    workspace_id: UUID,
    service: Annotated[
        SubtaskWorkspaceQueryService, Depends(get_subtask_workspace_query_service)
    ],
) -> dict[str, Any]:
    return jsonable_encoder(
        asdict(service.get(workspace_id, root_task_id, subtask_key))
    )
