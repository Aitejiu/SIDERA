"""Reusable conformance checks for CollaborationRuntime implementations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.domain.collaboration_runtime import (
    CollaborationRuntime,
    RuntimeDecisionOutcome,
    RuntimeProjectionV1,
)

WORKSPACE_ID = UUID("21000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("31000000-0000-0000-0000-000000000001")
PLAN_SNAPSHOT_ID = UUID("32000000-0000-0000-0000-000000000001")
RUN_ID = UUID("33000000-0000-0000-0000-000000000001")
PROJECTION_ID = UUID("34000000-0000-0000-0000-000000000001")
REQUEST_A = UUID("35000000-0000-0000-0000-000000000001")
REQUEST_B = UUID("35000000-0000-0000-0000-000000000002")
REQUEST_C = UUID("35000000-0000-0000-0000-000000000003")
OPERATION_ID = UUID("36000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _request(request_id: UUID, *, eligible: bool) -> dict[str, object]:
    return {
        "id": str(request_id),
        "subtask_key": "subtask-a",
        "worker_item_key": f"worker-{request_id.hex[-4:]}",
        "version": 1,
        "status": "blocked",
        "collaboration_gate_mode": "release_required",
        "eligible": eligible,
    }


def _projection(*, eligible: bool = True) -> RuntimeProjectionV1:
    return RuntimeProjectionV1(
        schema_version=1,
        projection_id=PROJECTION_ID,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        run_id=RUN_ID,
        plan_snapshot_id=PLAN_SNAPSHOT_ID,
        plan_generation=7,
        plan_content_hash="a" * 64,
        run_resource_version=3,
        fact_cursor=42,
        policy={
            "schema_version": 1,
            "default_policy": {"enabled": True, "strategy_id": "all_required"},
            "subtask_overrides": [],
        },
        tasks=(),
        subtasks=(),
        worker_items=(),
        dependencies=(),
        execution_requests=(
            _request(REQUEST_A, eligible=eligible),
            _request(REQUEST_B, eligible=eligible),
            _request(REQUEST_C, eligible=False),
        ),
        executions=(),
        artifacts=(),
        evidence=(),
        releases=(),
        previous_strategy_state={"safe_counter": 2},
        fingerprints={"semantic_change_hash": "b" * 64},
        created_at=NOW,
    )


def assert_collaboration_runtime_conforms(
    factory: Callable[[], CollaborationRuntime],
) -> None:
    """Exercise the public runtime contract without provider-specific details."""

    projection = _projection()
    runtime = factory()
    first = runtime.evaluate(
        projection,
        operation_id=OPERATION_ID,
        deadline=NOW + timedelta(seconds=5),
    )
    repeated = runtime.evaluate(
        projection,
        operation_id=OPERATION_ID,
        deadline=NOW + timedelta(seconds=5),
    )

    assert first == repeated
    assert first.run_id == RUN_ID
    assert first.projection_id == PROJECTION_ID
    assert first.projection_hash == projection.content_hash
    assert [intent.target_execution_request_id for intent in first.intents] == [
        REQUEST_A,
        REQUEST_B,
    ]
    assert [intent.ordinal for intent in first.intents] == [0, 1]
    assert all(intent.target_execution_request_id != REQUEST_C for intent in first.intents)
    assert all(intent.idempotency_key for intent in first.intents)

    no_change = factory().evaluate(
        _projection(eligible=False),
        operation_id=OPERATION_ID,
        deadline=NOW + timedelta(seconds=5),
    )
    assert no_change.outcome is RuntimeDecisionOutcome.NO_CHANGE
    assert no_change.intents == ()


def test_reference_runtime_conforms() -> None:
    assert_collaboration_runtime_conforms(
        lambda: ReferenceCollaborationRuntime(clock=lambda: NOW)
    )
