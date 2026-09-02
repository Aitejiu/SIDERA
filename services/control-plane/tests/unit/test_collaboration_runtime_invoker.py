from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest

from sidera_control_plane.application.collaboration_reconciler import (
    PreparedCollaborationEvaluation,
)
from sidera_control_plane.application.collaboration_runtime_invoker import (
    CollaborationRuntimeInvoker,
    RuntimeInvocationFailure,
    RuntimeInvocationResult,
)
from sidera_control_plane.application.collaboration_runtime_resolver import (
    ResolvedCollaborationRuntime,
    RuntimeResolutionError,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationEvaluation,
    CollaborationPolicyEntry,
    CollaborationRun,
    CollaborationStrategy,
)
from sidera_control_plane.domain.collaboration_runtime import (
    RuntimeDecisionOutcome,
    RuntimeDecisionV1,
    RuntimeProjectionV1,
)
from sidera_control_plane.domain.modules import (
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    StabilityLevel,
)

WORKSPACE_ID = UUID("21000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("31000000-0000-0000-0000-000000000001")
PLAN_SNAPSHOT_ID = UUID("32000000-0000-0000-0000-000000000001")
RUN_ID = UUID("33000000-0000-0000-0000-000000000001")
EVALUATION_ID = UUID("34000000-0000-0000-0000-000000000001")
PROJECTION_ID = UUID("35000000-0000-0000-0000-000000000001")
OPERATION_ID = UUID("36000000-0000-0000-0000-000000000001")
DEFINITION_ID = UUID("37000000-0000-0000-0000-000000000001")
INSTANCE_ID = UUID("38000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _capability(
    *,
    max_projection_bytes: int = 262_144,
    max_decision_bytes: int = 65_536,
    max_intents_per_decision: int = 128,
    max_strategy_state_bytes: int = 16_384,
) -> CollaborationRuntimeCapabilityProfile:
    return CollaborationRuntimeCapabilityProfile(
        interface_version="collaboration-runtime.v1",
        projection_schema_versions=(1,),
        decision_schema_versions=(1,),
        strategy_ids=("all_required",),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=max_projection_bytes,
        max_decision_bytes=max_decision_bytes,
        max_intents_per_decision=max_intents_per_decision,
        max_strategy_state_bytes=max_strategy_state_bytes,
        stability=StabilityLevel.STABLE,
    )


def _run(
    *, capability: CollaborationRuntimeCapabilityProfile | None = None
) -> CollaborationRun:
    run = CollaborationRun.create(
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        plan_snapshot_id=PLAN_SNAPSHOT_ID,
        plan_generation=3,
        plan_content_hash="a" * 64,
        runtime_definition_id=DEFINITION_ID,
        runtime_instance_id=INSTANCE_ID,
        runtime_interface_version="collaboration-runtime.v1",
        runtime_config_version=1,
        runtime_capability_snapshot=capability or _capability(),
        policy_snapshot=CollaborationPolicyEntry(
            enabled=True,
            strategy_id=CollaborationStrategy.ALL_REQUIRED,
        ).to_mapping(),
        now=NOW,
    )
    run.activate(now=NOW)
    return run


def _projection(run: CollaborationRun) -> RuntimeProjectionV1:
    return RuntimeProjectionV1(
        schema_version=1,
        projection_id=PROJECTION_ID,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        run_id=run.id,
        plan_snapshot_id=PLAN_SNAPSHOT_ID,
        plan_generation=run.plan_generation,
        plan_content_hash=run.plan_content_hash,
        run_resource_version=run.resource_version,
        fact_cursor=1,
        policy={"mode": "enabled", "default_policy": {"strategy_id": "all_required"}},
        tasks=({"id": str(ROOT_TASK_ID), "title": "Root task"},),
        subtasks=({"key": "subtask-a", "title": "Subtask A"},),
        worker_items=(),
        dependencies=(),
        execution_requests=(),
        executions=(),
        artifacts=(),
        evidence=(),
        releases=(),
        previous_strategy_state={},
        fingerprints={},
        created_at=NOW,
    )


def _prepared(
    *,
    run: CollaborationRun | None = None,
    lease_expires_at: datetime = NOW + timedelta(minutes=5),
) -> PreparedCollaborationEvaluation:
    selected_run = run or _run()
    evaluation = CollaborationEvaluation.create(
        evaluation_id=EVALUATION_ID,
        workspace_id=WORKSPACE_ID,
        run_id=selected_run.id,
        projection_snapshot_id=PROJECTION_ID,
        operation_id=OPERATION_ID,
        now=NOW,
    )
    evaluation.claim(
        owner_id="collaboration-worker-a",
        lease_expires_at=lease_expires_at,
        now=NOW,
    )
    return PreparedCollaborationEvaluation(
        run=selected_run,
        evaluation=evaluation,
        projection=_projection(selected_run),
        operation_id=OPERATION_ID,
        lease_owner="collaboration-worker-a",
        lease_expires_at=lease_expires_at,
    )


def _decision(
    prepared: PreparedCollaborationEvaluation, **changes: Any
) -> RuntimeDecisionV1:
    values: dict[str, object] = {
        "schema_version": 1,
        "operation_id": prepared.operation_id,
        "run_id": prepared.run.id,
        "projection_id": prepared.projection.projection_id,
        "projection_hash": prepared.projection.content_hash,
        "outcome": RuntimeDecisionOutcome.NO_CHANGE,
        "reason_code": "no_eligible_requests",
        "summary": "no release-required request is eligible",
        "strategy_state": {},
        "intents": (),
    }
    values.update(changes)
    return RuntimeDecisionV1(**values)


class StaticResolver:
    def __init__(
        self,
        runtime: object,
        *,
        error: RuntimeResolutionError | None = None,
    ) -> None:
        self.runtime = runtime
        self.error = error
        self.calls: list[CollaborationRun] = []

    def resolve(self, run: CollaborationRun) -> ResolvedCollaborationRuntime:
        self.calls.append(run)
        if self.error is not None:
            raise self.error
        return ResolvedCollaborationRuntime(
            runtime=self.runtime,  # type: ignore[arg-type]
            provider="reference",
            interface_version=run.runtime_interface_version,
            config_version=run.runtime_config_version,
        )


class ReturningRuntime:
    def __init__(
        self, decision: object, *, observe: dict[str, object] | None = None
    ) -> None:
        self.decision = decision
        self.observe = observe
        self.operation_ids: list[UUID] = []
        self.deadlines: list[datetime] = []

    def evaluate(
        self,
        projection: RuntimeProjectionV1,
        *,
        operation_id: UUID,
        deadline: datetime,
    ) -> object:
        self.operation_ids.append(operation_id)
        self.deadlines.append(deadline)
        if self.observe is not None:
            assert self.observe["transaction_closed"] is True
        return self.decision


def _invoker(
    resolver: StaticResolver,
    *,
    evaluation_timeout: timedelta = timedelta(seconds=30),
    lease_safety_margin: timedelta = timedelta(seconds=5),
    max_projection_bytes: int = 1_048_576,
    max_decision_bytes: int = 65_536,
    max_diagnostic_bytes: int = 4_096,
    monotonic_values: tuple[float, ...] = (10.0, 10.125),
) -> CollaborationRuntimeInvoker:
    values = iter(monotonic_values)
    return CollaborationRuntimeInvoker(
        runtime_resolver=resolver,
        evaluation_timeout=evaluation_timeout,
        lease_safety_margin=lease_safety_margin,
        max_projection_bytes=max_projection_bytes,
        max_decision_bytes=max_decision_bytes,
        max_diagnostic_bytes=max_diagnostic_bytes,
        monotonic_clock=lambda: next(values),
    )


def test_invocation_occurs_after_prepare_transaction_closed() -> None:
    prepared = _prepared()
    state: dict[str, object] = {"transaction_closed": True}
    runtime = ReturningRuntime(_decision(prepared), observe=state)
    result = _invoker(StaticResolver(runtime)).invoke(prepared, now=NOW)

    assert isinstance(result, RuntimeInvocationResult)
    assert result.decision.operation_id == OPERATION_ID
    assert runtime.operation_ids == [OPERATION_ID]
    assert result.response_bytes == result.decision.canonical_bytes()
    assert result.response_hash == sha256(result.response_bytes).hexdigest()
    assert result.duration_ms == 125


def test_deadline_is_bound_by_lease_safety_margin() -> None:
    prepared = _prepared(lease_expires_at=NOW + timedelta(seconds=9))
    runtime = ReturningRuntime(_decision(prepared))

    result = _invoker(StaticResolver(runtime)).invoke(prepared, now=NOW)

    assert isinstance(result, RuntimeInvocationResult)
    assert runtime.deadlines == [NOW + timedelta(seconds=4)]


@pytest.mark.parametrize(
    ("exception", "code"),
    (
        (TimeoutError("provider deadline exceeded"), "runtime_timeout"),
        (ConnectionError("provider unavailable"), "runtime_unavailable"),
    ),
)
def test_timeout_and_unavailable_are_retryable(exception: Exception, code: str) -> None:
    class RaisingRuntime:
        def evaluate(self, *_args: object, **_kwargs: object) -> object:
            raise exception

    result = _invoker(StaticResolver(RaisingRuntime())).invoke(_prepared(), now=NOW)

    assert isinstance(result, RuntimeInvocationFailure)
    assert result.code == code
    assert result.retryable is True
    assert result.response_hash is None


def test_echo_mismatch_is_a_non_retryable_invalid_output() -> None:
    prepared = _prepared()
    runtime = ReturningRuntime(
        _decision(prepared, run_id=UUID("39000000-0000-0000-0000-000000000001"))
    )

    result = _invoker(StaticResolver(runtime)).invoke(prepared, now=NOW)

    assert isinstance(result, RuntimeInvocationFailure)
    assert result.code == "runtime_output_invalid"
    assert result.retryable is False
    assert result.response_hash is not None


def test_invalid_output_is_hashed_but_never_copied_to_diagnostic() -> None:
    prepared = _prepared()
    raw = b'{"unknown": "secret response body"}'
    runtime = ReturningRuntime(raw)

    result = _invoker(StaticResolver(runtime)).invoke(prepared, now=NOW)

    assert isinstance(result, RuntimeInvocationFailure)
    assert result.code == "runtime_output_invalid"
    assert result.response_hash == sha256(raw).hexdigest()
    assert "secret response body" not in result.diagnostic_summary


def test_projection_overflow_does_not_call_runtime() -> None:
    prepared = _prepared()
    runtime = ReturningRuntime(_decision(prepared))
    resolver = StaticResolver(runtime)

    result = _invoker(
        resolver,
        max_projection_bytes=1,
    ).invoke(prepared, now=NOW)

    assert isinstance(result, RuntimeInvocationFailure)
    assert result.code == "projection_too_large"
    assert result.retryable is False
    assert resolver.calls == []
    assert runtime.operation_ids == []


def test_runtime_returned_after_deadline_is_retryable_timeout() -> None:
    prepared = _prepared()
    runtime = ReturningRuntime(_decision(prepared))

    result = _invoker(
        StaticResolver(runtime),
        evaluation_timeout=timedelta(seconds=1),
        lease_safety_margin=timedelta(0),
        monotonic_values=(10.0, 11.0),
    ).invoke(prepared, now=NOW)

    assert isinstance(result, RuntimeInvocationFailure)
    assert result.code == "runtime_timeout"
    assert result.retryable is True
    assert result.duration_ms == 1_000


@pytest.mark.parametrize(
    ("resolution_code", "expected_code"),
    (
        ("runtime_config_mismatch", "runtime_config_mismatch"),
        ("runtime_capability_unsupported", "unsupported_runtime_capability"),
        ("runtime_provider_unavailable", "runtime_unavailable"),
    ),
)
def test_resolution_failures_are_classified_without_fallback(
    resolution_code: str, expected_code: str
) -> None:
    resolver = StaticResolver(
        object(),
        error=RuntimeResolutionError(resolution_code, "secret=sk-test-provider-key"),
    )

    result = _invoker(resolver).invoke(_prepared(), now=NOW)

    assert isinstance(result, RuntimeInvocationFailure)
    assert result.code == expected_code
    assert result.retryable is (expected_code == "runtime_unavailable")
    assert "sk-test-provider-key" not in result.diagnostic_summary


def test_diagnostic_is_bounded_and_control_characters_are_removed() -> None:
    class RaisingRuntime:
        def evaluate(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("token=sk-1234567890\x00\n" + "x" * 100)

    result = _invoker(
        StaticResolver(RaisingRuntime()),
        max_diagnostic_bytes=24,
    ).invoke(_prepared(), now=NOW)

    assert isinstance(result, RuntimeInvocationFailure)
    assert len(result.diagnostic_summary.encode("utf-8")) <= 24
    assert "\x00" not in result.diagnostic_summary
    assert "sk-1234567890" not in result.diagnostic_summary
