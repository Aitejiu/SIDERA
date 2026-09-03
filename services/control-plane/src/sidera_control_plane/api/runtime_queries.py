from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from sidera_control_plane.api.dependencies import (
    get_database,
    get_runtime_metrics_service,
    get_settings,
)
from sidera_control_plane.api.schemas import (
    CollaborationMetricsResponse,
    CollaborationReadinessResponse,
    RuntimeMetricsResponse,
)
from sidera_control_plane.application.runtime_metrics import RuntimeMetricsService
from sidera_control_plane.collaboration_readiness import build_collaboration_readiness
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database

router = APIRouter(prefix="/api/v1")


@router.get(
    "/workspaces/{workspace_id}/runtime-metrics",
    response_model=RuntimeMetricsResponse,
)
def get_runtime_metrics(
    workspace_id: UUID,
    service: Annotated[RuntimeMetricsService, Depends(get_runtime_metrics_service)],
) -> RuntimeMetricsResponse:
    return RuntimeMetricsResponse.from_view(service.get_metrics(workspace_id))


@router.get(
    "/workspaces/{workspace_id}/collaboration-metrics",
    response_model=CollaborationMetricsResponse,
)
def get_collaboration_metrics(
    workspace_id: UUID,
    service: Annotated[RuntimeMetricsService, Depends(get_runtime_metrics_service)],
) -> CollaborationMetricsResponse:
    return CollaborationMetricsResponse.from_view(
        service.get_collaboration_metrics(workspace_id)
    )


@router.get(
    "/collaboration-readiness",
    response_model=CollaborationReadinessResponse,
)
def get_collaboration_readiness(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CollaborationReadinessResponse:
    report = build_collaboration_readiness(settings, database).check()
    return CollaborationReadinessResponse.from_report(report)
