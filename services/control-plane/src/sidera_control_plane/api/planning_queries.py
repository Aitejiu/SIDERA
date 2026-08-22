from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from sidera_control_plane.api.dependencies import (
    get_planning_metrics_service,
    get_planning_query_service,
)
from sidera_control_plane.api.schemas import (
    PlanningBriefPageResponse,
    PlanningClarificationPageResponse,
    PlanningMaterializationResponse,
    PlanningMetricsResponse,
    PlanningProfilePageResponse,
    PlanningProfileResponse,
    PlanningProposalResponse,
    PlanningRequestDetailResponse,
    PlanningRequestPageResponse,
    PlanningRequestSummaryResponse,
    PlanningRunPageResponse,
)
from sidera_control_plane.application.planning_metrics import PlanningMetricsService
from sidera_control_plane.application.planning_queries import PlanningQueryService

router = APIRouter(prefix="/api/v1/workspaces")


@router.get("/{workspace_id}/planning-profiles", response_model=PlanningProfilePageResponse)
def list_planning_profiles(
    workspace_id: UUID,
    service: Annotated[PlanningQueryService, Depends(get_planning_query_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> PlanningProfilePageResponse:
    page = service.list_profiles(workspace_id, limit=limit, cursor=cursor)
    return PlanningProfilePageResponse(
        items=tuple(PlanningProfileResponse.from_view(view) for view in page.items),
        next_cursor=page.next_cursor,
    )


@router.get("/{workspace_id}/planning-requests", response_model=PlanningRequestPageResponse)
def list_planning_requests(
    workspace_id: UUID,
    service: Annotated[PlanningQueryService, Depends(get_planning_query_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    closed_status: Annotated[bool | None, Query()] = None,
    requires_attention: Annotated[bool, Query()] = False,
) -> PlanningRequestPageResponse:
    page = service.list_requests(
        workspace_id,
        limit=limit,
        cursor=cursor,
        status=status,
        closed_status=closed_status,
        requires_attention=requires_attention,
    )
    return PlanningRequestPageResponse(
        items=tuple(PlanningRequestSummaryResponse.from_view(item) for item in page.items),
        next_cursor=page.next_cursor,
    )


@router.get(
    "/{workspace_id}/planning-requests/{request_id}",
    response_model=PlanningRequestDetailResponse,
)
def get_planning_request(
    workspace_id: UUID,
    request_id: UUID,
    service: Annotated[PlanningQueryService, Depends(get_planning_query_service)],
) -> PlanningRequestDetailResponse:
    return PlanningRequestDetailResponse.from_detail_view(
        service.get_request_detail(workspace_id, request_id)
    )


@router.get(
    "/{workspace_id}/planning-requests/{request_id}/briefs",
    response_model=PlanningBriefPageResponse,
)
def list_planning_briefs(
    workspace_id: UUID,
    request_id: UUID,
    service: Annotated[PlanningQueryService, Depends(get_planning_query_service)],
) -> PlanningBriefPageResponse:
    from sidera_control_plane.api.schemas import PlanningBriefResponse

    return PlanningBriefPageResponse(
        items=tuple(
            PlanningBriefResponse.from_view(item)
            for item in service.list_briefs(workspace_id, request_id)
        )
    )


@router.get(
    "/{workspace_id}/planning-requests/{request_id}/runs",
    response_model=PlanningRunPageResponse,
)
def list_planning_runs(
    workspace_id: UUID,
    request_id: UUID,
    service: Annotated[PlanningQueryService, Depends(get_planning_query_service)],
) -> PlanningRunPageResponse:
    from sidera_control_plane.api.schemas import PlanningRunResponse

    return PlanningRunPageResponse(
        items=tuple(
            PlanningRunResponse.from_view(item)
            for item in service.list_runs(workspace_id, request_id)
        )
    )


@router.get(
    "/{workspace_id}/planning-requests/{request_id}/clarifications",
    response_model=PlanningClarificationPageResponse,
)
def list_planning_clarifications(
    workspace_id: UUID,
    request_id: UUID,
    service: Annotated[PlanningQueryService, Depends(get_planning_query_service)],
) -> PlanningClarificationPageResponse:
    from sidera_control_plane.api.schemas import PlanningClarificationResponse

    return PlanningClarificationPageResponse(
        items=tuple(
            PlanningClarificationResponse.from_view(item)
            for item in service.list_clarifications(workspace_id, request_id)
        )
    )


@router.get(
    "/{workspace_id}/planning-requests/{request_id}/proposals/{revision}",
    response_model=PlanningProposalResponse,
)
def get_planning_proposal(
    workspace_id: UUID,
    request_id: UUID,
    revision: Annotated[int, Path(ge=1)],
    service: Annotated[PlanningQueryService, Depends(get_planning_query_service)],
) -> PlanningProposalResponse:
    return PlanningProposalResponse.from_view(
        service.get_proposal(workspace_id, request_id, revision)
    )


@router.get(
    "/{workspace_id}/planning-requests/{request_id}/materialization",
    response_model=PlanningMaterializationResponse,
)
def get_planning_materialization(
    workspace_id: UUID,
    request_id: UUID,
    service: Annotated[PlanningQueryService, Depends(get_planning_query_service)],
) -> PlanningMaterializationResponse:
    return PlanningMaterializationResponse.from_view(
        service.get_materialization(workspace_id, request_id)
    )


@router.get("/{workspace_id}/planning-metrics", response_model=PlanningMetricsResponse)
def get_planning_metrics(
    workspace_id: UUID,
    service: Annotated[PlanningMetricsService, Depends(get_planning_metrics_service)],
) -> PlanningMetricsResponse:
    return PlanningMetricsResponse.from_view(service.get_metrics(workspace_id))
