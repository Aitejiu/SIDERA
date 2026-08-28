from dataclasses import asdict
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder

from sidera_control_plane.api.dependencies import (
    get_plan_transition_query_service,
)
from sidera_control_plane.api.schemas import (
    PlanTransitionResponse,
    PlanTransitionSummaryResponse,
    WorkerTransitionResponse,
)
from sidera_control_plane.application.plan_transition_queries import (
    PlanTransitionQueryService,
)

router = APIRouter()


@router.get(
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/plan-transitions",
    response_model=tuple[PlanTransitionSummaryResponse, ...],
    operation_id="list_plan_transitions",
)
def list_plan_transitions(
    workspace_id: UUID,
    root_task_id: UUID,
    service: Annotated[
        PlanTransitionQueryService, Depends(get_plan_transition_query_service)
    ],
) -> list[dict[str, Any]]:
    return jsonable_encoder(
        [asdict(item) for item in service.list_plan_transitions(workspace_id, root_task_id)]
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/plan-transitions/{plan_transition_id}",
    response_model=PlanTransitionResponse,
    operation_id="get_plan_transition",
)
def get_plan_transition(
    workspace_id: UUID,
    plan_transition_id: UUID,
    service: Annotated[
        PlanTransitionQueryService, Depends(get_plan_transition_query_service)
    ],
) -> dict[str, Any]:
    return jsonable_encoder(
        asdict(service.get_plan_transition(workspace_id, plan_transition_id))
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/worker-transitions/{worker_transition_id}",
    response_model=WorkerTransitionResponse,
    operation_id="get_worker_transition",
)
def get_worker_transition(
    workspace_id: UUID,
    worker_transition_id: UUID,
    service: Annotated[
        PlanTransitionQueryService, Depends(get_plan_transition_query_service)
    ],
) -> dict[str, Any]:
    return jsonable_encoder(
        asdict(service.get_worker_transition(workspace_id, worker_transition_id))
    )
