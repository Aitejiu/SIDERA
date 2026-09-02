from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.domain.collaboration_runtime import (
    RuntimeDecisionOutcome,
    RuntimeEnvelopeError,
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


def _request(
    request_id: UUID,
    *,
    eligible: bool,
    version: int = 1,
    status: str = "blocked",
    subtask_key: str = "subtask-a",
) -> dict[str, object]:
    return {
        "id": str(request_id),
        "subtask_key": subtask_key,
        "worker_item_key": f"worker-{request_id.hex[-4:]}",
        "version": version,
        "status": status,
        "collaboration_gate_mode": "release_required",
        "eligible": eligible,
        "eligibility_reason": "eligible" if eligible else "dependencies_blocked",
    }


def projection_fixture(
    *,
    eligible: tuple[UUID, ...] = (REQUEST_A, REQUEST_B),
    blocked: tuple[UUID, ...] = (REQUEST_C,),
    releases: tuple[UUID, ...] = (),
) -> RuntimeProjectionV1:
    requests = tuple(
        _request(request_id, eligible=request_id in eligible)
        for request_id in (*eligible, *blocked)
    )
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
            "runtime_capability_snapshot": {
                "max_decision_bytes": 65_536,
                "max_intents_per_decision": 128,
                "max_strategy_state_bytes": 16_384,
            },
        },
        tasks=(),
        subtasks=(),
        worker_items=(),
        dependencies=(),
        execution_requests=requests,
        executions=(),
        artifacts=(),
        evidence=(),
        releases=tuple(
            {
                "id": str(UUID(int=370000000000000000000000000000000 + index)),
                "run_id": str(RUN_ID),
                "plan_snapshot_id": str(PLAN_SNAPSHOT_ID),
                "plan_generation": 7,
                "plan_content_hash": "a" * 64,
                "execution_request_id": str(request_id),
                "request_version_at_release": 1,
            }
            for index, request_id in enumerate(releases, start=1)
        ),
        previous_strategy_state={"safe_counter": 2},
        fingerprints={"semantic_change_hash": "b" * 64},
        created_at=NOW,
    )


def _runtime() -> ReferenceCollaborationRuntime:
    return ReferenceCollaborationRuntime(clock=lambda: NOW)


def test_reference_runtime_releases_every_eligible_request_only() -> None:
    decision = _runtime().evaluate(
        projection_fixture(eligible=(REQUEST_A, REQUEST_B), blocked=(REQUEST_C,)),
        operation_id=OPERATION_ID,
        deadline=NOW + timedelta(seconds=5),
    )

    assert decision.outcome is RuntimeDecisionOutcome.INTENTS
    assert {item.target_execution_request_id for item in decision.intents} == {
        REQUEST_A,
        REQUEST_B,
    }
    assert [item.ordinal for item in decision.intents] == [0, 1]


def test_reference_runtime_is_stable_and_skips_existing_releases() -> None:
    projection = projection_fixture(releases=(REQUEST_A,))
    runtime = _runtime()

    first = runtime.evaluate(
        projection,
        operation_id=OPERATION_ID,
        deadline=NOW + timedelta(seconds=5),
    )
    second = runtime.evaluate(
        projection,
        operation_id=OPERATION_ID,
        deadline=NOW + timedelta(seconds=5),
    )

    assert first == second
    assert [item.target_execution_request_id for item in first.intents] == [REQUEST_B]
    assert first.run_id == RUN_ID
    assert first.projection_id == PROJECTION_ID
    assert first.projection_hash == projection.content_hash
    assert first.intents[0].idempotency_key == (
        f"collaboration-release:{RUN_ID}:{PROJECTION_ID}:{REQUEST_B}:v1"
    )


def test_reference_runtime_returns_no_change_when_no_request_is_eligible() -> None:
    decision = _runtime().evaluate(
        projection_fixture(eligible=(), blocked=(REQUEST_A, REQUEST_B, REQUEST_C)),
        operation_id=OPERATION_ID,
        deadline=NOW + timedelta(seconds=5),
    )

    assert decision.outcome is RuntimeDecisionOutcome.NO_CHANGE
    assert decision.intents == ()
    assert decision.reason_code == "no_eligible_requests"


def test_reference_runtime_does_not_override_a_disabled_policy() -> None:
    projection = projection_fixture()
    disabled = replace(projection, policy={"mode": "disabled"})

    decision = _runtime().evaluate(
        disabled,
        operation_id=OPERATION_ID,
        deadline=NOW + timedelta(seconds=5),
    )

    assert decision.outcome is RuntimeDecisionOutcome.NO_CHANGE
    assert decision.intents == ()


def test_reference_runtime_fails_closed_on_an_expired_deadline() -> None:
    with pytest.raises(RuntimeEnvelopeError, match="deadline"):
        _runtime().evaluate(
            projection_fixture(),
            operation_id=OPERATION_ID,
            deadline=NOW - timedelta(seconds=1),
        )
