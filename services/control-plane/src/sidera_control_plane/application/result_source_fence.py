from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.errors import ExecutionContextChanged
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.execution_requests import ExecutionRequest

_MAX_SOURCE_DEPTH = 64


@dataclass(frozen=True, slots=True)
class ResultSourceFence:
    """Stable identities discovered before acquiring source mutation locks."""

    workspace_id: UUID
    request_id: UUID
    request_version: int
    request_lineage_id: UUID
    worker_item_id: UUID


def discover_result_source_fence(
    uow: ControlPlaneUnitOfWork,
    execution: Execution,
) -> ResultSourceFence | None:
    """Find the request whose effective result can be changed by an execution."""
    current = execution
    visited: set[UUID] = set()
    for _ in range(_MAX_SOURCE_DEPTH):
        if current.id in visited:
            raise ExecutionContextChanged(("source_execution_lineage",))
        visited.add(current.id)
        request = uow.execution_requests.get_effective_by_execution_lineage(
            current.lineage_id,
            current.workspace_id,
        )
        if request is not None:
            if request.worker_item_id is None:
                raise ExecutionContextChanged(("source_worker_item",))
            return ResultSourceFence(
                workspace_id=request.workspace_id,
                request_id=request.id,
                request_version=request.version,
                request_lineage_id=current.lineage_id,
                worker_item_id=request.worker_item_id,
            )
        if current.source_execution_id is None:
            return None
        parent = uow.executions.get(
            current.source_execution_id,
            current.workspace_id,
        )
        if parent is None:
            raise NotFound("source execution lineage is incomplete")
        current = parent
    raise ExecutionContextChanged(("source_execution_lineage",))


def lock_result_source_fence(
    uow: ControlPlaneUnitOfWork,
    fence: ResultSourceFence | None,
) -> ExecutionRequest | None:
    """Lock WorkerItem then request and reject an identity changed during discovery."""
    if fence is None:
        return None
    worker_item = uow.worker_items.get(
        fence.worker_item_id,
        fence.workspace_id,
        for_update=True,
    )
    request = uow.execution_requests.get(
        fence.request_id,
        fence.workspace_id,
        for_update=True,
    )
    changed: list[str] = []
    if worker_item is None:
        changed.append("source_worker_item")
    if request is None:
        changed.append("source_execution_request")
    else:
        if request.version != fence.request_version:
            changed.append("source_request_version")
        if request.worker_item_id != fence.worker_item_id:
            changed.append("source_worker_item")
        if request.execution_lineage_id != fence.request_lineage_id:
            changed.append("source_execution_lineage")
    if changed:
        raise ExecutionContextChanged(
            tuple(dict.fromkeys(changed)),
            current=(
                {
                    "request_id": str(request.id),
                    "request_version": request.version,
                    "execution_lineage_id": (
                        str(request.execution_lineage_id)
                        if request.execution_lineage_id is not None
                        else None
                    ),
                }
                if request is not None
                else None
            ),
        )
    return request
