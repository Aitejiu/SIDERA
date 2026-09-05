from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.collaboration_projection import (
    CollaborationProjectionAssembler,
    CollaborationRequestFact,
    CollaborationWorkerPlacementFact,
)
from sidera_control_plane.domain.collaboration_runtime import (
    ReleaseExecutionRequestIntentV2,
    RuntimeDecisionOutcome,
    RuntimeDecisionV2,
    RuntimeEnvelopeError,
    RuntimeProjectionV1,
    RuntimeProjectionV2,
    RuntimeResultInputEvaluation,
    canonical_json_bytes,
    parse_runtime_decision_v1,
    parse_runtime_decision_v2,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.input_selections import (
    InputSelectionGroupV1,
    InputSelectionSourceV1,
)
from sidera_control_plane.domain.modules import (
    COLLABORATION_INPUT_GROUPS_STRATEGY_ID,
)
from sidera_control_plane.domain.result_inputs import InputGroupV1

from .result_input_fixtures import group_mapping, projection_v2
from .test_collaboration_projection import _facts
from .test_collaboration_runtime_contracts import projection_fixture

OPERATION_ID = UUID("71000000-0000-0000-0000-000000000001")
SUMMARY_REQUEST_ID = UUID("71000000-0000-0000-0000-000000000002")
SUMMARY_WORKER_ITEM_ID = UUID("71000000-0000-0000-0000-000000000003")
SUMMARY_SUBTASK_ID = UUID("71000000-0000-0000-0000-000000000004")


def _selected_group(projection: RuntimeProjectionV2) -> InputSelectionGroupV1:
    raw_groups = projection.policy["input_groups"]
    assert isinstance(raw_groups, tuple)
    group = InputGroupV1.from_mapping(raw_groups[0])
    ready = projection.result_input_evaluations[0].evaluation.ready_sources
    priorities = {item.worker_item_key: item.priority for item in group.candidates}
    sources = tuple(
        InputSelectionSourceV1(
            worker_item_key=source.worker_item_key,
            worker_item_id=source.worker_item_id,
            subtask_path=source.subtask_path,
            effective_request_id=source.effective_request_id,
            allowed_lineage_ids=source.allowed_lineage_ids,
            effective_lineage_id=source.allowed_lineage_ids[-1],
            effective_execution_id=source.effective_execution_id,
            attempt=source.attempt,
            inheritance_refs=source.inheritance_refs,
            priority=priorities[source.worker_item_key],
            source_basis_hash=source.source_basis_hash,
            artifacts=source.artifacts,
            evidence=source.evidence,
        )
        for source in ready
        if source.worker_item_id is not None
        and source.effective_request_id is not None
        and source.effective_execution_id is not None
        and source.attempt is not None
        and source.source_basis_hash is not None
    )
    return InputSelectionGroupV1(
        group_key=group.group_key,
        rule_hash=sha256(canonical_json_bytes(group.to_mapping())).hexdigest(),
        sources=sources,
    )


def _decision(projection: RuntimeProjectionV2) -> RuntimeDecisionV2:
    request = projection.result_input_evaluations[0].consumer_execution_request_id
    return RuntimeDecisionV2(
        schema_version=2,
        operation_id=OPERATION_ID,
        run_id=projection.run_id,
        projection_id=projection.projection_id,
        projection_hash=projection.content_hash,
        outcome=RuntimeDecisionOutcome.INTENTS,
        reason_code="input_groups_satisfied",
        summary="release the summary worker",
        strategy_state={},
        intents=(
            ReleaseExecutionRequestIntentV2(
                ordinal=0,
                target_execution_request_id=request,
                target_request_version=1,
                reason_code="input_groups_satisfied",
                summary="use the frozen research sources",
                idempotency_key=f"release:{request}",
                input_group_selections=(_selected_group(projection),),
            ),
        ),
    )


def _assembler_v2_facts():
    facts = _facts()
    source_request = replace(
        facts.requests[0],
        status="materialized",
        expected_artifact_refs=("artifact_type:report",),
        expected_evidence_refs=("acceptance:evidence.output",),
    )
    consumer_request = CollaborationRequestFact(
        id=SUMMARY_REQUEST_ID,
        subtask_key="summary",
        worker_item_key="summary",
        subtask_task_id=SUMMARY_SUBTASK_ID,
        worker_item_id=SUMMARY_WORKER_ITEM_ID,
        version=1,
        status="blocked",
        collaboration_gate_mode="release_required",
        gate_reason="awaiting_runtime",
        execution_lineage_id=None,
        first_execution_id=None,
        expected_artifact_refs=(),
        expected_evidence_refs=(),
    )
    raw_group = group_mapping()
    raw_group["candidates"] = [
        {"worker_item_key": source_request.worker_item_key, "priority": 1}
    ]
    raw_group["minimum_count"] = 1
    raw_group["requirements"] = {
        "execution_status": "succeeded",
        "required_artifact_refs": ["artifact_type:report"],
        "required_evidence_refs": ["acceptance:evidence.output"],
        "evidence_status": "candidate",
    }
    capability = replace(
        facts.plan_head.runtime_capability_snapshot,
        projection_schema_versions=(1, 2),
        decision_schema_versions=(1, 2),
        strategy_ids=("all_required", COLLABORATION_INPUT_GROUPS_STRATEGY_ID),
    )
    return replace(
        facts,
        plan_head=replace(
            facts.plan_head,
            runtime_capability_snapshot=capability,
            policy_snapshot={
                "schema_version": 2,
                "default_policy": {
                    "enabled": True,
                    "strategy_id": "all_required",
                },
                "subtask_overrides": [],
                "input_groups": [raw_group],
            },
        ),
        requests=(source_request, consumer_request),
        executions=(replace(facts.executions[0], status="succeeded"),),
        evidence=(
            replace(
                facts.evidence[0],
                acceptance_criterion_ref="acceptance:evidence.output",
            ),
        ),
        worker_placements=(
            CollaborationWorkerPlacementFact(
                worker_item_key=source_request.worker_item_key,
                worker_item_id=source_request.worker_item_id,
                subtask_key=source_request.subtask_key,
                subtask_task_id=source_request.subtask_task_id,
                subtask_path=(source_request.subtask_key,),
            ),
            CollaborationWorkerPlacementFact(
                worker_item_key=consumer_request.worker_item_key,
                worker_item_id=consumer_request.worker_item_id,
                subtask_key=consumer_request.subtask_key,
                subtask_task_id=consumer_request.subtask_task_id,
                subtask_path=(consumer_request.subtask_key,),
            ),
        ),
    )


def test_v2_projection_round_trips_without_result_bodies() -> None:
    projection = projection_v2(ready_keys=("b", "c"))
    encoded = projection.canonical_bytes()
    restored = RuntimeProjectionV2.from_mapping(
        projection.to_mapping(), created_at=projection.created_at
    )

    assert projection.schema_version == 2
    assert b"body_text" not in encoded
    assert b"storage_ref" not in encoded
    assert restored == projection
    assert restored.result_input_evaluations[0].evaluation.ready_count == 2


def test_v2_projection_requires_exact_policy_and_evaluation_fields() -> None:
    projection = projection_v2(ready_keys=("a", "b"))
    unknown = projection.to_mapping()
    unknown["provider_trace"] = "forbidden"
    with pytest.raises(RuntimeEnvelopeError, match="unknown projection fields"):
        RuntimeProjectionV2.from_mapping(unknown, created_at=projection.created_at)

    nested = projection.to_mapping()
    evaluations = nested["result_input_evaluations"]
    assert isinstance(evaluations, list)
    evaluation = evaluations[0]
    assert isinstance(evaluation, dict)
    evaluation["body_text"] = "forbidden"
    with pytest.raises(RuntimeEnvelopeError):
        RuntimeProjectionV2.from_mapping(nested, created_at=projection.created_at)


def test_v2_projection_rejects_a_group_evaluation_for_another_target() -> None:
    projection = projection_v2(ready_keys=("a", "b"))
    evaluation = projection.result_input_evaluations[0]
    with pytest.raises(RuntimeEnvelopeError, match="consumer execution request"):
        replace(
            projection,
            result_input_evaluations=(
                RuntimeResultInputEvaluation(
                    consumer_execution_request_id=uuid4(),
                    group_key=evaluation.group_key,
                    evaluation=evaluation.evaluation,
                ),
            ),
        )


def test_v2_decision_round_trips_fixed_source_descriptors() -> None:
    projection = projection_v2(ready_keys=("b", "c"))
    decision = _decision(projection)
    parsed = parse_runtime_decision_v2(
        decision.canonical_bytes(),
        max_bytes=65_536,
        max_intents=128,
        max_state_bytes=16_384,
    )

    assert parsed == decision
    assert [
        source.worker_item_key
        for source in parsed.intents[0].input_group_selections[0].sources
    ] == ["b", "c"]


@pytest.mark.parametrize("forbidden_field", ("body_text", "storage_ref", "url"))
def test_v2_decision_rejects_runtime_supplied_file_fields(
    forbidden_field: str,
) -> None:
    projection = projection_v2(ready_keys=("a", "b"))
    raw = deepcopy(_decision(projection).to_mapping())
    intents = raw["intents"]
    assert isinstance(intents, list)
    intent = intents[0]
    assert isinstance(intent, dict)
    groups = intent["input_group_selections"]
    assert isinstance(groups, list)
    group = groups[0]
    assert isinstance(group, dict)
    sources = group["sources"]
    assert isinstance(sources, list)
    source = sources[0]
    assert isinstance(source, dict)
    source[forbidden_field] = "https://runtime.invalid/file"

    with pytest.raises(RuntimeEnvelopeError):
        parse_runtime_decision_v2(
            canonical_json_bytes(raw),
            max_bytes=65_536,
            max_intents=128,
            max_state_bytes=16_384,
        )


def test_v1_and_v2_parsers_do_not_guess_from_fields() -> None:
    projection = projection_v2(ready_keys=("a", "b"))
    v2_bytes = _decision(projection).canonical_bytes()
    with pytest.raises(RuntimeEnvelopeError, match="schema_version"):
        parse_runtime_decision_v1(
            v2_bytes,
            max_bytes=65_536,
            max_intents=128,
            max_state_bytes=16_384,
        )

    v1 = projection_fixture()
    assert isinstance(v1, RuntimeProjectionV1)
    with pytest.raises(RuntimeEnvelopeError, match="schema_version"):
        RuntimeProjectionV2.from_mapping(v1.to_mapping(), created_at=v1.created_at)


def test_v2_response_size_is_checked_before_nested_parsing() -> None:
    projection = projection_v2(ready_keys=("a", "b"))
    with pytest.raises(RuntimeEnvelopeError, match="maximum response size"):
        parse_runtime_decision_v2(
            _decision(projection).canonical_bytes(),
            max_bytes=32,
            max_intents=128,
            max_state_bytes=16_384,
        )


def test_v2_policy_cannot_hide_an_oversized_candidate_group() -> None:
    projection = projection_v2(ready_keys=("a", "b"))
    raw = projection.to_mapping()
    policy = raw["policy"]
    assert isinstance(policy, dict)
    groups = policy["input_groups"]
    assert isinstance(groups, list)
    oversized = group_mapping()
    oversized["candidates"] = [
        {"worker_item_key": f"worker-{index}", "priority": index + 1}
        for index in range(33)
    ]
    oversized["minimum_count"] = 2
    groups[0] = oversized

    with pytest.raises(RuntimeEnvelopeError):
        RuntimeProjectionV2.from_mapping(raw, created_at=projection.created_at)


def test_v2_projection_limits_groups_per_consumer() -> None:
    projection = projection_v2(ready_keys=("a", "b"))
    raw = projection.to_mapping()
    policy = raw["policy"]
    evaluations = raw["result_input_evaluations"]
    assert isinstance(policy, dict)
    groups = policy["input_groups"]
    assert isinstance(groups, list)
    assert isinstance(evaluations, list)
    original_group = groups[0]
    original_evaluation = evaluations[0]
    assert isinstance(original_group, dict)
    assert isinstance(original_evaluation, dict)
    groups.clear()
    evaluations.clear()
    for index in range(9):
        group = deepcopy(original_group)
        evaluation = deepcopy(original_evaluation)
        group_key = f"research-{index}"
        group["group_key"] = group_key
        evaluation["group_key"] = group_key
        nested = evaluation["evaluation"]
        assert isinstance(nested, dict)
        nested["group_key"] = group_key
        groups.append(group)
        evaluations.append(evaluation)

    with pytest.raises(RuntimeEnvelopeError, match="too many input groups"):
        RuntimeProjectionV2.from_mapping(raw, created_at=projection.created_at)


def test_assembler_builds_v2_evaluations_from_authoritative_source_facts() -> None:
    projection = CollaborationProjectionAssembler().assemble(
        _assembler_v2_facts(), fact_cursor=42
    )

    assert isinstance(projection, RuntimeProjectionV2)
    assert projection.schema_version == 2
    assert len(projection.result_input_evaluations) == 1
    result = projection.result_input_evaluations[0]
    assert result.consumer_execution_request_id == SUMMARY_REQUEST_ID
    assert result.evaluation.ready_count == 1
    assert result.evaluation.ready_sources[0].worker_item_key == "worker-a"
    assert "result_inputs" in projection.fingerprints


def test_assembler_rejects_v2_policy_without_complete_pinned_capability() -> None:
    facts = _assembler_v2_facts()
    capability = replace(
        facts.plan_head.runtime_capability_snapshot,
        decision_schema_versions=(1,),
    )

    with pytest.raises(ValidationError, match="input-group capable"):
        CollaborationProjectionAssembler().assemble(
            replace(
                facts,
                plan_head=replace(
                    facts.plan_head,
                    runtime_capability_snapshot=capability,
                ),
            ),
            fact_cursor=42,
        )
