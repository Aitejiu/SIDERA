from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Response

from sidera_control_plane.api.dependencies import (
    get_context_package_query_service,
)
from sidera_control_plane.api.schemas import (
    ContextMaterializationResponse,
    ContextPackageResponse,
    KnowledgeSubmissionPageResponse,
)
from sidera_control_plane.application.context_package_queries import (
    ContextPackageQueryService,
)
from sidera_control_plane.domain.context_packages import ContextPurpose

router = APIRouter(prefix="/api/v1/workspaces")


@router.get(
    "/{workspace_id}/context-packages/{package_id}",
    response_model=ContextPackageResponse,
    operation_id="get_context_package",
)
def get_context_package(
    workspace_id: UUID,
    package_id: UUID,
    service: Annotated[
        ContextPackageQueryService, Depends(get_context_package_query_service)
    ],
) -> ContextPackageResponse:
    return ContextPackageResponse.from_view(
        service.get_package(workspace_id, package_id)
    )


@router.get(
    "/{workspace_id}/planning-runs/{run_id}/context-package",
    response_model=ContextPackageResponse,
    operation_id="get_planning_run_context_package",
)
def get_planning_run_context_package(
    workspace_id: UUID,
    run_id: UUID,
    service: Annotated[
        ContextPackageQueryService, Depends(get_context_package_query_service)
    ],
) -> ContextPackageResponse:
    return ContextPackageResponse.from_view(
        service.get_by_consumer(workspace_id, run_id, ContextPurpose.PLANNING_RUN)
    )


@router.get(
    "/{workspace_id}/executions/{execution_id}/context-package",
    response_model=ContextPackageResponse,
    operation_id="get_execution_context_package",
)
def get_execution_context_package(
    workspace_id: UUID,
    execution_id: UUID,
    service: Annotated[
        ContextPackageQueryService, Depends(get_context_package_query_service)
    ],
) -> ContextPackageResponse:
    return ContextPackageResponse.from_view(
        service.get_by_consumer(workspace_id, execution_id, ContextPurpose.EXECUTION)
    )


@router.get(
    "/{workspace_id}/planning-runs/{run_id}/knowledge-submissions",
    response_model=KnowledgeSubmissionPageResponse,
    operation_id="list_planning_run_knowledge_submissions",
)
def list_planning_run_knowledge_submissions(
    workspace_id: UUID,
    run_id: UUID,
    service: Annotated[
        ContextPackageQueryService, Depends(get_context_package_query_service)
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> KnowledgeSubmissionPageResponse:
    return KnowledgeSubmissionPageResponse.from_view(
        service.list_submissions(
            workspace_id, planning_run_id=run_id, limit=limit, cursor=cursor
        )
    )


@router.get(
    "/{workspace_id}/tasks/{task_id}/knowledge-submissions",
    response_model=KnowledgeSubmissionPageResponse,
    operation_id="list_task_knowledge_submissions",
)
def list_task_knowledge_submissions(
    workspace_id: UUID,
    task_id: UUID,
    service: Annotated[
        ContextPackageQueryService, Depends(get_context_package_query_service)
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> KnowledgeSubmissionPageResponse:
    return KnowledgeSubmissionPageResponse.from_view(
        service.list_submissions(
            workspace_id, task_id=task_id, limit=limit, cursor=cursor
        )
    )


@router.get(
    "/{workspace_id}/worker-items/{worker_item_id}/knowledge-submissions",
    response_model=KnowledgeSubmissionPageResponse,
    operation_id="list_worker_item_knowledge_submissions",
)
def list_worker_item_knowledge_submissions(
    workspace_id: UUID,
    worker_item_id: UUID,
    service: Annotated[
        ContextPackageQueryService, Depends(get_context_package_query_service)
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> KnowledgeSubmissionPageResponse:
    return KnowledgeSubmissionPageResponse.from_view(
        service.list_submissions(
            workspace_id, worker_item_id=worker_item_id, limit=limit, cursor=cursor
        )
    )


@router.post(
    "/{workspace_id}/context-packages/{package_id}/materialize",
    response_model=ContextMaterializationResponse,
    operation_id="materialize_context_package_owner_audit",
)
def materialize_context_package(
    workspace_id: UUID,
    package_id: UUID,
    response: Response,
    service: Annotated[
        ContextPackageQueryService, Depends(get_context_package_query_service)
    ],
) -> ContextMaterializationResponse:
    response.headers["Cache-Control"] = "no-store"
    materialization = service.materialize_owner(
        workspace_id,
        package_id,
        correlation_id=uuid4(),
        caller_request_id=uuid4(),
    )
    return ContextMaterializationResponse.from_materialization(materialization)
