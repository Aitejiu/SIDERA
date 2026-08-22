from dataclasses import asdict
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder

from sidera_control_plane.api.dependencies import (
    get_worker_workspace_query_service,
)
from sidera_control_plane.api.schemas import (
    WorkerItemActivityPageResponse,
    WorkerWorkspaceProjectionResponse,
)
from sidera_control_plane.application.worker_workspace_queries import (
    WorkerWorkspaceQueryService,
)

router = APIRouter()


@router.get(
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/workspace",
    response_model=WorkerWorkspaceProjectionResponse,
    operation_id="get_worker_item_workspace",
)
def get_worker_item_workspace(
    workspace_id: UUID,
    worker_item_id: UUID,
    service: Annotated[
        WorkerWorkspaceQueryService, Depends(get_worker_workspace_query_service)
    ],
) -> dict[str, Any]:
    return jsonable_encoder(asdict(service.get_workspace(workspace_id, worker_item_id)))


@router.get(
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/activity",
    response_model=WorkerItemActivityPageResponse,
    operation_id="list_worker_item_activity",
)
def list_worker_item_activity(
    workspace_id: UUID,
    worker_item_id: UUID,
    service: Annotated[
        WorkerWorkspaceQueryService, Depends(get_worker_workspace_query_service)
    ],
    after: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return jsonable_encoder(
        asdict(
            service.list_activity(
                workspace_id,
                worker_item_id,
                after=after,
                limit=limit,
            )
        )
    )
