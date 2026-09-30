from __future__ import annotations

from collections.abc import Callable, Sequence
from hashlib import sha256
from uuid import UUID

from sidera_control_plane.application.collaboration_projection import (
    CollaborationExecutionFact,
    CollaborationFacts,
    CollaborationRequestFact,
    CollaborationWorkerPlacementFact,
)
from sidera_control_plane.domain.artifacts import ArtifactType, EvidenceClaimStatus
from sidera_control_plane.domain.code_results import (
    CodeInputManifestV1,
    CodeInputSourceV1,
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.collaboration_runtime import canonical_json_bytes
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.result_inputs import (
    ResultInputArtifact,
    ResultInputEvidence,
    ResultInputSource,
)

_UNKNOWN_HANDOFF_STATUSES = frozenset({"blocked_unknown"})


def order_code_result_dependency_closure(
    *,
    direct_snapshots: Sequence[CodeResultSnapshotV1],
    snapshots: Sequence[CodeResultSnapshotV1],
    workspace_id: UUID,
    root_task_id: UUID,
    repository_ref: str,
) -> tuple[CodeResultSnapshotV1, ...]:
    """Return exactly the selected snapshots and their dependencies, dependency-first."""

    if not direct_snapshots or not snapshots:
        raise ValueError("code result dependency closure must not be empty")

    snapshots_by_id: dict[UUID, CodeResultSnapshotV1] = {}
    for snapshot in snapshots:
        if not isinstance(snapshot, CodeResultSnapshotV1):
            raise TypeError("code result dependency snapshot must be typed")
        if snapshot.id in snapshots_by_id:
            raise ValueError("code result dependency snapshot is duplicated")
        if (
            snapshot.workspace_id != workspace_id
            or snapshot.root_task_id != root_task_id
            or snapshot.repository_ref != repository_ref
        ):
            raise ValueError("code result dependency snapshot scope does not match")
        snapshots_by_id[snapshot.id] = snapshot

    for snapshot in direct_snapshots:
        if snapshots_by_id.get(snapshot.id) != snapshot:
            raise ValueError("direct code result snapshot is missing or changed")

    ordered: list[CodeResultSnapshotV1] = []
    visiting: set[UUID] = set()
    visited: set[UUID] = set()

    def visit(snapshot: CodeResultSnapshotV1) -> None:
        if snapshot.id in visiting:
            raise ValueError("code result dependency graph contains a cycle")
        if snapshot.id in visited:
            return
        visiting.add(snapshot.id)
        for dependency_id in snapshot.consumed_result_ids:
            dependency = snapshots_by_id.get(dependency_id)
            if dependency is None:
                raise ValueError("code result dependency snapshot is missing")
            visit(dependency)
        visiting.remove(snapshot.id)
        visited.add(snapshot.id)
        ordered.append(snapshot)

    for snapshot in direct_snapshots:
        visit(snapshot)
    if visited != set(snapshots_by_id):
        raise ValueError("code result dependency closure contains an unrelated snapshot")
    return tuple(ordered)


def load_code_result_dependency_closure(
    *,
    direct_snapshots: Sequence[CodeResultSnapshotV1],
    load_snapshot: Callable[[UUID], object],
    workspace_id: UUID,
    root_task_id: UUID,
    repository_ref: str,
) -> tuple[CodeResultSnapshotV1, ...]:
    """Load only the immutable dependencies reachable from selected results."""

    snapshots_by_id = {snapshot.id: snapshot for snapshot in direct_snapshots}
    if len(snapshots_by_id) != len(direct_snapshots):
        raise ValueError("direct code result snapshot is duplicated")
    pending = [
        dependency_id
        for snapshot in direct_snapshots
        for dependency_id in snapshot.consumed_result_ids
    ]
    while pending:
        result_id = pending.pop()
        if result_id in snapshots_by_id:
            continue
        dependency = load_snapshot(result_id)
        if (
            not isinstance(dependency, CodeResultSnapshotV1)
            or dependency.id != result_id
        ):
            raise ValueError("code result dependency snapshot is missing")
        snapshots_by_id[result_id] = dependency
        pending.extend(dependency.consumed_result_ids)
    return order_code_result_dependency_closure(
        direct_snapshots=direct_snapshots,
        snapshots=tuple(snapshots_by_id.values()),
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        repository_ref=repository_ref,
    )


def build_rework_code_input_manifest(
    *,
    candidate: IntegrationCandidateV1,
    snapshots: Sequence[CodeResultSnapshotV1],
    request_id: UUID,
    execution_id: UUID,
    repository_ref: str,
) -> CodeInputManifestV1:
    """Build a fresh V3 code input identity from one authorized candidate."""

    if not isinstance(candidate, IntegrationCandidateV1):
        raise TypeError("rework candidate must be typed")
    if not snapshots:
        raise ValueError("rework code input snapshots must not be empty")
    snapshots_by_id: dict[UUID, CodeResultSnapshotV1] = {}
    for snapshot in snapshots:
        if not isinstance(snapshot, CodeResultSnapshotV1):
            raise TypeError("rework code input snapshots must be typed")
        if snapshot.id in snapshots_by_id:
            raise ValueError("rework code input snapshot is duplicated")
        snapshots_by_id[snapshot.id] = snapshot
    direct_snapshots = tuple(
        snapshots_by_id[item]
        for item in candidate.source_result_ids
        if item in snapshots_by_id
    )
    if len(direct_snapshots) != len(candidate.source_result_ids):
        raise ValueError("rework code input snapshots do not match candidate sources")
    ordered = order_code_result_dependency_closure(
        direct_snapshots=direct_snapshots,
        snapshots=tuple(snapshots_by_id.values()),
        workspace_id=candidate.workspace_id,
        root_task_id=candidate.root_task_id,
        repository_ref=repository_ref,
    )

    sources = tuple(
        CodeInputSourceV1(
            result_id=snapshot.id,
            result_hash=snapshot.content_hash,
            input_commit=snapshot.input_commit,
            input_tree=snapshot.input_tree,
            result_commit=snapshot.result_commit,
            result_tree=snapshot.result_tree,
        )
        for snapshot in ordered
    )
    content = {
        "schema_version": 1,
        "workspace_id": str(candidate.workspace_id),
        "root_task_id": str(candidate.root_task_id),
        "request_id": str(request_id),
        "execution_id": str(execution_id),
        "selection_id": str(candidate.input_selection_id),
        "selection_hash": candidate.input_selection_hash,
        "repository_ref": repository_ref,
        "base_commit": candidate.base_commit,
        "sources": [item.to_mapping() for item in sources],
    }
    return CodeInputManifestV1(
        schema_version=1,
        workspace_id=candidate.workspace_id,
        root_task_id=candidate.root_task_id,
        request_id=request_id,
        execution_id=execution_id,
        selection_id=candidate.input_selection_id,
        selection_hash=candidate.input_selection_hash,
        repository_ref=repository_ref,
        base_commit=candidate.base_commit,
        sources=sources,
        input_hash=sha256(canonical_json_bytes(content)).hexdigest(),
    )


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

    def matches_placement(item: CollaborationRequestFact) -> bool:
        return (
            item.worker_item_key == worker_item_key
            and item.worker_item_id == placement.worker_item_id
            and item.subtask_task_id == placement.subtask_task_id
            and item.handoff_transition_id is None
            and item.result_derivation_kind != "verification"
        )

    active_requests = tuple(
        item
        for item in facts.requests
        if matches_placement(item)
    )
    historical_requests = tuple(
        item
        for item in facts.result_source_requests
        if matches_placement(item)
    )
    source_requests = (*active_requests, *historical_requests)
    reintegration_requests = tuple(
        item for item in source_requests if item.purpose == "reintegrate"
    )
    if reintegration_requests:
        current_requests = reintegration_requests
    else:
        rework_requests = tuple(
            item for item in source_requests if item.purpose == "rework"
        )
        if rework_requests:
            current_requests = rework_requests
        else:
            current_requests = active_requests or historical_requests
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
        for item in (*facts.requests, *facts.result_source_requests)
        if item.worker_item_key == placement.worker_item_key
        and item.worker_item_id == placement.worker_item_id
        and item.subtask_task_id == placement.subtask_task_id
        and item.handoff_transition_id == transition_id
        and item.result_derivation_kind != "verification"
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
