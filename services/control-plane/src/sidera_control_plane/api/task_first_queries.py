from dataclasses import asdict
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder

from sidera_control_plane.api.dependencies import get_task_first_query_service
from sidera_control_plane.application.task_first_queries import TaskFirstQueryService

router = APIRouter()


@router.get("/api/v1/workspaces/{workspace_id}/task-overviews")
def list_task_overviews(
    workspace_id: UUID,
    service: Annotated[TaskFirstQueryService, Depends(get_task_first_query_service)],
) -> list[dict[str, Any]]:
    return [_encode(item) for item in service.list_overviews(workspace_id)]


@router.get("/api/v1/tasks/{root_task_id}/overview")
def get_task_overview(
    root_task_id: UUID,
    workspace_id: UUID,
    service: Annotated[TaskFirstQueryService, Depends(get_task_first_query_service)],
) -> dict[str, Any]:
    return _encode(service.get_overview(workspace_id, root_task_id))


@router.get("/api/v1/tasks/{root_task_id}/graph")
def get_task_graph(
    root_task_id: UUID,
    workspace_id: UUID,
    service: Annotated[TaskFirstQueryService, Depends(get_task_first_query_service)],
) -> dict[str, Any]:
    return _encode(service.get_graph(workspace_id, root_task_id))


@router.get("/api/v1/tasks/{root_task_id}/work-items/{work_item_key}")
def get_task_detail(
    root_task_id: UUID,
    work_item_key: str,
    workspace_id: UUID,
    service: Annotated[TaskFirstQueryService, Depends(get_task_first_query_service)],
) -> dict[str, Any]:
    return _encode(service.get_detail(workspace_id, root_task_id, work_item_key))


@router.get("/api/v1/tasks/{root_task_id}/activity")
def get_task_activity(
    root_task_id: UUID,
    workspace_id: UUID,
    service: Annotated[TaskFirstQueryService, Depends(get_task_first_query_service)],
    after: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return _encode(
        service.get_activity(
            workspace_id,
            root_task_id,
            after=after,
            limit=limit,
        )
    )


@router.get("/api/v1/tasks/{root_task_id}/plan-diff")
def get_plan_diff(
    root_task_id: UUID,
    workspace_id: UUID,
    left: Annotated[int, Query(ge=1)],
    right: Annotated[int, Query(ge=1)],
    service: Annotated[TaskFirstQueryService, Depends(get_task_first_query_service)],
) -> dict[str, Any]:
    return _encode(
        service.get_plan_diff(
            workspace_id,
            root_task_id,
            left_generation=left,
            right_generation=right,
        )
    )


@router.get("/api/v1/workspaces/{workspace_id}/capabilities")
def get_workspace_capabilities(
    workspace_id: UUID,
    service: Annotated[TaskFirstQueryService, Depends(get_task_first_query_service)],
) -> dict[str, Any]:
    del workspace_id
    return _encode(service.get_capabilities())


def _encode(value: Any) -> dict[str, Any]:
    return jsonable_encoder(asdict(value))
