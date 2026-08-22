from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from sidera_control_plane.api.dependencies import get_resource_query_service
from sidera_control_plane.api.schemas import ExecutionResponse, LeaseResponse
from sidera_control_plane.application.resource_queries import ResourceQueryService

router = APIRouter(prefix="/api/v1/executions")


@router.get("", response_model=tuple[ExecutionResponse, ...])
def list_executions(
    workspace_id: UUID,
    service: Annotated[ResourceQueryService, Depends(get_resource_query_service)],
    task_id: UUID | None = None,
    status: str | None = Query(default=None, min_length=1),
) -> tuple[ExecutionResponse, ...]:
    return tuple(
        ExecutionResponse.from_view(item)
        for item in service.list_executions(workspace_id, task_id, status)
    )


@router.get("/{execution_id}", response_model=ExecutionResponse)
def get_execution(
    execution_id: UUID,
    workspace_id: UUID,
    service: Annotated[ResourceQueryService, Depends(get_resource_query_service)],
) -> ExecutionResponse:
    return ExecutionResponse.from_view(
        service.get_execution(workspace_id, execution_id)
    )


@router.get("/{execution_id}/leases", response_model=tuple[LeaseResponse, ...])
def list_leases(
    execution_id: UUID,
    workspace_id: UUID,
    service: Annotated[ResourceQueryService, Depends(get_resource_query_service)],
) -> tuple[LeaseResponse, ...]:
    return tuple(
        LeaseResponse.from_view(item)
        for item in service.list_leases(workspace_id, execution_id)
    )
