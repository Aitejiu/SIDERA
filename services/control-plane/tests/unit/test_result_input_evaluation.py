from dataclasses import replace
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from sidera_control_plane.application.result_input_evaluation import (
    ResultInputEvaluator,
)
from sidera_control_plane.application.result_input_sources import (
    resolve_result_input_source,
)
from sidera_control_plane.domain.artifacts import ArtifactType
from sidera_control_plane.domain.result_inputs import (
    InputGroupV1,
    ResultInputArtifact,
)

from .result_input_fixtures import (
    effective_source_facts,
    group_mapping,
    result_source,
)


def _group() -> InputGroupV1:
    return InputGroupV1.from_mapping(group_mapping())


def _artifact(key: str, artifact_type: ArtifactType) -> ResultInputArtifact:
    body = f"artifact:{key}".encode()
    return ResultInputArtifact(
        artifact_id=uuid5(NAMESPACE_URL, f"sidera:r6.3.4:test-artifact:{key}"),
        artifact_type=artifact_type,
        content_hash=sha256(body).hexdigest(),
        size_bytes=len(body),
        media_type="text/plain",
    )


def test_at_least_group_counts_worker_items_not_files() -> None:
    a = result_source("a")
    second_report = _artifact("a-second-report", ArtifactType.REPORT)
    a = replace(a, artifacts=(*a.artifacts, second_report))

    result = ResultInputEvaluator().evaluate_group(
        group=_group(),
        sources=(a, result_source("b"), result_source("c", execution_status="failed")),
    )

    assert result.required_count == 2
    assert result.ready_count == 2
    assert result.satisfied is True
    assert result.reason_code is None
    assert [item.worker_item_key for item in result.ready_sources] == ["a", "b"]
    assert len(result.ready_sources[0].artifacts) == 2


def test_unknown_outcome_is_not_quorum_unreachable() -> None:
    result = ResultInputEvaluator().evaluate_group(
        group=_group(),
        sources=(
            result_source("a"),
            result_source(
                "b", execution_status="running", outcome_unknown=True
            ),
            result_source("c", execution_status="failed"),
        ),
    )

    assert result.ready_count == 1
    assert result.satisfied is False
    assert result.reason_code == "input_source_outcome_unknown"


def test_retry_budget_without_a_new_attempt_keeps_group_waiting() -> None:
    result = ResultInputEvaluator().evaluate_group(
        group=_group(),
        sources=(
            result_source("a"),
            result_source("b", execution_status="failed", retry_possible=True),
            result_source("c", execution_status="failed"),
        ),
    )

    assert result.satisfied is False
    assert result.reason_code == "input_group_waiting"
    assert result.candidate_states[1].retry_possible is True


def test_quorum_is_unreachable_only_when_known_possible_count_is_too_small() -> None:
    result = ResultInputEvaluator().evaluate_group(
        group=_group(),
        sources=(
            result_source("a"),
            result_source("b", execution_status="failed"),
            result_source("c", execution_status="canceled"),
        ),
    )

    assert result.ready_count == 1
    assert result.satisfied is False
    assert result.reason_code == "input_quorum_unreachable"


def test_all_mode_requires_every_configured_candidate() -> None:
    raw = group_mapping()
    raw["mode"] = "all"
    raw["minimum_count"] = None
    group = InputGroupV1.from_mapping(raw)

    result = ResultInputEvaluator().evaluate_group(
        group=group,
        sources=(
            result_source("a"),
            result_source("b"),
            result_source("c", execution_status="failed"),
        ),
    )

    assert result.required_count == 3
    assert result.ready_count == 2
    assert result.reason_code == "input_quorum_unreachable"


def test_succeeded_source_without_required_files_is_not_ready() -> None:
    source = replace(result_source("a"), artifacts=(), evidence=())
    result = ResultInputEvaluator().evaluate_group(
        group=_group(), sources=(source, result_source("b", execution_status="failed"))
    )
    state = result.candidate_states[0]

    assert state.ready is False
    assert state.missing_artifact_refs == ("artifact_type:report",)
    assert state.missing_evidence_refs == ("acceptance:research-complete",)


def test_failed_source_with_matching_files_is_not_ready() -> None:
    result = ResultInputEvaluator().evaluate_group(
        group=_group(), sources=(result_source("a", execution_status="failed"),)
    )

    assert result.candidate_states[0].ready is False
    assert result.ready_sources == ()


def test_missing_required_evidence_is_reported() -> None:
    source = replace(result_source("a"), evidence=())
    result = ResultInputEvaluator().evaluate_group(group=_group(), sources=(source,))

    assert result.candidate_states[0].missing_evidence_refs == (
        "acceptance:research-complete",
    )


def test_evidence_referencing_an_out_of_scope_artifact_is_rejected() -> None:
    source = result_source("a")
    foreign_artifact_id = uuid5(NAMESPACE_URL, "sidera:r6.3.4:foreign-artifact")
    evidence = replace(source.evidence[0], artifact_ids=(foreign_artifact_id,))
    source = replace(source, evidence=(evidence,))

    result = ResultInputEvaluator().evaluate_group(group=_group(), sources=(source,))
    state = result.candidate_states[0]

    assert state.ready is False
    assert state.missing_evidence_refs == ("acceptance:research-complete",)
    assert state.reason_code == "input_evidence_artifact_out_of_scope"


def test_evidence_artifact_closure_is_included_but_unrelated_files_are_not() -> None:
    source = result_source("a")
    report_id = source.artifacts[0].artifact_id
    evidence_build = _artifact("a-evidence-build", ArtifactType.BUILD)
    unrelated_diff = _artifact("a-unrelated-diff", ArtifactType.DIFF)
    evidence = replace(
        source.evidence[0],
        artifact_ids=(report_id, evidence_build.artifact_id),
    )
    source = replace(
        source,
        artifacts=(*source.artifacts, evidence_build, unrelated_diff),
        evidence=(evidence,),
    )

    result = ResultInputEvaluator().evaluate_group(group=_group(), sources=(source,))
    evaluated = result.ready_sources[0]

    assert {item.artifact_id for item in evaluated.artifacts} == {
        report_id,
        evidence_build.artifact_id,
    }
    assert unrelated_diff.artifact_id not in {
        item.artifact_id for item in evaluated.artifacts
    }


def test_basis_hash_ignores_unselected_files_and_other_candidate_progress() -> None:
    source = result_source("a")
    with_unselected = replace(
        source,
        artifacts=(*source.artifacts, _artifact("unselected", ArtifactType.DIFF)),
        source_basis_hash="f" * 64,
    )
    evaluator = ResultInputEvaluator()

    baseline = evaluator.evaluate_group(group=_group(), sources=(source,))
    changed = evaluator.evaluate_group(
        group=_group(),
        sources=(with_unselected, result_source("b", execution_status="running")),
    )

    assert baseline.candidate_states[0].source_basis_hash is not None
    assert (
        baseline.candidate_states[0].source_basis_hash
        == changed.candidate_states[0].source_basis_hash
    )
    assert changed.ready_sources[0].source_basis_hash != "f" * 64


def test_basis_hash_changes_with_requirements() -> None:
    source = result_source("a")
    raw = group_mapping()
    requirements = raw["requirements"]
    assert isinstance(requirements, dict)
    requirements["required_evidence_refs"] = []
    without_evidence = InputGroupV1.from_mapping(raw)
    evaluator = ResultInputEvaluator()

    first = evaluator.evaluate_group(group=_group(), sources=(source,))
    second = evaluator.evaluate_group(group=without_evidence, sources=(source,))

    assert (
        first.ready_sources[0].source_basis_hash
        != second.ready_sources[0].source_basis_hash
    )


def test_authorized_continuation_does_not_mix_old_attempt_artifacts() -> None:
    facts = effective_source_facts(request_case="authorized_continuation")
    base_execution_id = facts.requests[0].execution_lineage_id
    assert base_execution_id is not None
    facts = replace(
        facts,
        executions=tuple(
            replace(item, status="failed")
            if item.id == base_execution_id
            else item
            for item in facts.executions
        ),
    )
    source = resolve_result_input_source(worker_item_key="a", facts=facts)
    result = ResultInputEvaluator().evaluate_group(group=_group(), sources=(source,))

    assert result.candidate_states[0].ready is True
    assert {item.artifact_id for item in result.ready_sources[0].artifacts} == {
        item.artifact_id for item in source.artifacts
    }
    assert all(
        item.execution_id == source.effective_execution_id
        for item in facts.artifacts
        if item.id in {artifact.artifact_id for artifact in source.artifacts}
    )
    assert all(
        item.execution_id != base_execution_id
        for item in facts.artifacts
        if item.id in {artifact.artifact_id for artifact in source.artifacts}
    )


def test_explicit_inheritance_is_eligible_but_revoked_source_is_unknown() -> None:
    inherited = replace(
        result_source("a"),
        inheritance_refs=("handoff:source:target",),
    )
    revoked = replace(
        inherited,
        reason_code="input_source_ambiguous",
        outcome_unknown=True,
    )
    evaluator = ResultInputEvaluator()

    accepted = evaluator.evaluate_group(group=_group(), sources=(inherited,))
    rejected = evaluator.evaluate_group(group=_group(), sources=(revoked,))

    assert accepted.candidate_states[0].ready is True
    assert accepted.ready_sources[0].inheritance_refs == inherited.inheritance_refs
    assert rejected.candidate_states[0].ready is False
    assert rejected.reason_code == "input_source_outcome_unknown"


def test_duplicate_effective_sources_are_ambiguous_without_id_tiebreaking() -> None:
    first = result_source("a")
    second = replace(
        first,
        effective_execution_id=uuid5(
            NAMESPACE_URL, "sidera:r6.3.4:duplicate-execution"
        ),
    )

    result = ResultInputEvaluator().evaluate_group(
        group=_group(), sources=(first, second)
    )

    assert result.candidate_states[0].ready is False
    assert result.candidate_states[0].reason_code == "input_source_ambiguous"
    assert result.reason_code == "input_source_outcome_unknown"


def test_missing_candidate_source_remains_unknown_instead_of_unreachable() -> None:
    result = ResultInputEvaluator().evaluate_group(
        group=_group(), sources=(result_source("a"),)
    )

    assert result.candidate_states[1].worker_item_key == "b"
    assert result.candidate_states[1].outcome_unknown is True
    assert result.reason_code == "input_source_outcome_unknown"


def test_unknown_source_can_recover_into_a_satisfied_group() -> None:
    evaluator = ResultInputEvaluator()
    waiting = evaluator.evaluate_group(
        group=_group(),
        sources=(
            result_source("a"),
            result_source(
                "b", execution_status="running", outcome_unknown=True
            ),
            result_source("c", execution_status="failed"),
        ),
    )
    recovered = evaluator.evaluate_group(
        group=_group(),
        sources=(
            result_source("a"),
            result_source("b", attempt=2),
            result_source("c", execution_status="failed"),
        ),
    )

    assert waiting.reason_code == "input_source_outcome_unknown"
    assert recovered.satisfied is True
    assert recovered.reason_code is None
