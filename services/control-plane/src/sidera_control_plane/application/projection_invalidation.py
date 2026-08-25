from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sidera_control_plane.domain.events import EventData

PROJECTION_TYPES = frozenset(
    {
        "workers",
        "worker_runtime",
        "executions",
        "task_overview",
        "task_graph",
        "subtask_workspace",
        "worker_item_workspace",
        "worker_item_activity",
        "worker_trace",
        "planning_request",
        "work_context",
        "context_package",
    }
)

_WORKER_ITEM_OVERVIEW_TYPES = (
    "task_overview",
    "task_graph",
    "subtask_workspace",
    "worker_item_workspace",
)
_WORKER_ITEM_ACTIVITY_TYPES = (
    "task_graph",
    "subtask_workspace",
    "worker_item_workspace",
    "worker_item_activity",
)
_INPUT_TYPES = {
    "ExecutionInputAccepted",
    "ExecutionInputDelivered",
    "ExecutionInputDeferred",
    "ExecutionInputBlocked",
    "ExecutionInputFailed",
}
_EXECUTION_LIFECYCLE_TYPES = {
    "ExecutionCreated",
    "ExecutionLeased",
    "LeaseGranted",
    "ExecutionStarted",
    "ExecutionContinued",
    "ExecutionRetryQueued",
    "ExecutionCanceled",
    "ExecutionLost",
    "ExecutionStopRequested",
    "ExecutionSessionStarted",
    "ExecutionSessionClosed",
    "ExecutionSessionLost",
    "ExecutionRequestCreated",
    "ExecutionRequestGateBlocked",
    "ExecutionRequestGateReady",
    "ExecutionRequestMaterialized",
    "ExecutionRequestObsoleted",
    "TaskWaitingForDecision",
}


@dataclass(frozen=True, slots=True)
class ProjectionInvalidationTarget:
    projection_type: str
    resource_id: UUID | None
    change_type: str | None = None


def projection_targets_for_event(
    event: EventData,
    *,
    worker_item_id: UUID | None = None,
    root_task_id: UUID | None = None,
) -> tuple[ProjectionInvalidationTarget, ...]:
    """Map one durable domain event to the public projections it can affect."""

    payload = event.payload
    if event.aggregate_type == "context_package" or event.event_type in {
        "ContextPackageCreated",
        "ContextPackageAcknowledged",
        "ContextPackageMaterialized",
        "ContextPackageRevoked",
        "ContextPackageCorrupt",
    }:
        change_type = {
            "ContextPackageCreated": "context_package_created",
            "ContextPackageAcknowledged": "context_package_acknowledged",
            "ContextPackageMaterialized": "context_package_materialized",
            "ContextPackageRevoked": "context_package_revoked",
            "ContextPackageCorrupt": "context_package_corrupt",
        }.get(event.event_type, "context_package_updated")
        return (
            ProjectionInvalidationTarget(
                "context_package",
                _payload_uuid(payload, "package_id") or event.aggregate_id,
                change_type,
            ),
        )
    if event.aggregate_type in {"work_context", "work_context_binding"}:
        change_type = {
            "WorkContextCreated": "work_context_created",
            "WorkContextRevised": "work_context_revised",
            "WorkContextArchived": "work_context_archived",
            "WorkContextBoundToTask": "work_context_bound_to_task",
        }.get(event.event_type, "work_context_updated")
        return (
            ProjectionInvalidationTarget(
                "work_context",
                _payload_uuid(payload, "work_context_id") or event.aggregate_id,
                change_type,
            ),
        )
    planning_change_type = _planning_change_type(event.event_type)
    if event.aggregate_type == "planning_request":
        return (
            ProjectionInvalidationTarget(
                "planning_request",
                event.aggregate_id,
                planning_change_type or "planning_request_updated",
            ),
        )
    if event.aggregate_type == "planning_proposal":
        request_id = _payload_uuid(payload, "planning_request_id")
        if request_id is not None:
            return (
                ProjectionInvalidationTarget(
                    "planning_request",
                    request_id,
                    planning_change_type or "planning_request_updated",
                ),
            )
    if event.aggregate_type == "planning_run":
        request_id = _payload_uuid(payload, "planning_request_id")
        if request_id is not None:
            return (
                ProjectionInvalidationTarget(
                    "planning_request",
                    request_id,
                    planning_change_type or "planning_run_updated",
                ),
            )
    worker_item_id = worker_item_id or _payload_uuid(payload, "worker_item_id")
    root_task_id = root_task_id or _payload_uuid(payload, "root_task_id")
    context = payload.get("execution_context")
    if isinstance(context, Mapping):
        worker_item_id = worker_item_id or _mapping_uuid(context, "worker_item_id")
    execution_id = _payload_uuid(payload, "execution_id")
    if execution_id is None and event.aggregate_type == "execution":
        execution_id = event.aggregate_id
    worker_id = _payload_uuid(payload, "worker_id")

    if event.event_type == "WorkerRuntimeConfigured":
        return _targets(
            (
                "workers",
                "worker_runtime",
                "executions",
                "task_overview",
                "task_graph",
                "subtask_workspace",
                "worker_item_workspace",
            ),
            worker_item_id,
            root_task_id,
            execution_id=None,
            worker_id=(
                event.aggregate_id
                if event.aggregate_type == "worker_runtime_configuration"
                else worker_id
            ),
        )
    if event.event_type == "ModuleLifecycleChanged":
        return _targets(
            (
                "workers",
                "worker_runtime",
                "executions",
                "task_overview",
                "task_graph",
                "subtask_workspace",
                "worker_item_workspace",
            ),
            worker_item_id,
            root_task_id,
            execution_id=None,
            worker_id=None,
        )
    if event.event_type in {"ExecutionWorkerPinned", "ExecutionWorkerPinCleared"}:
        return _targets(
            ("executions", "worker_item_workspace", "subtask_workspace", "task_graph"),
            worker_item_id,
            root_task_id,
            execution_id=execution_id,
        )
    if event.event_type in {"ExecutionSchedulingBlocked", "ExecutionSchedulingUnblocked"}:
        return _targets(
            (
                "executions",
                "worker_item_workspace",
                "subtask_workspace",
                "task_overview",
                "task_graph",
            ),
            worker_item_id,
            root_task_id,
            execution_id=execution_id,
        )
    if event.event_type in {"ExecutionRuntimeBound", "ExecutionRuntimeStartBlocked"}:
        return _targets(
            (
                "executions",
                "worker_item_workspace",
                "worker_trace",
                "subtask_workspace",
                "task_overview",
                "task_graph",
            ),
            worker_item_id,
            root_task_id,
            execution_id=execution_id,
        )

    if event.event_type == "worker_item.created":
        worker_item_id = worker_item_id or _aggregate_worker_item_id(event)
        return _targets(_WORKER_ITEM_OVERVIEW_TYPES, worker_item_id, root_task_id)
    if event.event_type in {
        "worker_item.placement_materialized",
        "worker_item.updated",
    }:
        worker_item_id = worker_item_id or _aggregate_worker_item_id(event)
        if event.event_type == "worker_item.updated":
            return _targets(_WORKER_ITEM_OVERVIEW_TYPES, worker_item_id, root_task_id)
        return _targets(_WORKER_ITEM_ACTIVITY_TYPES[:3], worker_item_id, root_task_id)
    if event.event_type in _INPUT_TYPES:
        return _targets(("worker_item_workspace", "worker_item_activity"), worker_item_id, root_task_id)
    if event.event_type in _EXECUTION_LIFECYCLE_TYPES:
        return _targets(_WORKER_ITEM_ACTIVITY_TYPES, worker_item_id, root_task_id)
    if event.event_type == "ArtifactRecorded":
        return _targets(
            ("subtask_workspace", "worker_item_workspace", "worker_item_activity"),
            worker_item_id,
            root_task_id,
        )
    if event.event_type == "EvidenceRecorded":
        return _targets(
            _WORKER_ITEM_OVERVIEW_TYPES + ("worker_item_activity",),
            worker_item_id,
            root_task_id,
        )
    if event.aggregate_type == "task_plan":
        return _targets(
            ("task_overview", "task_graph", "subtask_workspace"),
            worker_item_id,
            root_task_id or event.aggregate_id,
        )
    if event.aggregate_type == "task":
        return _targets(("task_overview", "task_graph"), worker_item_id, root_task_id or event.aggregate_id)
    if event.aggregate_type == "attention":
        return _targets(("task_overview",), worker_item_id, root_task_id)
    return ()


def projection_targets_for_execution_event(
    *,
    worker_item_id: UUID | None,
    root_task_id: UUID | None,
    execution_id: UUID,
) -> tuple[ProjectionInvalidationTarget, ...]:
    return _targets(
        ("worker_item_workspace", "worker_item_activity", "worker_trace"),
        worker_item_id,
        root_task_id,
        trace_resource_id=execution_id,
    )


def projection_targets_for_artifact(
    *, worker_item_id: UUID | None, root_task_id: UUID | None
) -> tuple[ProjectionInvalidationTarget, ...]:
    return _targets(
        ("subtask_workspace", "worker_item_workspace", "worker_item_activity"),
        worker_item_id,
        root_task_id,
    )


def projection_targets_for_evidence(
    *, worker_item_id: UUID | None, root_task_id: UUID | None
) -> tuple[ProjectionInvalidationTarget, ...]:
    return _targets(
        _WORKER_ITEM_OVERVIEW_TYPES + ("worker_item_activity",),
        worker_item_id,
        root_task_id,
    )


def projection_targets_for_module() -> tuple[ProjectionInvalidationTarget, ...]:
    return _targets(
        (
            "workers",
            "worker_runtime",
            "executions",
            "task_overview",
            "task_graph",
            "subtask_workspace",
            "worker_item_workspace",
        ),
        None,
        None,
    )


def projection_targets_for_worker_item_change(
    *,
    worker_item_id: UUID,
    root_task_id: UUID,
    change_type: str,
) -> tuple[ProjectionInvalidationTarget, ...]:
    if change_type == "worker_item.placement_materialized":
        projection_types = _WORKER_ITEM_ACTIVITY_TYPES[:3]
    else:
        projection_types = _WORKER_ITEM_OVERVIEW_TYPES
    return _targets(projection_types, worker_item_id, root_task_id)


def _targets(
    projection_types: Sequence[str],
    worker_item_id: UUID | None,
    root_task_id: UUID | None,
    *,
    trace_resource_id: UUID | None = None,
    execution_id: UUID | None = None,
    worker_id: UUID | None = None,
) -> tuple[ProjectionInvalidationTarget, ...]:
    result: list[ProjectionInvalidationTarget] = []
    for projection_type in projection_types:
        if projection_type not in PROJECTION_TYPES:
            raise ValueError(f"unsupported projection type: {projection_type}")
        if projection_type in {"workers", "worker_runtime"}:
            resource_id = worker_id
        elif projection_type == "executions":
            resource_id = execution_id
        elif projection_type in {"worker_item_workspace", "worker_item_activity"}:
            resource_id = worker_item_id
        elif projection_type == "worker_trace":
            resource_id = trace_resource_id
        else:
            resource_id = root_task_id
        result.append(ProjectionInvalidationTarget(projection_type, resource_id))
    return tuple(result)


def _planning_change_type(event_type: str) -> str | None:
    if event_type in {
        "PlanningRequestSubmitted",
        "PlanningRequestCancelled",
        "PlanningClarificationAnswered",
        "PlanningProposalRevisionRequested",
        "PlanningProposalRejected",
        "PlanningBlockedRunRetried",
    }:
        return "planning_request_updated"
    return {
        "PlanningRunUpdated": "planning_run_updated",
        "PlanningClarificationRequired": "planning_clarification_required",
        "PlanningProposalReady": "planning_proposal_ready",
        "PlanningProposalConflicted": "planning_proposal_conflicted",
        "PlanningMaterialized": "planning_materialized",
        "PlanningProposalConfirmed": "planning_materialized",
    }.get(event_type)


def _aggregate_worker_item_id(event: EventData) -> UUID | None:
    if event.aggregate_type in {"worker_item", "worker_item_placement"}:
        return event.aggregate_id
    return None


def _payload_uuid(payload: Mapping[str, Any], key: str) -> UUID | None:
    return _mapping_uuid(payload, key)


def _mapping_uuid(payload: Mapping[str, Any], key: str) -> UUID | None:
    value = payload.get(key)
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None
