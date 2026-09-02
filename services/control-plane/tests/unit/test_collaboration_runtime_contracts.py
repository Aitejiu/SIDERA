from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.collaboration_runtime import (
    RuntimeDecisionOutcome,
    RuntimeEnvelopeError,
    RuntimeProjectionV1,
    canonical_json_bytes,
    parse_runtime_decision_v1,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
WORKSPACE_ID = uuid4()
ROOT_TASK_ID = uuid4()
PLAN_SNAPSHOT_ID = uuid4()
RUN_ID = uuid4()
PROJECTION_ID = uuid4()
OPERATION_ID = uuid4()
REQUEST_A = uuid4()
REQUEST_B = uuid4()


def projection_fixture(
    *, request_order: tuple[UUID, ...] = (REQUEST_B, REQUEST_A)
) -> RuntimeProjectionV1:
    return RuntimeProjectionV1(
        schema_version=1,
        projection_id=PROJECTION_ID,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        run_id=RUN_ID,
        plan_snapshot_id=PLAN_SNAPSHOT_ID,
        plan_generation=4,
        plan_content_hash="a" * 64,
        run_resource_version=2,
        fact_cursor=19,
        policy={
            "default_policy": {"enabled": True, "strategy_id": "all_required"}
        },
        tasks=({"task_id": str(ROOT_TASK_ID), "status": "running"},),
        subtasks=({"subtask_key": "verify", "status": "blocked"},),
        worker_items=({"worker_item_key": "verify.tests", "status": "queued"},),
        dependencies=(
            {
                "downstream_worker_item_key": "verify.tests",
                "upstream_worker_item_key": "build.code",
                "kind": "hard",
            },
        ),
        execution_requests=tuple(
            {
                "execution_request_id": str(request_id),
                "request_version": 1,
                "worker_item_key": f"worker-{request_id}",
                "gate_mode": "release_required",
            }
            for request_id in request_order
        ),
        executions=(),
        artifacts=(),
        evidence=(),
        releases=(),
        previous_strategy_state={},
        fingerprints={"requests": "b" * 64},
        created_at=NOW,
    )


def valid_decision_mapping() -> dict[str, object]:
    return {
        "schema_version": 1,
        "operation_id": str(OPERATION_ID),
        "run_id": str(RUN_ID),
        "projection_id": str(PROJECTION_ID),
        "projection_hash": "a" * 64,
        "outcome": "intents",
        "reason_code": "eligible",
        "summary": "release one request",
        "strategy_state": {"last_join": "complete"},
        "intents": [
            {
                "intent_type": "release_execution_request",
                "ordinal": 0,
                "target_execution_request_id": str(REQUEST_A),
                "target_request_version": 1,
                "reason_code": "dependencies_satisfied",
                "summary": "upstream is complete",
                "idempotency_key": f"release:{REQUEST_A}",
            }
        ],
    }


def test_projection_hash_is_stable_and_excludes_nonsemantic_time() -> None:
    first = projection_fixture(request_order=(REQUEST_B, REQUEST_A))
    second = replace(
        projection_fixture(request_order=(REQUEST_A, REQUEST_B)),
        created_at=NOW + timedelta(minutes=5),
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.content_hash == second.content_hash
    assert "assembled_at" not in first.to_mapping()


def test_projection_rejects_private_runtime_input_fields() -> None:
    with pytest.raises(RuntimeEnvelopeError, match="private projection field"):
        projection_fixture().with_fingerprint(
            {"provider_api_key": "secret-value"}
        )


def test_decision_parser_builds_typed_output_and_preserves_echoes() -> None:
    decision = parse_runtime_decision_v1(
        canonical_json_bytes(valid_decision_mapping()),
        max_bytes=65_536,
        max_intents=128,
        max_state_bytes=16_384,
    )

    assert decision.outcome is RuntimeDecisionOutcome.INTENTS
    assert decision.operation_id == OPERATION_ID
    assert decision.run_id == RUN_ID
    assert decision.projection_id == PROJECTION_ID
    assert decision.intents[0].target_execution_request_id == REQUEST_A


def test_decision_parser_rejects_unknown_intent_without_building_decision() -> None:
    raw = valid_decision_mapping()
    raw["intents"] = [{"intent_type": "pause_worker", "ordinal": 0}]

    with pytest.raises(RuntimeEnvelopeError, match="unsupported intent_type"):
        parse_runtime_decision_v1(
            canonical_json_bytes(raw),
            max_bytes=65_536,
            max_intents=128,
            max_state_bytes=16_384,
        )


def test_decision_parser_rejects_unknown_fields_duplicate_ordinals_and_bad_outcome() -> None:
    unknown = valid_decision_mapping()
    unknown["provider_trace"] = "do-not-store"
    with pytest.raises(RuntimeEnvelopeError, match="unknown decision fields"):
        parse_runtime_decision_v1(
            canonical_json_bytes(unknown),
            max_bytes=65_536,
            max_intents=128,
            max_state_bytes=16_384,
        )

    duplicate = valid_decision_mapping()
    duplicate["intents"] = [
        {**duplicate["intents"][0], "ordinal": 0},  # type: ignore[index]
        {**duplicate["intents"][0], "ordinal": 0},  # type: ignore[index]
    ]
    with pytest.raises(RuntimeEnvelopeError, match="contiguous intent ordinals"):
        parse_runtime_decision_v1(
            canonical_json_bytes(duplicate),
            max_bytes=65_536,
            max_intents=128,
            max_state_bytes=16_384,
        )

    no_change = valid_decision_mapping()
    no_change["outcome"] = "no_change"
    no_change["intents"] = []
    parsed = parse_runtime_decision_v1(
        canonical_json_bytes(no_change),
        max_bytes=65_536,
        max_intents=128,
        max_state_bytes=16_384,
    )
    assert parsed.outcome is RuntimeDecisionOutcome.NO_CHANGE


def test_decision_parser_applies_byte_state_and_intent_limits_before_returning() -> None:
    with pytest.raises(RuntimeEnvelopeError, match="maximum response size"):
        parse_runtime_decision_v1(
            canonical_json_bytes(valid_decision_mapping()),
            max_bytes=32,
            max_intents=128,
            max_state_bytes=16_384,
        )

    oversized_state = valid_decision_mapping()
    oversized_state["strategy_state"] = {"state": "x" * 100}
    with pytest.raises(RuntimeEnvelopeError, match="strategy_state"):
        parse_runtime_decision_v1(
            canonical_json_bytes(oversized_state),
            max_bytes=65_536,
            max_intents=128,
            max_state_bytes=16,
        )

    with pytest.raises(RuntimeEnvelopeError, match="maximum intent count"):
        parse_runtime_decision_v1(
            canonical_json_bytes(valid_decision_mapping()),
            max_bytes=65_536,
            max_intents=0,
            max_state_bytes=16_384,
        )


def test_decision_parser_rejects_invalid_utf8_nan_and_trailing_json() -> None:
    with pytest.raises(RuntimeEnvelopeError, match="UTF-8"):
        parse_runtime_decision_v1(
            b"\xff",
            max_bytes=65_536,
            max_intents=128,
            max_state_bytes=16_384,
        )

    with pytest.raises(RuntimeEnvelopeError, match="non-finite"):
        projection_hash = "a" * 64
        parse_runtime_decision_v1(
            f'{{"schema_version":1,"operation_id":"{OPERATION_ID}","run_id":"{RUN_ID}","projection_id":"{PROJECTION_ID}","projection_hash":"{projection_hash}","outcome":"no_change","reason_code":"none","summary":"none","strategy_state":{{"value":NaN}},"intents":[]}}'.encode(),
            max_bytes=65_536,
            max_intents=128,
            max_state_bytes=16_384,
        )

    with pytest.raises(RuntimeEnvelopeError, match="trailing JSON"):
        parse_runtime_decision_v1(
            canonical_json_bytes(valid_decision_mapping()) + b"{}",
            max_bytes=65_536,
            max_intents=128,
            max_state_bytes=16_384,
        )
