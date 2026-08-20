from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from sidera_control_plane.api.dependencies import get_resource_query_service
from sidera_control_plane.api.schemas import WorkerResponse
from sidera_control_plane.application.resource_queries import ResourceQueryService

router = APIRouter(prefix="/api/v1/workers")


@router.get("", response_model=tuple[WorkerResponse, ...])
def list_workers(
    workspace_id: UUID,
    service: Annotated[ResourceQueryService, Depends(get_resource_query_service)],
) -> tuple[WorkerResponse, ...]:
    return tuple(
        WorkerResponse.from_view(item) for item in service.list_workers(workspace_id)
    )


@router.get("/{worker_id}", response_model=WorkerResponse)
def get_worker(
    worker_id: UUID,
    workspace_id: UUID,
    service: Annotated[ResourceQueryService, Depends(get_resource_query_service)],
) -> WorkerResponse:
    return WorkerResponse.from_view(service.get_worker(workspace_id, worker_id))
