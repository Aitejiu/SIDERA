from dataclasses import asdict
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder

from sidera_control_plane.api.dependencies import get_result_input_query_service
from sidera_control_plane.api.schemas import (
    InputSelectionResponse,
    WorkerResultInputsResponse,
)
from sidera_control_plane.application.result_input_queries import (
    ResultInputQueryService,
)

router = APIRouter()


@router.get(
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/result-inputs",
    response_model=WorkerResultInputsResponse,
    operation_id="get_worker_result_inputs",
)
def get_worker_result_inputs(
    workspace_id: UUID,
    worker_item_id: UUID,
    service: Annotated[
        ResultInputQueryService, Depends(get_result_input_query_service)
    ],
    execution_request_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    return jsonable_encoder(
        asdict(
            service.get_worker_result_inputs(
                workspace_id,
                worker_item_id,
                execution_request_id=execution_request_id,
            )
        )
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/collaboration/input-selections/{selection_id}",
    response_model=InputSelectionResponse,
    operation_id="get_collaboration_input_selection",
)
def get_collaboration_input_selection(
    workspace_id: UUID,
    selection_id: UUID,
    service: Annotated[
        ResultInputQueryService, Depends(get_result_input_query_service)
    ],
) -> dict[str, Any]:
    return jsonable_encoder(asdict(service.get_input_selection(workspace_id, selection_id)))
