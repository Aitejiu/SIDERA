from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from sidera_control_plane.api.dependencies import get_module_query_service
from sidera_control_plane.api.schemas import ModuleSummaryResponse
from sidera_control_plane.application.module_queries import ModuleQueryService

router = APIRouter(prefix="/api/v1/modules")

ModuleType = Literal["worker_adapter", "workspace_provider"]


@router.get("", response_model=tuple[ModuleSummaryResponse, ...])
def list_modules(
    workspace_id: UUID,
    service: Annotated[ModuleQueryService, Depends(get_module_query_service)],
    module_type: Annotated[ModuleType | None, Query()] = None,
) -> tuple[ModuleSummaryResponse, ...]:
    return tuple(
        ModuleSummaryResponse.from_view(item)
        for item in service.list_modules(workspace_id, module_type)
    )
