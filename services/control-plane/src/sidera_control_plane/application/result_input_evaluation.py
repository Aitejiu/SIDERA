from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID

from sidera_control_plane.domain.artifacts import EvidenceClaimStatus
from sidera_control_plane.domain.collaboration_runtime import canonical_json_bytes
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.execution_requests import DependencyEnforcement
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
    InputSelectionHardInputV1,
    InputSelectionSourceV1,
)
from sidera_control_plane.domain.result_inputs import (
    InputCandidateState,
    InputGroupCandidateV1,
    InputGroupEvaluation,
    InputGroupMode,
    InputGroupRequirementsV1,
    InputGroupV1,
    InputSelectionStrategy,
    ResultInputArtifact,
    ResultInputEvidence,
    ResultInputSource,
)

if TYPE_CHECKING:
    from sidera_control_plane.application.collaboration_projection import (
        CollaborationFacts,
        CollaborationRequestFact,
    )

_POSSIBLY_ACTIVE_STATUSES = frozenset(
    {ExecutionStatus.QUEUED, ExecutionStatus.LEASED, ExecutionStatus.RUNNING}
)


class ResultInputEvaluator:
    """Evaluate configured candidates without reading bodies or mutating facts."""

    def evaluate_group(
        self,
        *,
        group: InputGroupV1,
        sources: tuple[ResultInputSource, ...],
    ) -> InputGroupEvaluation:
        sources_by_key: dict[str, list[ResultInputSource]] = {}
        for source in sources:
            sources_by_key.setdefault(source.worker_item_key, []).append(source)

        states: list[InputCandidateState] = []
        ready_sources: list[ResultInputSource] = []
        for candidate in group.candidates:
            matching = sources_by_key.get(candidate.worker_item_key, [])
            if not matching:
                state = _missing_state(candidate.worker_item_key)
            elif len(matching) > 1:
                state = _ambiguous_state(candidate.worker_item_key)
            else:
                state = _evaluate_source(
                    source=matching[0],
                    requirements=group.requirements,
                )
            states.append(state)
            if state.ready:
                assert state.source is not None
                ready_sources.append(state.source)

        required_count = (
            len(group.candidates)
            if group.mode is InputGroupMode.ALL
            else _minimum_count(group)
        )
        ready_count = len(ready_sources)
        satisfied = ready_count >= required_count
        reason_code = _group_reason(
            states=states,
            ready_count=ready_count,
            required_count=required_count,
            satisfied=satisfied,
        )
        return InputGroupEvaluation(
            group_key=group.group_key,
            required_count=required_count,
            ready_count=ready_count,
            ready_sources=tuple(ready_sources),
            candidate_states=tuple(states),
            satisfied=satisfied,
            reason_code=reason_code,
        )


def validate_selected_sources(
    *,
    selection: CollaborationInputSelectionV1,
    facts: CollaborationFacts,
) -> str | None:
    """Validate an immutable selection without selecting newer candidates."""

    head = facts.plan_head
    if (
        selection.workspace_id != head.workspace_id
        or selection.root_task_id != head.root_task_id
        or selection.run_id != head.run_id
        or selection.plan_snapshot_id != head.plan_snapshot_id
        or selection.plan_generation != head.plan_generation
        or selection.plan_content_hash != head.plan_content_hash
    ):
        return "plan_generation_stale"
    request = next(
        (
            item
            for item in facts.requests
            if item.id == selection.target_execution_request_id
        ),
        None,
    )
    if (
        request is None
        or request.worker_item_id != selection.target_worker_item_id
        or request.version != selection.target_request_version
    ):
        return "input_selection_target_stale"

    try:
        configured = _configured_groups(facts, request=request)
    except (ValidationError, ValueError, TypeError):
        return "input_selection_rule_changed"
    selected_by_key = {item.group_key: item for item in selection.groups}
    configured_by_key = {item.group_key: item for item in configured}
    if set(selected_by_key) != set(configured_by_key):
        return "input_selection_rule_changed"

    from sidera_control_plane.application.result_input_sources import (
        resolve_result_input_source,
    )

    evaluator = ResultInputEvaluator()
    for group_key in sorted(configured_by_key):
        group = configured_by_key[group_key]
        selected = selected_by_key[group_key]
        rule_hash = sha256(canonical_json_bytes(group.to_mapping())).hexdigest()
        if selected.rule_hash != rule_hash:
            return "input_selection_rule_changed"
        candidate_priority = {
            item.worker_item_key: item.priority for item in group.candidates
        }
        evaluation = evaluator.evaluate_group(
            group=group,
            sources=tuple(
                resolve_result_input_source(
                    worker_item_key=item.worker_item_key,
                    facts=facts,
                )
                for item in group.candidates
            ),
        )
        states = {item.worker_item_key: item for item in evaluation.candidate_states}
        for source in selected.sources:
            state = states.get(source.worker_item_key)
            if (
                candidate_priority.get(source.worker_item_key) != source.priority
                or state is None
                or not state.ready
                or state.source is None
            ):
                return "input_selection_source_stale"
            try:
                current = _selection_source(state.source, priority=source.priority)
            except (ValidationError, ValueError):
                return "input_selection_source_stale"
            if current != source:
                return "input_selection_source_stale"

    try:
        hard_inputs = build_hard_input_selections(request=request, facts=facts)
    except (ValidationError, ValueError):
        return "hard_dependency_changed"
    if hard_inputs != selection.hard_inputs:
        return "hard_dependency_changed"
    return None


def build_hard_input_selections(
    *,
    request: CollaborationRequestFact,
    facts: CollaborationFacts,
) -> tuple[InputSelectionHardInputV1, ...]:
    """Build exact hard-dependency selections from authoritative facts."""

    from sidera_control_plane.application.result_input_sources import (
        resolve_result_input_source,
    )

    requests = {item.id: item for item in facts.requests}
    evaluator = ResultInputEvaluator()
    values: list[InputSelectionHardInputV1] = []
    dependencies = sorted(
        (
            item
            for item in facts.dependencies
            if item.downstream_request_id == request.id
            and item.enforcement is DependencyEnforcement.HARD
        ),
        key=lambda item: str(item.upstream_request_id),
    )
    for dependency in dependencies:
        upstream = requests.get(dependency.upstream_request_id)
        if upstream is None:
            raise ValueError("hard input source request is missing")
        requirements = InputGroupRequirementsV1(
            execution_status=ExecutionStatus.SUCCEEDED,
            required_artifact_refs=dependency.required_artifact_refs,
            required_evidence_refs=dependency.required_evidence_refs,
            evidence_status=EvidenceClaimStatus.CANDIDATE,
        )
        group = InputGroupV1(
            group_key=f"hard:{dependency.upstream_request_id}",
            title=f"Hard input {upstream.worker_item_key}",
            consumer_worker_item_key=request.worker_item_key,
            candidates=(
                InputGroupCandidateV1(
                    worker_item_key=upstream.worker_item_key,
                    priority=1,
                ),
            ),
            requirements=requirements,
            mode=InputGroupMode.ALL,
            minimum_count=None,
            selection_strategy=InputSelectionStrategy.PLAN_ORDER,
        )
        evaluation = evaluator.evaluate_group(
            group=group,
            sources=(
                resolve_result_input_source(
                    worker_item_key=upstream.worker_item_key,
                    facts=facts,
                ),
            ),
        )
        if not evaluation.satisfied or len(evaluation.ready_sources) != 1:
            raise ValueError("hard input source is not ready")
        selected = _selection_source(evaluation.ready_sources[0], priority=1)
        dependency_mapping = {
            "downstream_request_id": str(dependency.downstream_request_id),
            "upstream_request_id": str(dependency.upstream_request_id),
            "enforcement": dependency.enforcement.value,
            "required_artifact_refs": list(dependency.required_artifact_refs),
            "required_evidence_refs": list(dependency.required_evidence_refs),
        }
        values.append(
            InputSelectionHardInputV1(
                dependency_key=f"request:{dependency.upstream_request_id}",
                rule_hash=sha256(canonical_json_bytes(dependency_mapping)).hexdigest(),
                source=selected,
            )
        )
    return tuple(values)


def _configured_groups(
    facts: CollaborationFacts,
    *,
    request: CollaborationRequestFact,
) -> tuple[InputGroupV1, ...]:
    policy = facts.plan_head.policy_snapshot
    if policy is None:
        configured = facts.plan_snapshot.collaboration_policy
        policy = configured.to_mapping() if configured is not None else {}
    raw_groups = policy.get("input_groups", ())
    if not isinstance(raw_groups, (list, tuple)):
        raise TypeError("input groups are invalid")
    return tuple(
        group
        for group in (InputGroupV1.from_mapping(item) for item in raw_groups)
        if group.consumer_worker_item_key == request.worker_item_key
    )


def _selection_source(
    source: ResultInputSource,
    *,
    priority: int,
) -> InputSelectionSourceV1:
    if (
        source.worker_item_id is None
        or source.effective_request_id is None
        or not source.allowed_lineage_ids
        or source.effective_execution_id is None
        or source.attempt is None
        or source.source_basis_hash is None
    ):
        raise ValueError("ready input source has incomplete provenance")
    return InputSelectionSourceV1(
        worker_item_key=source.worker_item_key,
        worker_item_id=source.worker_item_id,
        subtask_path=source.subtask_path,
        effective_request_id=source.effective_request_id,
        allowed_lineage_ids=source.allowed_lineage_ids,
        effective_lineage_id=source.allowed_lineage_ids[-1],
        effective_execution_id=source.effective_execution_id,
        attempt=source.attempt,
        inheritance_refs=source.inheritance_refs,
        priority=priority,
        source_basis_hash=source.source_basis_hash,
        artifacts=source.artifacts,
        evidence=source.evidence,
    )


def _evaluate_source(
    *,
    source: ResultInputSource,
    requirements: InputGroupRequirementsV1,
) -> InputCandidateState:
    artifact_by_id = {item.artifact_id: item for item in source.artifacts}
    matched_artifacts: dict[UUID, ResultInputArtifact] = {}
    missing_artifact_refs: list[str] = []
    for selector in requirements.required_artifact_refs:
        artifact_type = selector.removeprefix("artifact_type:")
        matches = tuple(
            item
            for item in source.artifacts
            if item.artifact_type.value == artifact_type
        )
        if not matches:
            missing_artifact_refs.append(selector)
        for item in matches:
            matched_artifacts[item.artifact_id] = item

    matched_evidence: dict[UUID, ResultInputEvidence] = {}
    missing_evidence_refs: list[str] = []
    evidence_out_of_scope = False
    for selector in requirements.required_evidence_refs:
        matches = tuple(
            item
            for item in source.evidence
            if item.acceptance_criterion_ref == selector
        )
        valid_matches = tuple(
            item
            for item in matches
            if all(artifact_id in artifact_by_id for artifact_id in item.artifact_ids)
        )
        if len(valid_matches) != len(matches):
            evidence_out_of_scope = True
        if not valid_matches or len(valid_matches) != len(matches):
            missing_evidence_refs.append(selector)
            continue
        for item in valid_matches:
            matched_evidence[item.evidence_id] = item
            for artifact_id in item.artifact_ids:
                matched_artifacts[artifact_id] = artifact_by_id[artifact_id]

    evaluated_source = replace(
        source,
        artifacts=tuple(matched_artifacts.values()),
        evidence=tuple(matched_evidence.values()),
        source_basis_hash=None,
    )
    identity_complete = _source_identity_complete(evaluated_source)
    basis_hash = (
        _source_basis_hash(evaluated_source, requirements=requirements)
        if identity_complete
        else None
    )
    evaluated_source = replace(evaluated_source, source_basis_hash=basis_hash)

    outcome_unknown = source.outcome_unknown or not identity_complete
    ready = (
        source.reason_code is None
        and not outcome_unknown
        and source.execution_status is requirements.execution_status
        and not missing_artifact_refs
        and not missing_evidence_refs
        and not evidence_out_of_scope
    )
    return InputCandidateState(
        worker_item_key=source.worker_item_key,
        source=evaluated_source,
        ready=ready,
        missing_artifact_refs=tuple(missing_artifact_refs),
        missing_evidence_refs=tuple(missing_evidence_refs),
        retry_possible=source.retry_possible,
        outcome_unknown=outcome_unknown,
        source_basis_hash=basis_hash,
        reason_code=_candidate_reason(
            source=source,
            ready=ready,
            identity_complete=identity_complete,
            evidence_out_of_scope=evidence_out_of_scope,
            missing_artifact_refs=missing_artifact_refs,
            missing_evidence_refs=missing_evidence_refs,
        ),
    )


def _source_identity_complete(source: ResultInputSource) -> bool:
    return (
        source.worker_item_id is not None
        and source.effective_request_id is not None
        and bool(source.allowed_lineage_ids)
        and source.effective_execution_id is not None
        and source.attempt is not None
        and source.execution_status is not None
    )


def _source_basis_hash(
    source: ResultInputSource,
    *,
    requirements: InputGroupRequirementsV1,
) -> str:
    payload = {
        "worker_item_key": source.worker_item_key,
        "worker_item_id": str(source.worker_item_id),
        "subtask_path": list(source.subtask_path),
        "effective_request_id": str(source.effective_request_id),
        "allowed_lineage_ids": [str(item) for item in source.allowed_lineage_ids],
        "effective_execution_id": str(source.effective_execution_id),
        "attempt": source.attempt,
        "execution_status": (
            source.execution_status.value
            if source.execution_status is not None
            else None
        ),
        "inheritance_refs": list(source.inheritance_refs),
        "requirements": requirements.to_mapping(),
        "artifacts": [item.to_mapping() for item in source.artifacts],
        "evidence": [item.to_mapping() for item in source.evidence],
    }
    return sha256(canonical_json_bytes(payload)).hexdigest()


def _candidate_reason(
    *,
    source: ResultInputSource,
    ready: bool,
    identity_complete: bool,
    evidence_out_of_scope: bool,
    missing_artifact_refs: Sequence[str],
    missing_evidence_refs: Sequence[str],
) -> str | None:
    if ready:
        return None
    if source.outcome_unknown:
        return "input_source_outcome_unknown"
    if source.reason_code is not None:
        return source.reason_code
    if not identity_complete:
        return "input_source_outcome_unknown"
    if source.execution_status is not ExecutionStatus.SUCCEEDED:
        return (
            "input_group_waiting"
            if source.retry_possible
            or source.execution_status in _POSSIBLY_ACTIVE_STATUSES
            else "input_source_failed"
        )
    if evidence_out_of_scope:
        return "input_evidence_artifact_out_of_scope"
    if missing_artifact_refs:
        return "input_artifact_missing"
    if missing_evidence_refs:
        return "input_evidence_missing"
    return "input_group_waiting"


def _missing_state(worker_item_key: str) -> InputCandidateState:
    return InputCandidateState(
        worker_item_key=worker_item_key,
        source=None,
        ready=False,
        missing_artifact_refs=(),
        missing_evidence_refs=(),
        retry_possible=False,
        outcome_unknown=True,
        source_basis_hash=None,
        reason_code="input_source_not_found",
    )


def _ambiguous_state(worker_item_key: str) -> InputCandidateState:
    return InputCandidateState(
        worker_item_key=worker_item_key,
        source=None,
        ready=False,
        missing_artifact_refs=(),
        missing_evidence_refs=(),
        retry_possible=False,
        outcome_unknown=True,
        source_basis_hash=None,
        reason_code="input_source_ambiguous",
    )


def _group_reason(
    *,
    states: Sequence[InputCandidateState],
    ready_count: int,
    required_count: int,
    satisfied: bool,
) -> str | None:
    if satisfied:
        return None
    if any(item.outcome_unknown for item in states):
        return "input_source_outcome_unknown"
    possible_count = sum(_candidate_can_still_succeed(item) for item in states)
    if ready_count + possible_count < required_count:
        return "input_quorum_unreachable"
    return "input_group_waiting"


def _candidate_can_still_succeed(state: InputCandidateState) -> bool:
    if state.ready:
        return False
    if state.retry_possible or state.outcome_unknown:
        return True
    return (
        state.source is not None
        and state.source.execution_status in _POSSIBLY_ACTIVE_STATUSES
    )


def _minimum_count(group: InputGroupV1) -> int:
    assert group.minimum_count is not None
    return group.minimum_count
