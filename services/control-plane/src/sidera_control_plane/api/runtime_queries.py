from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from sidera_control_plane.api.dependencies import get_runtime_metrics_service
from sidera_control_plane.api.schemas import RuntimeMetricsResponse
from sidera_control_plane.application.runtime_metrics import RuntimeMetricsService

router = APIRouter(prefix="/api/v1/workspaces")


@router.get("/{workspace_id}/runtime-metrics", response_model=RuntimeMetricsResponse)
def get_runtime_metrics(
    workspace_id: UUID,
    service: Annotated[RuntimeMetricsService, Depends(get_runtime_metrics_service)],
) -> RuntimeMetricsResponse:
    return RuntimeMetricsResponse.from_view(service.get_metrics(workspace_id))
