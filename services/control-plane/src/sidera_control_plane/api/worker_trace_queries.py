from dataclasses import asdict
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder

from sidera_control_plane.api.dependencies import get_worker_trace_query_service
from sidera_control_plane.application.worker_trace_queries import (
    WorkerTraceQueryService,
)

router = APIRouter()


@router.get(
    "/api/v1/tasks/{root_task_id}/executions/{execution_id}/sessions/{session_id}/trace"
)
def get_worker_trace(
    root_task_id: UUID,
    execution_id: UUID,
    session_id: UUID,
    workspace_id: UUID,
    service: Annotated[WorkerTraceQueryService, Depends(get_worker_trace_query_service)],
    before_sequence: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=100),
) -> dict[str, Any]:
    result = service.get_trace(
        workspace_id,
        root_task_id,
        execution_id,
        session_id,
        before_sequence=before_sequence,
        limit=limit,
    )
    payload = asdict(result.projection)
    payload["context_branch"] = asdict(result.context_branch) if result.context_branch else None
    return jsonable_encoder(payload)
