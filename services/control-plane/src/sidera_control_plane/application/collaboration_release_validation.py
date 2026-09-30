from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from sidera_control_plane.application.collaboration_projection import CollaborationFacts
from sidera_control_plane.application.result_input_evaluation import (
    build_hard_input_selections,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationGateMode,
    CollaborationRun,
    CollaborationRunStatus,
    RuntimeIntent,
    RuntimeIntentApplicationStatus,
    RuntimeRelease,
)
from sidera_control_plane.domain.collaboration_runtime import (
    RuntimeProjectionV1,
    RuntimeProjectionV2,
    canonical_json_bytes,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
    InputSelectionGroupV1,
    InputSelectionHardInputV1,
    InputSelectionSourceV1,
)
from sidera_control_plane.domain.plan_revisions import RootTaskPlanHead
from sidera_control_plane.domain.result_inputs import (
    InputGroupEvaluation,
    InputGroupMode,
    InputGroupV1,
    ResultInputSource,
)


@dataclass(frozen=True, slots=True)
class ReleaseEligibilityAssessment:
    status: RuntimeIntentApplicationStatus
    result_code: str
    result_summary: str
    request: ExecutionRequest | None
    existing_release: RuntimeRelease | None = None
    proposal: object | None = None
    input_groups: tuple[InputSelectionGroupV1, ...] = ()
    hard_inputs: tuple[InputSelectionHardInputV1, ...] = ()


class CollaborationReleaseEligibilityValidator:
    """Assess one intent using the same current release eligibility rules."""

    def assess(
        self,
        intent: RuntimeIntent,
        *,
        request: ExecutionRequest | None,
        run: CollaborationRun,
        plan_head: RootTaskPlanHead | None,
        current_projection: RuntimeProjectionV1 | RuntimeProjectionV2 | None,
        existing_release: RuntimeRelease | None,
        existing_selection: CollaborationInputSelectionV1 | None = None,
        current_facts: CollaborationFacts | None = None,
    ) -> ReleaseEligibilityAssessment:
        if request is None:
            return _rejected(
                None,
                "intent_target_invalid",
                "the target ExecutionRequest does not exist in this workspace",
            )
        if not _request_matches_run(request, run):
            return _rejected(
                request,
                "intent_target_invalid",
                "the target ExecutionRequest is outside the current Run",
                existing_release=existing_release,
            )
        if existing_release is not None:
            if (
                _release_matches(
                    request,
                    run=run,
                    release=existing_release,
                    plan_head=plan_head,
                    selection=existing_selection,
                )
                and existing_release.request_version_at_release
                == intent.target_request_version
            ):
                return ReleaseEligibilityAssessment(
                    status=RuntimeIntentApplicationStatus.DUPLICATE,
                    result_code="duplicate",
                    result_summary="the target request already has this Release",
                    request=request,
                    existing_release=existing_release,
                )
            return _rejected(
                request,
                "decision_conflict",
                "the target request has a different Release identity",
                existing_release=existing_release,
            )
        if request.version != intent.target_request_version:
            return ReleaseEligibilityAssessment(
                status=RuntimeIntentApplicationStatus.STALE,
                result_code="projection_stale",
                result_summary="the target request version changed",
                request=request,
            )
        if (
            request.collaboration_gate_mode
            is not CollaborationGateMode.RELEASE_REQUIRED
        ):
            return _rejected(
                request,
                "intent_target_invalid",
                "only release-required requests can receive a Runtime Release",
            )
        if request.status not in {
            ExecutionRequestStatus.BLOCKED,
            ExecutionRequestStatus.READY,
        }:
            return _rejected(
                request,
                "intent_target_invalid",
                "the target request is already closed",
            )
        entries = _projection_requests(current_projection)
        entry = entries.get(request.id)
        if entry is None:
            return _rejected(
                request,
                "intent_target_invalid",
                "the target request is absent from the current Projection",
            )
        if entry.get("eligible") is not True:
            return _rejected(
                request,
                "join_condition_unsatisfied",
                "the target request no longer satisfies all_required",
            )
        input_groups: tuple[InputSelectionGroupV1, ...] = ()
        hard_inputs: tuple[InputSelectionHardInputV1, ...] = ()
        if isinstance(current_projection, RuntimeProjectionV2):
            try:
                input_groups = _validated_input_groups(
                    intent,
                    request=request,
                    projection=current_projection,
                )
                hard_inputs = _hard_inputs(
                    request=request,
                    facts=current_facts,
                )
            except (ValidationError, ValueError):
                return _rejected(
                    request,
                    "input_selection_invalid",
                    "the Runtime input selection does not match current facts",
                )
        return ReleaseEligibilityAssessment(
            status=RuntimeIntentApplicationStatus.APPLIED,
            result_code="applied",
            result_summary="Release issued",
            request=request,
            input_groups=input_groups,
            hard_inputs=hard_inputs,
        )


class CollaborationReleaseActivator:
    """Marker collaborator for the shared request-gate activation path."""

    def activate(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError


def _rejected(
    request: ExecutionRequest | None,
    code: str,
    summary: str,
    *,
    existing_release: RuntimeRelease | None = None,
) -> ReleaseEligibilityAssessment:
    return ReleaseEligibilityAssessment(
        status=RuntimeIntentApplicationStatus.REJECTED,
        result_code=code,
        result_summary=summary,
        request=request,
        existing_release=existing_release,
    )


def _request_matches_run(request: ExecutionRequest, run: CollaborationRun) -> bool:
    return (
        request.workspace_id == run.workspace_id
        and request.root_task_id == run.root_task_id
        and request.plan_snapshot_id == run.plan_snapshot_id
        and request.plan_generation == run.plan_generation
    )


def _head_matches_run(head: RootTaskPlanHead, run: CollaborationRun) -> bool:
    return (
        head.workspace_id == run.workspace_id
        and head.root_task_id == run.root_task_id
        and head.active_snapshot_id == run.plan_snapshot_id
        and head.active_generation == run.plan_generation
        and head.active_snapshot_hash == run.plan_content_hash
    )


def _release_matches(
    request: ExecutionRequest,
    *,
    run: CollaborationRun,
    release: RuntimeRelease,
    plan_head: RootTaskPlanHead | None,
    selection: CollaborationInputSelectionV1 | None,
) -> bool:
    return (
        run.status is CollaborationRunStatus.ACTIVE
        and plan_head is not None
        and release.workspace_id == request.workspace_id == run.workspace_id
        and release.run_id == run.id
        and release.plan_snapshot_id == request.plan_snapshot_id == run.plan_snapshot_id
        and release.plan_generation == request.plan_generation == run.plan_generation
        and release.plan_content_hash == run.plan_content_hash
        and release.execution_request_id == request.id
        and release.input_selection_id == request.input_selection_id
        and release.input_selection_hash == request.input_selection_hash
        and release_input_selection_matches(
            request,
            run=run,
            release=release,
            selection=selection,
        )
        and _head_matches_run(plan_head, run)
        and release.request_version_at_release <= request.version
    )


def release_input_selection_matches(
    request: ExecutionRequest,
    *,
    run: CollaborationRun,
    release: RuntimeRelease,
    selection: CollaborationInputSelectionV1 | None,
) -> bool:
    """Verify a committed Selection identity without reselecting current inputs."""

    if request.input_selection_id is None and request.input_selection_hash is None:
        return (
            release.input_selection_id is None
            and release.input_selection_hash is None
        )
    if (
        request.input_selection_id is None
        or request.input_selection_hash is None
        or release.input_selection_id is None
        or release.input_selection_hash is None
        or selection is None
    ):
        return False
    return (
        selection.id == request.input_selection_id == release.input_selection_id
        and selection.selection_hash
        == request.input_selection_hash
        == release.input_selection_hash
        and selection.selection_hash == selection.compute_selection_hash()
        and selection.workspace_id == request.workspace_id == run.workspace_id
        and selection.root_task_id == request.root_task_id == run.root_task_id
        and selection.run_id == run.id == release.run_id
        and selection.plan_snapshot_id
        == request.plan_snapshot_id
        == run.plan_snapshot_id
        == release.plan_snapshot_id
        and selection.plan_generation
        == request.plan_generation
        == run.plan_generation
        == release.plan_generation
        and selection.plan_content_hash
        == run.plan_content_hash
        == release.plan_content_hash
        and selection.target_worker_item_id == request.worker_item_id
        and selection.target_execution_request_id
        == request.id
        == release.execution_request_id
        and selection.target_request_version == release.request_version_at_release
        and selection.decision_id == release.decision_id
        and selection.intent_id == release.intent_id
    )


def _projection_requests(
    projection: RuntimeProjectionV1 | RuntimeProjectionV2 | None,
) -> dict[UUID, dict[str, object]]:
    if projection is None:
        return {}
    result: dict[UUID, dict[str, object]] = {}
    for item in projection.execution_requests:
        value = item.get("id")
        if isinstance(value, str):
            try:
                result[UUID(value)] = dict(item)
            except ValueError:
                continue
    return result


def _validated_input_groups(
    intent: RuntimeIntent,
    *,
    request: ExecutionRequest,
    projection: RuntimeProjectionV2,
) -> tuple[InputSelectionGroupV1, ...]:
    raw_groups = projection.policy.get("input_groups")
    if not isinstance(raw_groups, (list, tuple)):
        raise ValidationError("Projection v2 input groups are invalid")
    configured = tuple(InputGroupV1.from_mapping(item) for item in raw_groups)
    groups_by_key = {
        group.group_key: group
        for group in configured
        if group.consumer_worker_item_key == request.worker_item_key
    }
    evaluations = {
        item.group_key: item.evaluation
        for item in projection.result_input_evaluations
        if item.consumer_execution_request_id == request.id
    }
    if set(evaluations) != set(groups_by_key):
        raise ValidationError("input selection evaluations do not match target groups")

    expected = tuple(
        _selection_group(groups_by_key[group_key], evaluations[group_key])
        for group_key in sorted(groups_by_key)
    )
    raw_selected = intent.payload_json.get("input_group_selections", ())
    if not isinstance(raw_selected, (list, tuple)):
        raise ValidationError("intent input group selections must be an array")
    selected = tuple(InputSelectionGroupV1.from_mapping(item) for item in raw_selected)
    if selected != expected:
        raise ValidationError(
            "Runtime input selection is not the legal plan-order result"
        )
    return expected


def _selection_group(
    group: InputGroupV1,
    evaluation: InputGroupEvaluation,
) -> InputSelectionGroupV1:
    if not evaluation.satisfied:
        raise ValidationError("input group is not satisfied")
    ready = {item.worker_item_key: item for item in evaluation.ready_sources}
    ordered = tuple(
        candidate
        for candidate in group.candidates
        if candidate.worker_item_key in ready
    )
    if group.mode is InputGroupMode.AT_LEAST:
        if group.minimum_count is None:
            raise ValidationError("input group minimum count is missing")
        ordered = ordered[: group.minimum_count]
    if len(ordered) != evaluation.required_count:
        raise ValidationError("input group selected source count is invalid")
    return InputSelectionGroupV1(
        group_key=group.group_key,
        rule_hash=sha256(canonical_json_bytes(group.to_mapping())).hexdigest(),
        sources=tuple(
            _selection_source(ready[item.worker_item_key], priority=item.priority)
            for item in ordered
        ),
    )


def _hard_inputs(
    *,
    request: ExecutionRequest,
    facts: CollaborationFacts | None,
) -> tuple[InputSelectionHardInputV1, ...]:
    if facts is None:
        raise ValidationError("current collaboration facts are required for V2 release")
    return build_hard_input_selections(request=request, facts=facts)


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
        raise ValidationError("ready input source has incomplete provenance")
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
