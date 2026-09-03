from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sidera_control_plane.application.collaboration_governance_fingerprint import (
    build_release_proposal_fingerprint,
)
from sidera_control_plane.domain.collaboration import RuntimeIntent
from sidera_control_plane.domain.collaboration_runtime import RuntimeProjectionV1

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
WORKSPACE_ID = uuid4()
ROOT_TASK_ID = uuid4()
RUN_ID = uuid4()
PLAN_SNAPSHOT_ID = uuid4()
TARGET_REQUEST_ID = uuid4()
UPSTREAM_REQUEST_ID = uuid4()
TARGET_WORKER_ID = uuid4()
UPSTREAM_WORKER_ID = uuid4()
UPSTREAM_EXECUTION_ID = uuid4()
UPSTREAM_LINEAGE_ID = uuid4()
UPSTREAM_ARTIFACT_ID = uuid4()
UPSTREAM_EVIDENCE_ID = uuid4()


def projection_fixture() -> RuntimeProjectionV1:
    return RuntimeProjectionV1(
        schema_version=1,
        projection_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        run_id=RUN_ID,
        plan_snapshot_id=PLAN_SNAPSHOT_ID,
        plan_generation=4,
        plan_content_hash="a" * 64,
        run_resource_version=2,
        fact_cursor=19,
        policy={
            "mode": "enabled",
            "default_policy": {"enabled": True, "strategy_id": "all_required"},
            "subtask_overrides": [],
            "summary": "discard this policy summary",
        },
        tasks=(),
        subtasks=(),
        worker_items=(
            {"id": str(TARGET_WORKER_ID), "key": "verify.tests", "title": "Tests"},
            {"id": str(UPSTREAM_WORKER_ID), "key": "build.code", "title": "Build"},
            {"id": str(uuid4()), "key": "unrelated", "title": "Noise"},
        ),
        dependencies=(
            {
                "dependency_type": "execution_request",
                "downstream_request_id": str(TARGET_REQUEST_ID),
                "upstream_request_id": str(UPSTREAM_REQUEST_ID),
                "enforcement": "hard",
                "required_artifact_refs": ["build"],
                "required_evidence_refs": ["acceptance:build"],
            },
        ),
        execution_requests=(
            {
                "id": str(TARGET_REQUEST_ID),
                "worker_item_id": str(TARGET_WORKER_ID),
                "worker_item_key": "verify.tests",
                "subtask_key": "verify",
                "version": 3,
                "status": "ready",
                "collaboration_gate_mode": "release_required",
                "gate_reason": "eligible",
                "execution_lineage_id": None,
                "expected_artifact_refs": [],
                "expected_evidence_refs": [],
            },
            {
                "id": str(UPSTREAM_REQUEST_ID),
                "worker_item_id": str(UPSTREAM_WORKER_ID),
                "worker_item_key": "build.code",
                "subtask_key": "build",
                "version": 2,
                "status": "completed",
                "collaboration_gate_mode": "release_required",
                "gate_reason": "complete",
                "execution_lineage_id": str(UPSTREAM_LINEAGE_ID),
                "expected_artifact_refs": ["build"],
                "expected_evidence_refs": ["acceptance:build"],
            },
        ),
        executions=(
            {
                "id": str(UPSTREAM_EXECUTION_ID),
                "lineage_id": str(UPSTREAM_LINEAGE_ID),
                "task_id": str(ROOT_TASK_ID),
                "worker_item_id": str(UPSTREAM_WORKER_ID),
                "attempt": 1,
                "status": "completed",
                "failure_code": None,
                "started_at": NOW.isoformat(),
                "finished_at": (NOW + timedelta(minutes=1)).isoformat(),
                "version": 2,
            },
        ),
        artifacts=(
            {
                "id": str(UPSTREAM_ARTIFACT_ID),
                "execution_id": str(UPSTREAM_EXECUTION_ID),
                "task_id": str(ROOT_TASK_ID),
                "artifact_type": "build",
                "content_hash": "b" * 64,
                "size_bytes": 10,
                "media_type": "text/plain",
            },
        ),
        evidence=(
            {
                "id": str(UPSTREAM_EVIDENCE_ID),
                "execution_id": str(UPSTREAM_EXECUTION_ID),
                "task_id": str(ROOT_TASK_ID),
                "acceptance_criterion_ref": "acceptance:build",
                "status": "passed",
                "artifact_ids": [str(UPSTREAM_ARTIFACT_ID)],
            },
        ),
        releases=(),
        previous_strategy_state={"cursor": 3},
        fingerprints={"semantic_change_hash": "d" * 64},
        created_at=NOW,
    )


def intent_fixture() -> RuntimeIntent:
    return RuntimeIntent(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        decision_id=uuid4(),
        ordinal=0,
        intent_type="release_execution_request",
        target_execution_request_id=TARGET_REQUEST_ID,
        target_request_version=3,
        reason_code="eligible",
        summary="ignore intent summary",
        idempotency_key="release:test",
        payload_json={"request_id": str(TARGET_REQUEST_ID)},
        created_at=NOW,
    )


def test_target_fingerprint_ignores_projection_noise_and_sorts_collections() -> None:
    first = projection_fixture()
    second = replace(
        first,
        projection_id=uuid4(),
        fact_cursor=999,
        created_at=NOW + timedelta(hours=1),
        worker_items=tuple(reversed(first.worker_items)),
        execution_requests=tuple(reversed(first.execution_requests)),
        executions=tuple(
            {
                **item,
                "started_at": (NOW + timedelta(hours=2)).isoformat(),
                "finished_at": (NOW + timedelta(hours=3)).isoformat(),
            }
            for item in first.executions
        ),
    )

    first_result = build_release_proposal_fingerprint(
        first, intent_fixture(), worker_item_id=TARGET_WORKER_ID
    )
    second_result = build_release_proposal_fingerprint(
        second, intent_fixture(), worker_item_id=TARGET_WORKER_ID
    )

    assert first_result.version == 1
    assert first_result.value == second_result.value
    assert first_result.canonical_input["worker_item_id"] == str(TARGET_WORKER_ID)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        ("request", 4),
        ("artifact", "e" * 64),
        ("evidence", "failed"),
        ("plan", "f" * 64),
    ],
)
def test_target_fingerprint_changes_when_release_eligibility_facts_change(
    path: str, replacement: object
) -> None:
    projection = projection_fixture()
    if path == "request":
        requests = tuple(
            {**item, "version": replacement}
            if item["id"] == str(TARGET_REQUEST_ID)
            else item
            for item in projection.execution_requests
        )
        changed = replace(projection, execution_requests=requests)
    elif path == "artifact":
        changed = replace(
            projection,
            artifacts=({**projection.artifacts[0], "content_hash": replacement},),
        )
    elif path == "evidence":
        changed = replace(
            projection,
            evidence=({**projection.evidence[0], "status": replacement},),
        )
    else:
        changed = replace(projection, plan_content_hash=replacement)

    original = build_release_proposal_fingerprint(
        projection, intent_fixture(), worker_item_id=TARGET_WORKER_ID
    )
    updated = build_release_proposal_fingerprint(
        changed, intent_fixture(), worker_item_id=TARGET_WORKER_ID
    )
    assert original.value != updated.value


def test_target_fingerprint_does_not_include_private_or_unrelated_fields() -> None:
    result = build_release_proposal_fingerprint(
        projection_fixture(), intent_fixture(), worker_item_id=TARGET_WORKER_ID
    )
    serialized = str(result.canonical_input)
    for forbidden in (
        "created_at",
        "started_at",
        "finished_at",
        "fact_cursor",
        "projection_id",
        "summary",
        "reason",
        "trace",
        "conversation",
        "unrelated",
    ):
        assert forbidden not in serialized
    assert len(result.value) == 64
    assert set(result.value) <= set("0123456789abcdef")


def test_target_fingerprint_changes_with_policy_and_handoff_fence() -> None:
    projection = projection_fixture()
    intent = intent_fixture()
    original = build_release_proposal_fingerprint(
        projection, intent, worker_item_id=TARGET_WORKER_ID
    )
    changed_policy = replace(
        projection,
        policy={
            "mode": "enabled",
            "default_policy": {"enabled": True, "strategy_id": "different"},
            "subtask_overrides": [],
        },
    )
    changed_fence = build_release_proposal_fingerprint(
        projection,
        intent,
        worker_item_id=TARGET_WORKER_ID,
        target_handoff_fence={"transition_id": str(uuid4()), "version": 2},
    )

    assert original.value != build_release_proposal_fingerprint(
        changed_policy, intent, worker_item_id=TARGET_WORKER_ID
    ).value
    assert original.value != changed_fence.value
