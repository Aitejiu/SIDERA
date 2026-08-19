from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from sidera_control_plane.api.dependencies import get_task_query_service
from sidera_control_plane.api.schemas import (
    TaskPageResponse,
    TaskResponse,
    TimelineItemResponse,
)
from sidera_control_plane.application.task_queries import TaskQueryService

router = APIRouter(prefix="/api/v1/tasks")


@router.get("", response_model=TaskPageResponse)
def list_tasks(
    workspace_id: UUID,
    service: Annotated[TaskQueryService, Depends(get_task_query_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> TaskPageResponse:
    return TaskPageResponse.from_page(
        service.list_tasks(workspace_id, limit=limit, offset=offset)
    )


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: UUID,
    workspace_id: UUID,
    service: Annotated[TaskQueryService, Depends(get_task_query_service)],
) -> TaskResponse:
    return TaskResponse.from_view(service.get_task(workspace_id, task_id))


@router.get("/{task_id}/timeline", response_model=tuple[TimelineItemResponse, ...])
def get_task_timeline(
    task_id: UUID,
    workspace_id: UUID,
    service: Annotated[TaskQueryService, Depends(get_task_query_service)],
) -> tuple[TimelineItemResponse, ...]:
    return tuple(
        TimelineItemResponse.from_item(item)
        for item in service.get_timeline(workspace_id, task_id)
    )
