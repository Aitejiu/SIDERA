from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from sidera_control_plane.api.dependencies import get_database
from sidera_control_plane.api.schemas import TaskPlanSnapshotResponse
from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.plan_repository import PlanRepository

router = APIRouter(prefix="/api/v1/tasks")


@router.get(
    "/{root_task_id}/plan/current", response_model=TaskPlanSnapshotResponse
)
def get_current_plan(
    root_task_id: UUID,
    workspace_id: UUID,
    database: Annotated[Database, Depends(get_database)],
) -> TaskPlanSnapshotResponse:
    with database.session() as session:
        snapshot = PlanRepository(session).get_current_for_root(
            root_task_id, workspace_id
        )
    if snapshot is None:
        raise NotFound("task plan not found")
    return TaskPlanSnapshotResponse.from_snapshot(snapshot)


@router.get(
    "/{root_task_id}/plan/generations",
    response_model=tuple[TaskPlanSnapshotResponse, ...],
)
def list_plan_generations(
    root_task_id: UUID,
    workspace_id: UUID,
    database: Annotated[Database, Depends(get_database)],
) -> tuple[TaskPlanSnapshotResponse, ...]:
    with database.session() as session:
        snapshots = PlanRepository(session).list_for_root(root_task_id, workspace_id)
    if not snapshots:
        raise NotFound("task plan not found")
    return tuple(
        TaskPlanSnapshotResponse.from_snapshot(item)
        for item in reversed(snapshots)
    )


@router.get(
    "/{root_task_id}/plan/generations/{generation}",
    response_model=TaskPlanSnapshotResponse,
)
def get_plan_generation(
    root_task_id: UUID,
    generation: int,
    workspace_id: UUID,
    database: Annotated[Database, Depends(get_database)],
) -> TaskPlanSnapshotResponse:
    with database.session() as session:
        snapshot = PlanRepository(session).get_for_generation(
            root_task_id, workspace_id, generation
        )
    if snapshot is None:
        raise NotFound("task plan generation not found")
    return TaskPlanSnapshotResponse.from_snapshot(snapshot)
