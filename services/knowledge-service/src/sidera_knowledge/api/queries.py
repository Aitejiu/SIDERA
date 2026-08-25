from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from sidera_knowledge.api.dependencies import (
    authorize_any_capability,
    authorize_capability,
)
from sidera_knowledge.api.schemas import MaterializeRequest
from sidera_knowledge.application.retrieval import SearchFilters

router = APIRouter(prefix="/api/v1")
_OWNER_READ = ("local_owner", "main_agent")
_ALL_CAPABILITIES = ("local_owner", "main_agent", "control_plane")


def _query_service(request: Request):
    return request.app.state.query_service


@router.get("/workspaces/{workspace_id}/knowledge/pages")
def list_pages(
    request: Request,
    workspace_id: UUID,
    include_archived: bool = False,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    authorize_any_capability(request, _OWNER_READ)
    result = _query_service(request).list_pages(
        workspace_id,
        include_archived=include_archived,
        cursor=cursor,
        limit=limit,
    )
    return jsonable_encoder(result)


@router.get("/workspaces/{workspace_id}/knowledge/pages/{page_id}")
def get_page(request: Request, workspace_id: UUID, page_id: UUID) -> dict[str, object]:
    authorize_any_capability(request, _OWNER_READ)
    result = _query_service(request).get_page(workspace_id, page_id)
    if result is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="not_found")
    return jsonable_encoder(result)


@router.get(
    "/workspaces/{workspace_id}/knowledge/pages/{page_id}/revisions/{revision_id}"
)
def get_revision(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    revision_id: UUID,
) -> dict[str, object]:
    authorize_any_capability(request, _OWNER_READ)
    result = _query_service(request).get_revision(workspace_id, page_id, revision_id)
    if result is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="not_found")
    return jsonable_encoder(result)


@router.get("/workspaces/{workspace_id}/knowledge/pages/{page_id}/diff")
def diff_revisions(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    from_revision_id: UUID,
    to_revision_id: UUID,
    max_diff_lines: Annotated[int, Query(ge=1, le=5_000)] = 500,
    max_diff_chars: Annotated[int, Query(ge=1, le=256_000)] = 32_000,
) -> dict[str, object]:
    authorize_any_capability(request, _OWNER_READ)
    service = _query_service(request)
    before = service.get_revision(workspace_id, page_id, from_revision_id)
    after = service.get_revision(workspace_id, page_id, to_revision_id)
    if before is None or after is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="not_found")
    return jsonable_encoder(
        service.diff(before, after, max_diff_lines=max_diff_lines, max_diff_chars=max_diff_chars)
    )


@router.get("/workspaces/{workspace_id}/knowledge/search")
def search_knowledge(
    request: Request,
    workspace_id: UUID,
    q: str = "",
    tags: Annotated[list[str] | None, Query()] = None,
    source_types: Annotated[list[str] | None, Query()] = None,
    status: str | None = "active",
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, object]:
    authorize_any_capability(request, _ALL_CAPABILITIES)
    filters = SearchFilters(
        tags=tuple(tags or ()),
        source_types=tuple(source_types or ()),
        status=status,
    )
    hits = request.app.state.retrieval_adapter.search(
        workspace_id, q, filters, limit=limit
    )
    return {"items": jsonable_encoder(hits), "next_cursor": None}


@router.get("/workspaces/{workspace_id}/knowledge/candidates")
def list_candidates(
    request: Request,
    workspace_id: UUID,
    status: str | None = None,
    source_task_id: UUID | None = None,
    source_subtask_id: UUID | None = None,
    source_worker_item_id: UUID | None = None,
    source_execution_id: UUID | None = None,
    source_artifact_id: UUID | None = None,
    source_evidence_id: UUID | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    authorize_any_capability(request, _OWNER_READ)
    result = _query_service(request).list_candidates(
        workspace_id,
        status=status,
        source_task_id=source_task_id,
        source_subtask_id=source_subtask_id,
        source_worker_item_id=source_worker_item_id,
        source_execution_id=source_execution_id,
        source_artifact_id=source_artifact_id,
        source_evidence_id=source_evidence_id,
        cursor=cursor,
        limit=limit,
    )
    return jsonable_encoder(result)


@router.get("/workspaces/{workspace_id}/knowledge/candidates/{candidate_id}")
def get_candidate(
    request: Request, workspace_id: UUID, candidate_id: UUID
) -> dict[str, object]:
    authorize_any_capability(request, _OWNER_READ)
    result = _query_service(request).get_candidate(workspace_id, candidate_id)
    if result is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="not_found")
    return jsonable_encoder(result)


@router.get("/workspaces/{workspace_id}/knowledge/sources")
def list_sources(
    request: Request,
    workspace_id: UUID,
    availability: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    authorize_any_capability(request, _ALL_CAPABILITIES)
    result = _query_service(request).list_sources(
        workspace_id,
        availability=availability,
        cursor=cursor,
        limit=limit,
    )
    return jsonable_encoder(result)


@router.get("/workspaces/{workspace_id}/commands/{command_id}")
def get_command_status(
    request: Request, workspace_id: UUID, command_id: UUID
) -> dict[str, object]:
    authorize_any_capability(request, _ALL_CAPABILITIES)
    return jsonable_encoder(
        _query_service(request).get_command_status(workspace_id, command_id)
    )


@router.post("/internal/workspaces/{workspace_id}/materialize")
def materialize(
    request: Request,
    workspace_id: UUID,
    body: MaterializeRequest,
) -> JSONResponse:
    authorize_capability(request, "control_plane")
    refs = tuple(item.to_domain() for item in body.sections)
    result = _query_service(request).materialize_sections(workspace_id, refs)
    return JSONResponse(status_code=200, content=jsonable_encoder(result))
