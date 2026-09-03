from uuid import UUID

import pytest

from sidera_control_plane.application.projection_invalidation import (
    PROJECTION_TYPES,
    projection_targets_for_event,
)
from sidera_control_plane.domain.events import EventData

WORKSPACE_ID = UUID("21000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("31000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("41000000-0000-0000-0000-000000000001")
WORKER_ID = UUID("42000000-0000-0000-0000-000000000001")
EXECUTION_ID = UUID("61000000-0000-0000-0000-000000000001")


def _event(
    event_type: str,
    *,
    aggregate_type: str = "execution",
    aggregate_id: UUID = EXECUTION_ID,
    payload: dict[str, object] | None = None,
) -> EventData:
    return EventData(
        id=UUID("51000000-0000-0000-0000-000000000001"),
        workspace_id=WORKSPACE_ID,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=4,
        event_type=event_type,
        payload=payload
        if payload is not None
        else {
            "root_task_id": str(ROOT_TASK_ID),
            "worker_item_id": str(WORKER_ITEM_ID),
        },
        actor_type="test",
        actor_id="projection-test",
        causation_id=UUID("71000000-0000-0000-0000-000000000001"),
        correlation_id=UUID("81000000-0000-0000-0000-000000000001"),
    )


@pytest.mark.parametrize(
    ("event_type", "expected_projection_types"),
    [
        (
            "worker_item.created",
            {
                "task_overview",
                "task_graph",
                "subtask_workspace",
                "worker_item_workspace",
            },
        ),
        (
            "worker_item.placement_materialized",
            {"task_graph", "subtask_workspace", "worker_item_workspace"},
        ),
        (
            "ExecutionStarted",
            {
                "task_graph",
                "subtask_workspace",
                "worker_item_workspace",
                "worker_item_activity",
                "collaboration_runtime",
            },
        ),
        (
            "ExecutionInputAccepted",
            {"worker_item_workspace", "worker_item_activity"},
        ),
        (
            "ArtifactRecorded",
            {
                "subtask_workspace",
                "worker_item_workspace",
                "worker_item_activity",
                "collaboration_runtime",
            },
        ),
        (
            "EvidenceRecorded",
            {
                "task_overview",
                "task_graph",
                "subtask_workspace",
                "worker_item_workspace",
                "worker_item_activity",
                "collaboration_runtime",
            },
        ),
        (
            "ContextPackageCreated",
            {"context_package"},
        ),
        (
            "ContextPackageAcknowledged",
            {"context_package"},
        ),
        (
            "ContextPackageRevoked",
            {"context_package"},
        ),
        (
            "ContextPackageMaterialized",
            {"context_package"},
        ),
    ],
)
def test_projection_change_targets_only_affected_resources(
    event_type: str, expected_projection_types: set[str]
) -> None:
    targets = projection_targets_for_event(_event(event_type))

    assert {target.projection_type for target in targets} == expected_projection_types
    assert all(target.projection_type in PROJECTION_TYPES for target in targets)
    if expected_projection_types & {"worker_item_workspace", "worker_item_activity"}:
        assert {
            target.resource_id
            for target in targets
            if target.projection_type
            in {"worker_item_workspace", "worker_item_activity"}
        } == {WORKER_ITEM_ID}
    subtask_resources = {
        target.resource_id
        for target in targets
        if target.projection_type == "subtask_workspace"
    }
    if "subtask_workspace" in expected_projection_types:
        assert subtask_resources == {ROOT_TASK_ID}
    elif "subtask_workspace" in {target.projection_type for target in targets}:
        assert subtask_resources == set()


def test_projection_targets_use_workspace_scope_when_identity_is_unavailable() -> None:
    event = _event("ExecutionStarted")
    event = EventData(
        id=event.id,
        workspace_id=event.workspace_id,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        aggregate_version=event.aggregate_version,
        event_type=event.event_type,
        payload={},
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        causation_id=event.causation_id,
        correlation_id=event.correlation_id,
    )

    targets = projection_targets_for_event(event)

    assert targets
    assert all(target.resource_id is None for target in targets)


@pytest.mark.parametrize(
    "event_type",
    [
        "ExecutionStarted",
        "ExecutionRetryQueued",
        "ExecutionRequestGateReady",
        "ArtifactRecorded",
        "EvidenceRecorded",
        "PlanningMaterialized",
    ],
)
def test_collaboration_runtime_target_is_root_scoped_for_runtime_facts(
    event_type: str,
) -> None:
    aggregate_type = "planning_request" if event_type == "PlanningMaterialized" else "execution"
    targets = projection_targets_for_event(
        _event(event_type, aggregate_type=aggregate_type)
    )

    assert any(
        target.projection_type == "collaboration_runtime"
        and target.resource_id == ROOT_TASK_ID
        for target in targets
    )


def test_collaboration_runtime_target_is_omitted_when_root_is_unresolved() -> None:
    event = _event(
        "ExecutionStarted",
        payload={},
    )

    targets = projection_targets_for_event(event)

    assert all(target.projection_type != "collaboration_runtime" for target in targets)


def test_collaboration_run_events_invalidate_the_root_runtime_projection() -> None:
    event = _event(
        "CollaborationRunActivated",
        aggregate_type="collaboration_run",
        aggregate_id=UUID("91000000-0000-0000-0000-000000000001"),
    )

    targets = projection_targets_for_event(event)

    assert targets == (type(targets[0])("collaboration_runtime", ROOT_TASK_ID),)


@pytest.mark.parametrize(
    ("event_type", "aggregate_type"),
    [
        ("CollaborationReleasePaused", "collaboration_governance_hold"),
        ("CollaborationReleaseProposalCreated", "collaboration_release_proposal"),
        ("CollaborationReleaseProposalApproved", "collaboration_release_proposal"),
        ("CollaborationReleaseProposalRejected", "collaboration_release_proposal"),
        ("CollaborationReleaseProposalStale", "collaboration_release_proposal"),
        ("CollaborationAutomaticReleaseResumed", "collaboration_governance_hold"),
    ],
)
def test_governance_events_refresh_governance_views_without_runtime_wake(
    event_type: str, aggregate_type: str
) -> None:
    targets = projection_targets_for_event(
        _event(event_type, aggregate_type=aggregate_type)
    )

    assert {
        target.projection_type for target in targets
    } == {
        "collaboration_governance",
        "task_overview",
        "task_graph",
        "subtask_workspace",
        "worker_item_workspace",
        "worker_item_activity",
    }
    assert all(target.projection_type != "collaboration_runtime" for target in targets)


def test_candidate_submission_invalidates_its_related_resources() -> None:
    targets = projection_targets_for_event(
        _event(
            "KnowledgeCandidateSubmissionQueued",
            aggregate_type="knowledge_candidate_submission",
            payload={
                "task_id": str(ROOT_TASK_ID),
                "subtask_task_id": str(ROOT_TASK_ID),
                "worker_item_id": str(WORKER_ITEM_ID),
                "execution_id": str(EXECUTION_ID),
            },
        )
    )

    assert {
        target.projection_type for target in targets
    } == {
        "knowledge_candidate_submission",
        "task_overview",
        "task_graph",
        "subtask_workspace",
        "worker_item_workspace",
        "worker_item_activity",
        "executions",
    }


@pytest.mark.parametrize(
    ("event_type", "aggregate_type", "aggregate_id", "payload", "expected"),
    [
        (
            "WorkerRuntimeConfigured",
            "worker_runtime_configuration",
            WORKER_ID,
            {},
            {
                "workers",
                "worker_runtime",
                "executions",
                "task_overview",
                "task_graph",
                "subtask_workspace",
                "worker_item_workspace",
            },
        ),
        (
            "ModuleLifecycleChanged",
            "module_instance",
            UUID("43000000-0000-0000-0000-000000000001"),
            {},
            {
                "workers",
                "worker_runtime",
                "executions",
                "task_overview",
                "task_graph",
                "subtask_workspace",
                "worker_item_workspace",
            },
        ),
        (
            "ExecutionWorkerPinned",
            "execution",
            EXECUTION_ID,
            {"worker_id": str(WORKER_ID)},
            {"executions", "worker_item_workspace", "subtask_workspace", "task_graph"},
        ),
        (
            "ExecutionWorkerPinCleared",
            "execution",
            EXECUTION_ID,
            {},
            {"executions", "worker_item_workspace", "subtask_workspace", "task_graph"},
        ),
        (
            "ExecutionSchedulingBlocked",
            "scheduling_block",
            UUID("44000000-0000-0000-0000-000000000001"),
            {"execution_id": str(EXECUTION_ID)},
            {
                "executions",
                "worker_item_workspace",
                "subtask_workspace",
                "task_overview",
                "task_graph",
            },
        ),
        (
            "ExecutionSchedulingUnblocked",
            "scheduling_block",
            UUID("44000000-0000-0000-0000-000000000001"),
            {"execution_id": str(EXECUTION_ID)},
            {
                "executions",
                "worker_item_workspace",
                "subtask_workspace",
                "task_overview",
                "task_graph",
            },
        ),
        (
            "ExecutionRuntimeBound",
            "execution_binding",
            UUID("45000000-0000-0000-0000-000000000001"),
            {"execution_id": str(EXECUTION_ID)},
            {
                "executions",
                "worker_item_workspace",
                "worker_trace",
                "subtask_workspace",
                "task_overview",
                "task_graph",
            },
        ),
        (
            "ExecutionRuntimeStartBlocked",
            "gateway_operation",
            UUID("46000000-0000-0000-0000-000000000001"),
            {"execution_id": str(EXECUTION_ID)},
            {
                "executions",
                "worker_item_workspace",
                "worker_trace",
                "subtask_workspace",
                "task_overview",
                "task_graph",
            },
        ),
    ],
)
def test_runtime_events_invalidate_only_the_public_facts_they_can_change(
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    payload: dict[str, object],
    expected: set[str],
) -> None:
    event = _event(
        event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload={
            "root_task_id": str(ROOT_TASK_ID),
            "worker_item_id": str(WORKER_ITEM_ID),
            **payload,
        },
    )

    targets = projection_targets_for_event(event)

    assert {target.projection_type for target in targets} == expected
    assert all(target.projection_type in PROJECTION_TYPES for target in targets)
    execution_target = next(
        target for target in targets if target.projection_type == "executions"
    )
    assert execution_target.resource_id == (
        EXECUTION_ID
        if "execution_id" in payload or aggregate_type == "execution"
        else None
    )


def test_worker_runtime_changes_keep_worker_identity_but_scope_derived_facts_to_workspace() -> None:
    targets = projection_targets_for_event(
        _event(
            "WorkerRuntimeConfigured",
            aggregate_type="worker_runtime_configuration",
            aggregate_id=WORKER_ID,
            payload={},
        )
    )

    assert {
        target.resource_id
        for target in targets
        if target.projection_type in {"workers", "worker_runtime"}
    } == {WORKER_ID}
    assert all(
        target.resource_id is None
        for target in targets
        if target.projection_type
        not in {"workers", "worker_runtime"}
    )


@pytest.mark.parametrize(
    "event_type",
    [
        "HandoffProposalPrepared",
        "HandoffProposalStale",
        "HandoffProposalCancelled",
        "HandoffConfirmed",
        "HandoffCheckpointCaptured",
        "HandoffSourceStopped",
        "HandoffWorkspaceFrozen",
        "HandoffTargetExecutionCreated",
        "HandoffContextBundleCreated",
        "HandoffWorkspaceTransferred",
        "HandoffTargetSessionStarted",
        "HandoffContextAcknowledged",
        "HandoffInputTransferred",
        "HandoffTargetActivated",
        "HandoffBlocked",
        "HandoffRecovered",
        "HandoffConverged",
    ],
)
def test_handoff_events_invalidate_the_worker_item_and_root_projections(
    event_type: str,
) -> None:
    event = _event(
        event_type,
        aggregate_type=(
            "handoff_proposal" if event_type.startswith("HandoffProposal") else "handoff_transition"
        ),
        payload={
            "root_task_id": str(ROOT_TASK_ID),
            "worker_item_id": str(WORKER_ITEM_ID),
            "target_execution_id": str(EXECUTION_ID),
        },
    )

    targets = projection_targets_for_event(event)

    assert {
        target.projection_type for target in targets
    } >= {
        "task_overview",
        "task_graph",
        "subtask_workspace",
        "worker_item_workspace",
        "worker_item_activity",
    }
    assert all(target.projection_type in PROJECTION_TYPES for target in targets)
    assert {
        target.resource_id
        for target in targets
        if target.projection_type == "worker_item_workspace"
    } == {WORKER_ITEM_ID}
