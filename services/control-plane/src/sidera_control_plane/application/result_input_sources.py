from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sidera_control_plane.application.collaboration_projection import (
    CollaborationExecutionFact,
    CollaborationFacts,
    CollaborationRequestFact,
    CollaborationWorkerPlacementFact,
)
from sidera_control_plane.domain.artifacts import ArtifactType, EvidenceClaimStatus
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.result_inputs import (
    ResultInputArtifact,
    ResultInputEvidence,
    ResultInputSource,
)

_UNKNOWN_HANDOFF_STATUSES = frozenset({"blocked_unknown"})


def resolve_result_input_source(
    *, worker_item_key: str, facts: CollaborationFacts
) -> ResultInputSource:
    placements = tuple(
        item
        for item in facts.worker_placements
        if item.worker_item_key == worker_item_key
    )
    if not placements:
        return _unresolved(worker_item_key, reason_code="input_source_not_found")
    if len(placements) != 1:
        return _unresolved(worker_item_key, reason_code="input_source_ambiguous")
    placement = placements[0]

    current_requests = tuple(
        item
        for item in facts.requests
        if item.worker_item_key == worker_item_key
        and item.worker_item_id == placement.worker_item_id
        and item.subtask_task_id == placement.subtask_task_id
        and item.handoff_transition_id is None
    )
    if len(current_requests) != 1:
        return _unresolved(
            worker_item_key,
            placement=placement,
            reason_code=(
                "input_source_not_materialized"
                if not current_requests
                else "input_source_ambiguous"
            ),
        )

    request = current_requests[0]
    if request.execution_lineage_id is None:
        return _for_request(
            placement=placement,
            request=request,
            reason_code="input_source_not_started",
        )
    execution, ambiguous = _latest_in_lineage(
        facts.executions,
        lineage_id=request.execution_lineage_id,
        placement=placement,
    )
    if ambiguous:
        return _for_request(
            placement=placement,
            request=request,
            reason_code="input_source_ambiguous",
            outcome_unknown=True,
        )
    if execution is None:
        return _for_request(
            placement=placement,
            request=request,
            reason_code="input_source_execution_missing",
            outcome_unknown=True,
        )

    allowed_lineages: list[UUID] = [execution.lineage_id]
    inheritance_refs: list[str] = []
    visited_execution_ids: set[UUID] = set()
    while True:
        if execution.id in visited_execution_ids:
            return _for_request(
                placement=placement,
                request=request,
                reason_code="input_source_ambiguous",
                outcome_unknown=True,
            )
        visited_execution_ids.add(execution.id)

        transitions = tuple(
            item
            for item in facts.handoff_transitions
            if item.worker_item_id == placement.worker_item_id
            and item.source_execution_id == execution.id
            and item.status in {"converged", *_UNKNOWN_HANDOFF_STATUSES}
        )
        continuation_lineages = {
            item.lineage_id
            for item in facts.executions
            if item.worker_item_id == placement.worker_item_id
            and item.task_id == placement.subtask_task_id
            and item.source_execution_id == execution.id
            and item.handoff_transition_id is None
        }
        if len(transitions) + len(continuation_lineages) > 1:
            return _for_request(
                placement=placement,
                request=request,
                reason_code="input_source_ambiguous",
                outcome_unknown=True,
            )

        if transitions:
            transition = transitions[0]
            if transition.status in _UNKNOWN_HANDOFF_STATUSES:
                return _resolved(
                    placement=placement,
                    request=request,
                    execution=execution,
                    allowed_lineages=allowed_lineages,
                    inheritance_refs=inheritance_refs,
                    facts=facts,
                    reason_code="input_source_outcome_unknown",
                    outcome_unknown=True,
                )
            target = _handoff_target(
                transition_id=transition.id,
                target_execution_id=transition.target_execution_id,
                placement=placement,
                facts=facts,
            )
            if target is None:
                return _for_request(
                    placement=placement,
                    request=request,
                    reason_code="input_source_ambiguous",
                    outcome_unknown=True,
                )
            request, target_execution = target
            execution, ambiguous = _latest_in_lineage(
                facts.executions,
                lineage_id=target_execution.lineage_id,
                placement=placement,
            )
            if ambiguous or execution is None:
                return _for_request(
                    placement=placement,
                    request=request,
                    reason_code=(
                        "input_source_ambiguous"
                        if ambiguous
                        else "input_source_execution_missing"
                    ),
                    outcome_unknown=True,
                )
            allowed_lineages.append(execution.lineage_id)
            inheritance_refs.append(
                f"handoff:{transition.id}:{transition.source_execution_id}:"
                f"{target_execution.id}"
            )
            continue

        if continuation_lineages:
            lineage_id = next(iter(continuation_lineages))
            child, ambiguous = _latest_in_lineage(
                facts.executions,
                lineage_id=lineage_id,
                placement=placement,
            )
            if ambiguous or child is None:
                return _for_request(
                    placement=placement,
                    request=request,
                    reason_code="input_source_ambiguous",
                    outcome_unknown=True,
                )
            inheritance_refs.append(f"continuation:{execution.id}:{child.id}")
            allowed_lineages.append(child.lineage_id)
            execution = child
            continue
        break

    binding_count = sum(
        item.execution_id == execution.id for item in facts.execution_bindings
    )
    if binding_count > 1:
        return _for_request(
            placement=placement,
            request=request,
            reason_code="input_source_ambiguous",
            outcome_unknown=True,
        )
    return _resolved(
        placement=placement,
        request=request,
        execution=execution,
        allowed_lineages=allowed_lineages,
        inheritance_refs=inheritance_refs,
        facts=facts,
    )


def _latest_in_lineage(
    executions: Sequence[CollaborationExecutionFact],
    *,
    lineage_id: UUID,
    placement: CollaborationWorkerPlacementFact,
) -> tuple[CollaborationExecutionFact | None, bool]:
    matching = tuple(
        item
        for item in executions
        if item.lineage_id == lineage_id
        and item.worker_item_id == placement.worker_item_id
        and item.task_id == placement.subtask_task_id
    )
    if not matching:
        return None, False
    latest_attempt = max(item.attempt for item in matching)
    latest = tuple(item for item in matching if item.attempt == latest_attempt)
    return (latest[0], False) if len(latest) == 1 else (None, True)


def _handoff_target(
    *,
    transition_id: UUID,
    target_execution_id: UUID | None,
    placement: CollaborationWorkerPlacementFact,
    facts: CollaborationFacts,
) -> tuple[CollaborationRequestFact, CollaborationExecutionFact] | None:
    if target_execution_id is None:
        return None
    executions = tuple(
        item
        for item in facts.executions
        if item.id == target_execution_id
        and item.worker_item_id == placement.worker_item_id
        and item.task_id == placement.subtask_task_id
        and item.handoff_transition_id == transition_id
    )
    requests = tuple(
        item
        for item in facts.requests
        if item.worker_item_key == placement.worker_item_key
        and item.worker_item_id == placement.worker_item_id
        and item.subtask_task_id == placement.subtask_task_id
        and item.handoff_transition_id == transition_id
        and item.execution_lineage_id is not None
    )
    if len(executions) != 1 or len(requests) != 1:
        return None
    execution = executions[0]
    request = requests[0]
    if request.execution_lineage_id != execution.lineage_id:
        return None
    return request, execution


def _resolved(
    *,
    placement: CollaborationWorkerPlacementFact,
    request: CollaborationRequestFact,
    execution: CollaborationExecutionFact,
    allowed_lineages: Sequence[UUID],
    inheritance_refs: Sequence[str],
    facts: CollaborationFacts,
    reason_code: str | None = None,
    outcome_unknown: bool = False,
) -> ResultInputSource:
    artifacts = tuple(
        ResultInputArtifact(
            artifact_id=item.id,
            artifact_type=ArtifactType(item.artifact_type),
            content_hash=item.content_hash,
            size_bytes=item.size_bytes,
            media_type=item.media_type,
        )
        for item in facts.artifacts
        if item.execution_id == execution.id
        and item.task_id == placement.subtask_task_id
    )
    evidence = tuple(
        ResultInputEvidence(
            evidence_id=item.id,
            acceptance_criterion_ref=_normalized_evidence_ref(
                item.acceptance_criterion_ref
            ),
            artifact_ids=item.artifact_ids,
        )
        for item in facts.evidence
        if item.execution_id == execution.id
        and item.task_id == placement.subtask_task_id
        and item.status == EvidenceClaimStatus.CANDIDATE.value
    )
    status = ExecutionStatus(execution.status)
    return ResultInputSource(
        worker_item_key=placement.worker_item_key,
        worker_item_id=placement.worker_item_id,
        subtask_path=placement.subtask_path,
        effective_request_id=request.id,
        allowed_lineage_ids=tuple(dict.fromkeys(allowed_lineages)),
        effective_execution_id=execution.id,
        attempt=execution.attempt,
        execution_status=status,
        inheritance_refs=tuple(inheritance_refs),
        artifacts=artifacts,
        evidence=evidence,
        retry_possible=(
            status in {ExecutionStatus.FAILED, ExecutionStatus.LOST}
            and execution.attempt < execution.max_attempts
        ),
        outcome_unknown=outcome_unknown,
        reason_code=reason_code,
    )


def _normalized_evidence_ref(value: str) -> str:
    return value if value.startswith("acceptance:") else f"acceptance:{value}"


def _for_request(
    *,
    placement: CollaborationWorkerPlacementFact,
    request: CollaborationRequestFact,
    reason_code: str,
    outcome_unknown: bool = False,
) -> ResultInputSource:
    return ResultInputSource(
        worker_item_key=placement.worker_item_key,
        worker_item_id=placement.worker_item_id,
        subtask_path=placement.subtask_path,
        effective_request_id=request.id,
        allowed_lineage_ids=(),
        effective_execution_id=None,
        attempt=None,
        execution_status=None,
        inheritance_refs=(),
        artifacts=(),
        evidence=(),
        retry_possible=request.max_attempts > 0,
        outcome_unknown=outcome_unknown,
        reason_code=reason_code,
    )


def _unresolved(
    worker_item_key: str,
    *,
    reason_code: str,
    placement: CollaborationWorkerPlacementFact | None = None,
) -> ResultInputSource:
    return ResultInputSource(
        worker_item_key=worker_item_key,
        worker_item_id=placement.worker_item_id if placement is not None else None,
        subtask_path=placement.subtask_path if placement is not None else (),
        effective_request_id=None,
        allowed_lineage_ids=(),
        effective_execution_id=None,
        attempt=None,
        execution_status=None,
        inheritance_refs=(),
        artifacts=(),
        evidence=(),
        retry_possible=False,
        outcome_unknown=reason_code == "input_source_ambiguous",
        reason_code=reason_code,
    )
