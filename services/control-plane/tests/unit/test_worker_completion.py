from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.application.worker_completion import (
    CompletionPolicyError,
    CompletionProtocolError,
    ExecutionCompletionPolicy,
    decide_completion,
    resolve_completion_policy,
)
from sidera_control_plane.domain.gateway import ExecutionEvent
from sidera_control_plane.domain.modules import ModuleInstance, ModuleLifecycle

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
WORKSPACE_ID = uuid4()
EXECUTION_ID = uuid4()
SESSION_ID = uuid4()


def _instance(
    *,
    namespace: str = "worker",
    completion_policy: str | None = None,
) -> ModuleInstance:
    config = (
        {f"{namespace}.completion_policy": completion_policy}
        if completion_policy is not None
        else {}
    )
    return ModuleInstance(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        definition_id=uuid4(),
        instance_name="worker",
        config_version=1,
        config_namespace=namespace,
        config=config,
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )


def _event(
    event_type: str,
    sequence: int,
    payload: dict[str, object] | None = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        execution_id=EXECUTION_ID,
        session_id=SESSION_ID,
        sequence=sequence,
        event_type=event_type,
        payload=payload or {},
        occurred_at=NOW,
        raw_ref=None,
    )


def test_missing_completion_policy_defaults_to_explicit() -> None:
    assert resolve_completion_policy(_instance()) is ExecutionCompletionPolicy.EXPLICIT


@pytest.mark.parametrize("namespace", ["worker", "opencode"])
def test_completion_policy_uses_the_instance_namespace(namespace: str) -> None:
    instance = _instance(namespace=namespace, completion_policy="initial_turn")

    assert resolve_completion_policy(instance) is ExecutionCompletionPolicy.INITIAL_TURN


def test_invalid_completion_policy_fails_closed() -> None:
    with pytest.raises(CompletionPolicyError) as caught:
        resolve_completion_policy(_instance(completion_policy="best_effort"))

    assert caught.value.code == "completion_policy_invalid"


def test_explicit_policy_ignores_turn_completion() -> None:
    operation_id = uuid4()
    capability_snapshot = FakeWorkerAdapter(turn_completion_enabled=True).probe({})

    assert (
        decide_completion(
            policy=ExecutionCompletionPolicy.EXPLICIT,
            capability_snapshot=capability_snapshot,
            events=(
                _event(
                    "turn.completed",
                    2,
                    {
                        "schema_version": 1,
                        "operation_id": str(operation_id),
                        "stop_reason": "end_turn",
                    },
                ),
            ),
            initial_operation_id=operation_id,
            initial_operation_succeeded=True,
        )
        is None
    )


def test_initial_turn_accepts_only_the_successful_initial_operation() -> None:
    operation_id = uuid4()
    capability_snapshot = FakeWorkerAdapter(turn_completion_enabled=True).probe({})

    decision = decide_completion(
        policy=ExecutionCompletionPolicy.INITIAL_TURN,
        capability_snapshot=capability_snapshot,
        events=(
            _event(
                "turn.completed",
                2,
                {
                    "schema_version": 1,
                    "operation_id": str(operation_id),
                    "stop_reason": "end_turn",
                },
            ),
        ),
        initial_operation_id=operation_id,
        initial_operation_succeeded=True,
    )

    assert decision is not None
    assert decision.outcome == "succeeded"
    assert decision.event_type == "turn.completed"
    assert decision.sequence == 2
    assert decision.input_operation_id == operation_id
    assert decision.policy_version == 1
    assert decision.failure_code is None


@pytest.mark.parametrize(
    ("payload", "capability_enabled", "operation_succeeded"),
    [
        (
            {
                "schema_version": 1,
                "operation_id": str(uuid4()),
                "stop_reason": "end_turn",
            },
            False,
            True,
        ),
        (
            {
                "schema_version": 1,
                "operation_id": str(uuid4()),
                "stop_reason": "end_turn",
            },
            True,
            False,
        ),
        (
            {
                "schema_version": 1,
                "operation_id": str(uuid4()),
                "stop_reason": "end_turn",
            },
            True,
            True,
        ),
    ],
)
def test_initial_turn_does_not_complete_without_the_bound_contract(
    payload: dict[str, object],
    capability_enabled: bool,
    operation_succeeded: bool,
) -> None:
    operation_id = uuid4()
    capability_snapshot = FakeWorkerAdapter(
        turn_completion_enabled=capability_enabled
    ).probe({})

    assert (
        decide_completion(
            policy=ExecutionCompletionPolicy.INITIAL_TURN,
            capability_snapshot=capability_snapshot,
            events=(_event("turn.completed", 2, payload),),
            initial_operation_id=operation_id,
            initial_operation_succeeded=operation_succeeded,
        )
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "operation_id": str(uuid4()), "stop_reason": "end_turn"},
        {"schema_version": 1, "operation_id": "not-a-uuid", "stop_reason": "end_turn"},
        {"schema_version": 1, "operation_id": str(uuid4()), "stop_reason": "tool_use"},
    ],
)
def test_malformed_turn_protocol_raises_instead_of_guessing(
    payload: dict[str, object],
) -> None:
    with pytest.raises(CompletionProtocolError) as caught:
        decide_completion(
            policy=ExecutionCompletionPolicy.INITIAL_TURN,
            capability_snapshot=FakeWorkerAdapter(
                turn_completion_enabled=True
            ).probe({}),
            events=(_event("turn.completed", 2, payload),),
            initial_operation_id=uuid4(),
            initial_operation_succeeded=True,
        )

    assert caught.value.code == "completion_protocol_invalid"


def test_user_input_operation_does_not_complete_the_execution() -> None:
    initial_operation_id = uuid4()
    assert (
        decide_completion(
            policy=ExecutionCompletionPolicy.INITIAL_TURN,
            capability_snapshot=FakeWorkerAdapter(
                turn_completion_enabled=True
            ).probe({}),
            events=(
                _event(
                    "turn.completed",
                    2,
                    {
                        "schema_version": 1,
                        "operation_id": str(uuid4()),
                        "stop_reason": "end_turn",
                    },
                ),
            ),
            initial_operation_id=initial_operation_id,
            initial_operation_succeeded=True,
        )
        is None
    )


@pytest.mark.parametrize("failure_code", ["process_exited", "protocol_failed"])
def test_session_failure_returns_a_bounded_failure_code(failure_code: str) -> None:
    decision = decide_completion(
        policy=ExecutionCompletionPolicy.EXPLICIT,
        capability_snapshot=FakeWorkerAdapter().probe({}),
        events=(_event("session.failed", 4, {"failure_code": failure_code}),),
        initial_operation_id=uuid4(),
        initial_operation_succeeded=False,
    )

    assert decision is not None
    assert decision.outcome == "failed"
    assert decision.failure_code == failure_code


def test_unknown_session_failure_code_is_normalized() -> None:
    decision = decide_completion(
        policy=ExecutionCompletionPolicy.EXPLICIT,
        capability_snapshot=FakeWorkerAdapter().probe({}),
        events=(_event("session.failed", 4, {"failure_code": "provider-secret"}),),
        initial_operation_id=uuid4(),
        initial_operation_succeeded=False,
    )

    assert decision is not None
    assert decision.failure_code == "worker_session_failed"


def test_lowest_terminal_sequence_wins_during_replay() -> None:
    decision = decide_completion(
        policy=ExecutionCompletionPolicy.EXPLICIT,
        capability_snapshot=FakeWorkerAdapter().probe({}),
        events=(
            _event("session.failed", 5, {"failure_code": "provider_failed"}),
            _event("session.succeeded", 3),
        ),
        initial_operation_id=uuid4(),
        initial_operation_succeeded=False,
    )

    assert decision is not None
    assert decision.outcome == "succeeded"
    assert decision.sequence == 3


def test_completion_decision_accepts_real_uuid_values() -> None:
    operation_id = UUID(str(uuid4()))
    event = _event(
        "turn.completed",
        1,
        {
            "schema_version": 1,
            "operation_id": str(operation_id),
            "stop_reason": "end_turn",
        },
    )

    decision = decide_completion(
        policy=ExecutionCompletionPolicy.INITIAL_TURN,
        capability_snapshot=FakeWorkerAdapter(turn_completion_enabled=True).probe({}),
        events=(event,),
        initial_operation_id=operation_id,
        initial_operation_succeeded=True,
    )

    assert decision is not None
