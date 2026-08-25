from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from sidera_control_plane.api.dependencies import get_work_context_query_service
from sidera_control_plane.api.schemas import (
    WorkContextDetailResponse,
    WorkContextRevisionResponse,
)
from sidera_control_plane.application.work_context_queries import (
    WorkContextQueryService,
)

router = APIRouter(prefix="/api/v1/workspaces")


@router.get(
    "/{workspace_id}/planning-requests/{planning_request_id}/work-context",
    response_model=WorkContextDetailResponse,
)
def get_work_context_by_planning_request(
    workspace_id: UUID,
    planning_request_id: UUID,
    service: Annotated[
        WorkContextQueryService, Depends(get_work_context_query_service)
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> WorkContextDetailResponse:
    return WorkContextDetailResponse.from_view(
        service.get_by_planning_request(
            workspace_id,
            planning_request_id,
            limit=limit,
            cursor=cursor,
        )
    )


@router.get(
    "/{workspace_id}/tasks/{root_task_id}/work-context",
    response_model=WorkContextDetailResponse,
)
def get_work_context_by_root_task(
    workspace_id: UUID,
    root_task_id: UUID,
    service: Annotated[
        WorkContextQueryService, Depends(get_work_context_query_service)
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> WorkContextDetailResponse:
    return WorkContextDetailResponse.from_view(
        service.get_by_root_task(
            workspace_id,
            root_task_id,
            limit=limit,
            cursor=cursor,
        )
    )


@router.get(
    "/{workspace_id}/work-contexts/{work_context_id}/revisions/{revision_id}",
    response_model=WorkContextRevisionResponse,
)
def get_work_context_revision(
    workspace_id: UUID,
    work_context_id: UUID,
    revision_id: UUID,
    service: Annotated[
        WorkContextQueryService, Depends(get_work_context_query_service)
    ],
) -> WorkContextRevisionResponse:
    return WorkContextRevisionResponse.from_revision_view(
        service.get_revision(workspace_id, work_context_id, revision_id)
    )
