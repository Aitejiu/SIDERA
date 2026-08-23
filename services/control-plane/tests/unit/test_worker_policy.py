from datetime import UTC, datetime
from uuid import UUID

from sidera_control_plane.application.worker_policy import (
    AssignmentDecision,
    LowestCostWorkerPolicy,
    WorkerCandidate,
)
from sidera_control_plane.domain.execution import Execution


def make_execution(
    *,
    requirements: frozenset[str] = frozenset({"code", "python"}),
    pinned_worker_id: UUID | None = None,
    adapter_preferences: tuple[str, ...] = (),
) -> Execution:
    execution_id = UUID("00000000-0000-0000-0000-000000000001")
    return Execution.create(
        execution_id=execution_id,
        workspace_id=UUID("00000000-0000-0000-0000-000000000010"),
        task_id=UUID("00000000-0000-0000-0000-000000000020"),
        lineage_id=execution_id,
        attempt=1,
        max_attempts=3,
        purpose="policy test",
        capability_requirements=requirements,
        pinned_worker_id=pinned_worker_id,
        not_before=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        adapter_preferences=adapter_preferences,
    )


def candidate(
    worker_id: str,
    *,
    adapter_type: str = "fake",
    capabilities: set[str],
    startup: int,
    load: int,
    capacity: int = 1,
    cost: int,
) -> WorkerCandidate:
    return WorkerCandidate(
        id=UUID(worker_id),
        adapter_type=adapter_type,
        capabilities=frozenset(capabilities),
        capacity=capacity,
        current_load=load,
        cost_per_hour_microunits=cost,
        startup_latency_ms=startup,
    )


def test_policy_orders_by_closeness_latency_load_cost_and_uuid() -> None:
    execution = make_execution()
    exact = candidate(
        "00000000-0000-0000-0000-000000000002",
        capabilities={"code", "python"},
        startup=50,
        load=0,
        cost=900,
    )
    extra = candidate(
        "00000000-0000-0000-0000-000000000003",
        capabilities={"code", "python", "review"},
        startup=1,
        load=0,
        cost=1,
    )

    decision = LowestCostWorkerPolicy().select(execution, (extra, exact))

    assert decision == AssignmentDecision(
        worker_id=exact.id,
        reason={
            "policy": "lowest-cost-v1",
            "eligible_count": 2,
            "required_capabilities": ["code", "python"],
            "selected_adapter_type": "fake",
            "adapter_preference_rank": 0,
            "adapter_preference_fallback": False,
            "pin_applied": False,
            "selected_score": {
                "extra_capabilities": 0,
                "startup_latency_ms": 50,
                "load_ppm": 0,
                "cost_per_hour_microunits": 900,
            },
        },
    )


def test_policy_prefers_startup_then_integer_load_ratio_then_cost() -> None:
    execution = make_execution()
    fast = candidate(
        "00000000-0000-0000-0000-000000000002",
        capabilities={"code", "python"},
        startup=10,
        load=1,
        capacity=3,
        cost=900,
    )
    slow = candidate(
        "00000000-0000-0000-0000-000000000003",
        capabilities={"code", "python"},
        startup=20,
        load=0,
        cost=1,
    )
    fast_decision = LowestCostWorkerPolicy().select(execution, (fast, slow))
    assert fast_decision is not None
    assert fast_decision.worker_id == fast.id

    low_load = candidate(
        "00000000-0000-0000-0000-000000000004",
        capabilities={"code", "python"},
        startup=10,
        load=0,
        capacity=1,
        cost=900,
    )
    load_decision = LowestCostWorkerPolicy().select(execution, (fast, low_load))
    assert load_decision is not None
    assert load_decision.worker_id == low_load.id

    low_cost = candidate(
        "00000000-0000-0000-0000-000000000005",
        capabilities={"code", "python"},
        startup=10,
        load=0,
        capacity=1,
        cost=1,
    )
    cost_decision = LowestCostWorkerPolicy().select(execution, (low_load, low_cost))
    assert cost_decision is not None
    assert cost_decision.worker_id == low_cost.id


def test_policy_uses_uuid_tie_break_and_returns_none_without_candidates() -> None:
    execution = make_execution()
    first = candidate(
        "00000000-0000-0000-0000-000000000002",
        capabilities={"code", "python"},
        startup=10,
        load=0,
        cost=1,
    )
    second = candidate(
        "00000000-0000-0000-0000-000000000003",
        capabilities={"code", "python"},
        startup=10,
        load=0,
        cost=1,
    )
    policy = LowestCostWorkerPolicy()

    tie_decision = policy.select(execution, (second, first))
    assert tie_decision is not None
    assert tie_decision.worker_id == first.id
    assert policy.select(execution, ()) is None


def test_policy_accepts_a_pre_filtered_pinned_candidate_tuple() -> None:
    pinned_id = UUID("00000000-0000-0000-0000-000000000003")
    execution = make_execution(pinned_worker_id=pinned_id)
    pinned = candidate(
        str(pinned_id),
        capabilities={"code", "python"},
        startup=50,
        load=0,
        cost=900,
    )

    decision = LowestCostWorkerPolicy().select(execution, (pinned,))

    assert decision is not None
    assert decision.worker_id == pinned_id


def test_policy_prefers_first_adapter_preference_before_cost() -> None:
    execution = make_execution(adapter_preferences=("opencode", "codex"))
    codex = candidate(
        "00000000-0000-0000-0000-000000000002",
        adapter_type="codex",
        capabilities={"code", "python"},
        startup=1,
        load=0,
        cost=1,
    )
    opencode = candidate(
        "00000000-0000-0000-0000-000000000003",
        adapter_type="opencode",
        capabilities={"code", "python"},
        startup=50,
        load=0,
        cost=900,
    )

    decision = LowestCostWorkerPolicy().select(execution, (codex, opencode))

    assert decision is not None
    assert decision.worker_id == opencode.id
    assert decision.reason["selected_adapter_type"] == "opencode"
    assert decision.reason["adapter_preference_rank"] == 0
    assert decision.reason["adapter_preference_fallback"] is False


def test_policy_falls_back_when_no_preferred_adapter_is_ready() -> None:
    execution = make_execution(adapter_preferences=("opencode", "codex"))
    fallback = candidate(
        "00000000-0000-0000-0000-000000000002",
        adapter_type="fake",
        capabilities={"code", "python"},
        startup=1,
        load=0,
        cost=1,
    )

    decision = LowestCostWorkerPolicy().select(execution, (fallback,))

    assert decision is not None
    assert decision.worker_id == fallback.id
    assert decision.reason["adapter_preference_rank"] == 2
    assert decision.reason["adapter_preference_fallback"] is True


def test_policy_marks_a_single_pinned_candidate_without_changing_hard_filters() -> None:
    pinned_id = UUID("00000000-0000-0000-0000-000000000003")
    execution = make_execution(
        pinned_worker_id=pinned_id,
        adapter_preferences=("codex", "opencode"),
    )
    pinned = candidate(
        str(pinned_id),
        adapter_type="opencode",
        capabilities={"code", "python"},
        startup=50,
        load=0,
        cost=900,
    )

    decision = LowestCostWorkerPolicy().select(execution, (pinned,))

    assert decision is not None
    assert decision.worker_id == pinned_id
    assert decision.reason["adapter_preference_rank"] == 1
    assert decision.reason["adapter_preference_fallback"] is False
    assert decision.reason["pin_applied"] is True
