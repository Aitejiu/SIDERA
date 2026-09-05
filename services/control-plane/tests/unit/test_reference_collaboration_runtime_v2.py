from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
    select_plan_order,
)
from sidera_control_plane.application.result_input_evaluation import (
    ResultInputEvaluator,
)
from sidera_control_plane.domain.collaboration_runtime import (
    RuntimeDecisionOutcome,
    RuntimeDecisionV2,
    RuntimeProjectionV2,
    RuntimeResultInputEvaluation,
)
from sidera_control_plane.domain.result_inputs import InputGroupV1

from .result_input_fixtures import group_mapping, projection_v2, result_source

OPERATION_ID = UUID("74000000-0000-0000-0000-000000000001")


def _runtime() -> ReferenceCollaborationRuntime:
    projection = projection_v2(ready_keys=("b", "c"))
    return ReferenceCollaborationRuntime(clock=lambda: projection.created_at)


def _evaluate(projection: RuntimeProjectionV2) -> RuntimeDecisionV2:
    decision = _runtime().evaluate(
        projection,
        operation_id=OPERATION_ID,
        deadline=projection.created_at + timedelta(seconds=5),
    )
    assert isinstance(decision, RuntimeDecisionV2)
    return decision


def _summary_request_id(projection: RuntimeProjectionV2) -> UUID:
    return projection.result_input_evaluations[0].consumer_execution_request_id


def test_threshold_does_not_wait_for_higher_priority_unready_source() -> None:
    group = InputGroupV1.from_mapping(group_mapping())
    evaluation = ResultInputEvaluator().evaluate_group(
        group=group,
        sources=(
            result_source("a", execution_status="running"),
            result_source("b"),
            result_source("c"),
        ),
    )

    assert select_plan_order(group=group, evaluation=evaluation) == ("b", "c")


def test_plan_order_selects_exactly_k_and_all_modes_select_every_source() -> None:
    at_least = InputGroupV1.from_mapping({**group_mapping(), "minimum_count": 1})
    all_mapping = group_mapping()
    all_mapping["mode"] = "all"
    all_mapping["minimum_count"] = None
    all_group = InputGroupV1.from_mapping(all_mapping)
    sources = tuple(result_source(key) for key in ("a", "b", "c"))

    assert select_plan_order(
        group=at_least,
        evaluation=ResultInputEvaluator().evaluate_group(
            group=at_least, sources=sources
        ),
    ) == ("a",)
    assert select_plan_order(
        group=all_group,
        evaluation=ResultInputEvaluator().evaluate_group(
            group=all_group, sources=sources
        ),
    ) == ("a", "b", "c")


def test_runtime_copies_exact_frozen_provenance_for_selected_sources() -> None:
    projection = projection_v2(ready_keys=("b", "c"))

    first = _evaluate(projection)
    repeated = _evaluate(projection)

    assert first == repeated
    assert first.outcome is RuntimeDecisionOutcome.INTENTS
    assert len(first.intents) == 1
    selection = first.intents[0].input_group_selections[0]
    assert tuple(source.worker_item_key for source in selection.sources) == ("b", "c")
    ready = {
        source.worker_item_key: source
        for source in projection.result_input_evaluations[0].evaluation.ready_sources
    }
    assert selection.sources[0].source_basis_hash == ready["b"].source_basis_hash
    assert selection.sources[0].artifacts == ready["b"].artifacts
    assert selection.sources[0].evidence == ready["b"].evidence
    assert "body_text" not in first.canonical_bytes().decode()


def test_multiple_groups_are_an_and_gate() -> None:
    projection = projection_v2(ready_keys=("b", "c"))
    raw_groups = projection.policy["input_groups"]
    assert isinstance(raw_groups, tuple)
    first_group = InputGroupV1.from_mapping(raw_groups[0])
    second_mapping = deepcopy(first_group.to_mapping())
    second_mapping["group_key"] = "review"
    second_mapping["minimum_count"] = 3
    second_group = InputGroupV1.from_mapping(second_mapping)
    second_evaluation = ResultInputEvaluator().evaluate_group(
        group=second_group,
        sources=tuple(
            result_source(
                key,
                execution_status="succeeded" if key in {"b", "c"} else "running",
            )
            for key in ("a", "b", "c")
        ),
    )
    policy = dict(projection.policy)
    policy["input_groups"] = [first_group.to_mapping(), second_group.to_mapping()]
    blocked = replace(
        projection,
        policy=policy,
        result_input_evaluations=(
            projection.result_input_evaluations[0],
            RuntimeResultInputEvaluation(
                consumer_execution_request_id=_summary_request_id(projection),
                group_key=second_group.group_key,
                evaluation=second_evaluation,
            ),
        ),
    )

    decision = _evaluate(blocked)

    assert decision.outcome is RuntimeDecisionOutcome.NO_CHANGE
    assert decision.intents == ()


def test_hard_gate_blocks_a_satisfied_input_group() -> None:
    projection = projection_v2(ready_keys=("b", "c"))
    target_id = _summary_request_id(projection)
    requests = tuple(
        {
            **request,
            "eligible": False,
            "eligibility_reason": "dependencies_blocked",
        }
        if request.get("id") == str(target_id)
        else request
        for request in projection.execution_requests
    )

    decision = _evaluate(replace(projection, execution_requests=requests))

    assert decision.outcome is RuntimeDecisionOutcome.NO_CHANGE


def test_v2_projection_can_release_an_all_required_worker_without_groups() -> None:
    projection = projection_v2(ready_keys=())
    policy = dict(projection.policy)
    policy["input_groups"] = []
    without_groups = replace(
        projection,
        policy=policy,
        result_input_evaluations=(),
    )

    decision = _evaluate(without_groups)

    assert decision.outcome is RuntimeDecisionOutcome.INTENTS
    assert len(decision.intents) == 1
    assert decision.intents[0].input_group_selections == ()


def test_existing_release_is_not_emitted_again() -> None:
    projection = projection_v2(ready_keys=("b", "c"))
    target_id = _summary_request_id(projection)
    release = {
        "id": "74000000-0000-0000-0000-000000000002",
        "run_id": str(projection.run_id),
        "plan_snapshot_id": str(projection.plan_snapshot_id),
        "plan_generation": projection.plan_generation,
        "plan_content_hash": projection.plan_content_hash,
        "execution_request_id": str(target_id),
        "request_version_at_release": 1,
    }

    decision = _evaluate(replace(projection, releases=(release,)))

    assert decision.outcome is RuntimeDecisionOutcome.NO_CHANGE
    assert decision.intents == ()
