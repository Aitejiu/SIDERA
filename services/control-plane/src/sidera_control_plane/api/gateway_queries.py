from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from sidera_control_plane.api.dependencies import get_resource_query_service
from sidera_control_plane.api.schemas import (
    ArtifactMetadataResponse,
    ArtifactPageResponse,
    EvidenceClaimPageResponse,
    EvidenceClaimResponse,
    ExecutionBindingResponse,
    ExecutionEventPageResponse,
    ExecutionEventResponse,
    ExecutionInputPageResponse,
    ExecutionInputStatusResponse,
    ExecutionSessionPageResponse,
    ExecutionSessionResponse,
    ExecutionWorkspaceResponse,
)
from sidera_control_plane.application.resource_queries import ResourceQueryService

router = APIRouter(prefix="/api/v1/gateway")

WorkspaceQuery = Annotated[UUID, Query()]
CursorQuery = Annotated[int, Query(ge=0)]
LimitQuery = Annotated[int, Query(ge=1, le=100)]


@router.get(
    "/executions/{execution_id}/binding",
    response_model=ExecutionBindingResponse,
)
def get_execution_binding(
    execution_id: UUID,
    workspace_id: WorkspaceQuery,
    service: Annotated[ResourceQueryService, Depends(get_resource_query_service)],
) -> ExecutionBindingResponse:
    return ExecutionBindingResponse.from_view(
        service.get_execution_binding(workspace_id, execution_id)
    )


@router.get(
    "/executions/{execution_id}/workspace",
    response_model=ExecutionWorkspaceResponse,
)
def get_execution_workspace(
    execution_id: UUID,
    workspace_id: WorkspaceQuery,
    service: Annotated[ResourceQueryService, Depends(get_resource_query_service)],
) -> ExecutionWorkspaceResponse:
    return ExecutionWorkspaceResponse.from_view(
        service.get_execution_workspace(workspace_id, execution_id)
    )


@router.get("/sessions", response_model=ExecutionSessionPageResponse)
def list_execution_sessions(
    workspace_id: WorkspaceQuery,
    service: Annotated[ResourceQueryService, Depends(get_resource_query_service)],
    execution_id: UUID | None = None,
    session_id: UUID | None = None,
    status: str | None = Query(default=None, min_length=1),
    cursor: CursorQuery = 0,
    limit: LimitQuery = 50,
) -> ExecutionSessionPageResponse:
    page = service.list_execution_sessions(
        workspace_id,
        execution_id=execution_id,
        session_id=session_id,
        status=status,
        cursor=cursor,
        limit=limit,
    )
    return ExecutionSessionPageResponse(
        items=tuple(ExecutionSessionResponse.from_view(item) for item in page.items),
        next_cursor=page.next_cursor,
    )


@router.get("/events", response_model=ExecutionEventPageResponse)
def list_execution_events(
    workspace_id: WorkspaceQuery,
    service: Annotated[ResourceQueryService, Depends(get_resource_query_service)],
    execution_id: UUID | None = None,
    session_id: UUID | None = None,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    cursor: CursorQuery = 0,
    limit: LimitQuery = 100,
) -> ExecutionEventPageResponse:
    page = service.list_execution_events(
        workspace_id,
        execution_id=execution_id,
        session_id=session_id,
        after_sequence=after_sequence,
        cursor=cursor,
        limit=limit,
    )
    return ExecutionEventPageResponse(
        items=tuple(ExecutionEventResponse.from_view(item) for item in page.items),
        next_cursor=page.next_cursor,
    )


@router.get("/artifacts", response_model=ArtifactPageResponse)
def list_artifacts(
    workspace_id: WorkspaceQuery,
    service: Annotated[ResourceQueryService, Depends(get_resource_query_service)],
    task_id: UUID | None = None,
    execution_id: UUID | None = None,
    session_id: UUID | None = None,
    cursor: CursorQuery = 0,
    limit: LimitQuery = 50,
) -> ArtifactPageResponse:
    page = service.list_artifacts(
        workspace_id,
        task_id=task_id,
        execution_id=execution_id,
        session_id=session_id,
        cursor=cursor,
        limit=limit,
    )
    return ArtifactPageResponse(
        items=tuple(ArtifactMetadataResponse.from_view(item) for item in page.items),
        next_cursor=page.next_cursor,
    )


@router.get("/evidence", response_model=EvidenceClaimPageResponse)
def list_evidence_claims(
    workspace_id: WorkspaceQuery,
    service: Annotated[ResourceQueryService, Depends(get_resource_query_service)],
    task_id: UUID | None = None,
    execution_id: UUID | None = None,
    cursor: CursorQuery = 0,
    limit: LimitQuery = 50,
) -> EvidenceClaimPageResponse:
    page = service.list_evidence_claims(
        workspace_id,
        task_id=task_id,
        execution_id=execution_id,
        cursor=cursor,
        limit=limit,
    )
    return EvidenceClaimPageResponse(
        items=tuple(EvidenceClaimResponse.from_view(item) for item in page.items),
        next_cursor=page.next_cursor,
    )


@router.get("/inputs", response_model=ExecutionInputPageResponse)
def list_execution_inputs(
    workspace_id: WorkspaceQuery,
    service: Annotated[ResourceQueryService, Depends(get_resource_query_service)],
    execution_id: UUID | None = None,
    session_id: UUID | None = None,
    status: str | None = Query(default=None, min_length=1),
    cursor: CursorQuery = 0,
    limit: LimitQuery = 50,
) -> ExecutionInputPageResponse:
    page = service.list_execution_inputs(
        workspace_id,
        execution_id=execution_id,
        session_id=session_id,
        status=status,
        cursor=cursor,
        limit=limit,
    )
    return ExecutionInputPageResponse(
        items=tuple(
            ExecutionInputStatusResponse.from_view(item) for item in page.items
        ),
        next_cursor=page.next_cursor,
    )
