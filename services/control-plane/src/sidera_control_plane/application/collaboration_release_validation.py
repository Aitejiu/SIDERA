from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sidera_control_plane.domain.collaboration import (
    CollaborationGateMode,
    CollaborationRun,
    CollaborationRunStatus,
    RuntimeIntent,
    RuntimeIntentApplicationStatus,
    RuntimeRelease,
)
from sidera_control_plane.domain.collaboration_runtime import RuntimeProjectionV1
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.plan_revisions import RootTaskPlanHead


@dataclass(frozen=True, slots=True)
class ReleaseEligibilityAssessment:
    status: RuntimeIntentApplicationStatus
    result_code: str
    result_summary: str
    request: ExecutionRequest | None
    existing_release: RuntimeRelease | None = None
    proposal: object | None = None


class CollaborationReleaseEligibilityValidator:
    """Assess one intent using the same current release eligibility rules."""

    def assess(
        self,
        intent: RuntimeIntent,
        *,
        request: ExecutionRequest | None,
        run: CollaborationRun,
        plan_head: RootTaskPlanHead | None,
        current_projection: RuntimeProjectionV1 | None,
        existing_release: RuntimeRelease | None,
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
                    request, run=run, release=existing_release, plan_head=plan_head
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
        if request.collaboration_gate_mode is not CollaborationGateMode.RELEASE_REQUIRED:
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
        return ReleaseEligibilityAssessment(
            status=RuntimeIntentApplicationStatus.APPLIED,
            result_code="applied",
            result_summary="Release issued",
            request=request,
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
        and _head_matches_run(plan_head, run)
        and release.request_version_at_release <= request.version
    )


def _projection_requests(
    projection: RuntimeProjectionV1 | None,
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
