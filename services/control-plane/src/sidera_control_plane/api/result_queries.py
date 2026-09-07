from dataclasses import asdict
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder

from sidera_control_plane.api.dependencies import get_result_query_service
from sidera_control_plane.api.schemas import (
    ResultReworkPageResponse,
    ResultVerificationPageResponse,
    TaskResultDetailResponse,
    TaskResultOverviewResponse,
)
from sidera_control_plane.application.result_queries import ResultQueryService

router = APIRouter()


@router.get(
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/results",
    response_model=TaskResultOverviewResponse,
    operation_id="get_task_result_overview",
)
def get_task_result_overview(
    workspace_id: UUID,
    root_task_id: UUID,
    service: Annotated[ResultQueryService, Depends(get_result_query_service)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    return jsonable_encoder(
        asdict(
            service.get_overview(
                workspace_id,
                root_task_id,
                cursor=cursor,
                limit=limit,
            )
        )
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/results/{candidate_id}",
    response_model=TaskResultDetailResponse,
    operation_id="get_task_result_detail",
)
def get_task_result_detail(
    workspace_id: UUID,
    root_task_id: UUID,
    candidate_id: UUID,
    service: Annotated[ResultQueryService, Depends(get_result_query_service)],
    verification_cursor: Annotated[str | None, Query()] = None,
    rework_cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    view = service.get_detail(
        workspace_id,
        root_task_id,
        candidate_id,
        verification_cursor=verification_cursor,
        rework_cursor=rework_cursor,
        limit=limit,
    )
    payload = asdict(view)
    payload["verifications"] = payload["verifications"]["items"]
    payload["rework"] = payload["rework"]["items"]
    return jsonable_encoder(payload)


@router.get(
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/results/{candidate_id}/verifications",
    response_model=ResultVerificationPageResponse,
    operation_id="list_task_result_verifications",
)
def list_task_result_verifications(
    workspace_id: UUID,
    root_task_id: UUID,
    candidate_id: UUID,
    service: Annotated[ResultQueryService, Depends(get_result_query_service)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    page = service.get_verifications(
        workspace_id,
        root_task_id,
        candidate_id,
        cursor=cursor,
        limit=limit,
    )
    return jsonable_encoder(
        {
            "candidate_id": candidate_id,
            "items": page.items,
            "next_cursor": page.next_cursor,
            "last_change_cursor": 0,
        }
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/results/{candidate_id}/rework",
    response_model=ResultReworkPageResponse,
    operation_id="list_task_result_rework",
)
def list_task_result_rework(
    workspace_id: UUID,
    root_task_id: UUID,
    candidate_id: UUID,
    service: Annotated[ResultQueryService, Depends(get_result_query_service)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    page = service.get_rework(
        workspace_id,
        root_task_id,
        candidate_id,
        cursor=cursor,
        limit=limit,
    )
    return jsonable_encoder(
        {
            "candidate_id": candidate_id,
            "items": page.items,
            "next_cursor": page.next_cursor,
            "last_change_cursor": 0,
        }
    )
