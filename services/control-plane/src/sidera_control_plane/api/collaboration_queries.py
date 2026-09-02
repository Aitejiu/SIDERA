from dataclasses import asdict
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder

from sidera_control_plane.api.dependencies import get_collaboration_query_service
from sidera_control_plane.api.schemas import (
    CollaborationDecisionPageResponse,
    CollaborationDetailResponse,
)
from sidera_control_plane.application.collaboration_queries import (
    CollaborationQueryService,
)

router = APIRouter()


@router.get(
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/collaboration",
    response_model=CollaborationDetailResponse,
    operation_id="get_collaboration_detail",
)
def get_collaboration_detail(
    workspace_id: UUID,
    root_task_id: UUID,
    service: Annotated[
        CollaborationQueryService, Depends(get_collaboration_query_service)
    ],
) -> dict[str, Any]:
    return jsonable_encoder(asdict(service.get_detail(workspace_id, root_task_id)))


@router.get(
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/collaboration/decisions",
    response_model=CollaborationDecisionPageResponse,
    operation_id="list_collaboration_decisions",
)
def list_collaboration_decisions(
    workspace_id: UUID,
    root_task_id: UUID,
    service: Annotated[
        CollaborationQueryService, Depends(get_collaboration_query_service)
    ],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    return jsonable_encoder(
        asdict(
            service.list_decisions(
                workspace_id,
                root_task_id,
                cursor=cursor,
                limit=limit,
            )
        )
    )
