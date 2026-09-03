from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from sidera_control_plane.application.collaboration_governance_fingerprint import (
    build_release_proposal_fingerprint,
)
from sidera_control_plane.application.collaboration_release_validation import (
    ReleaseEligibilityAssessment,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationRun,
    RuntimeIntent,
    RuntimeIntentApplicationStatus,
)
from sidera_control_plane.domain.collaboration_governance import (
    CollaborationReleaseProposal,
)
from sidera_control_plane.domain.collaboration_runtime import RuntimeProjectionV1

if TYPE_CHECKING:
    from sidera_control_plane.application.ports import ControlPlaneUnitOfWork


@dataclass(frozen=True, slots=True)
class GovernanceDispositionResult:
    assessments: tuple[ReleaseEligibilityAssessment, ...]
    proposals: tuple[CollaborationReleaseProposal, ...]


class CollaborationGovernanceDispositionService:
    def dispose(
        self,
        *,
        uow: ControlPlaneUnitOfWork,
        intents: tuple[RuntimeIntent, ...],
        assessments: tuple[ReleaseEligibilityAssessment, ...],
        run: CollaborationRun,
        current_projection: RuntimeProjectionV1,
        now: datetime,
    ) -> GovernanceDispositionResult:
        eligible = tuple(
            assessment
            for assessment in assessments
            if assessment.status is RuntimeIntentApplicationStatus.APPLIED
            and assessment.request is not None
        )
        worker_item_ids = tuple(
            assessment.request.worker_item_id
            for assessment in eligible
            if assessment.request is not None
        )
        holds = uow.collaboration_governance.get_active_holds(
            run.workspace_id, run.id, worker_item_ids, for_update=True
        )
        hold_by_worker = {item.worker_item_id: item for item in holds}
        proposals: list[CollaborationReleaseProposal] = []
        updated: list[ReleaseEligibilityAssessment] = []
        for intent, assessment in zip(intents, assessments, strict=True):
            request = assessment.request
            hold = hold_by_worker.get(request.worker_item_id) if request else None
            if (
                assessment.status is not RuntimeIntentApplicationStatus.APPLIED
                or request is None
                or hold is None
            ):
                updated.append(assessment)
                continue
            fingerprint = build_release_proposal_fingerprint(
                current_projection,
                intent,
                worker_item_id=request.worker_item_id,
            )
            existing = uow.collaboration_governance.find_matching_proposal(
                run.workspace_id,
                hold.id,
                request.id,
                intent.target_request_version,
                fingerprint.version,
                fingerprint.value,
                for_update=True,
            )
            if existing is not None:
                updated.append(
                    replace(
                        assessment,
                        status=RuntimeIntentApplicationStatus.DUPLICATE,
                        result_code="governance_proposal_duplicate",
                        result_summary="the matching governance Proposal already exists",
                        proposal=existing,
                    )
                )
                continue
            proposal = CollaborationReleaseProposal.create(
                id=uuid5(
                    NAMESPACE_URL,
                    f"sidera:collaboration-release-proposal:{hold.id}:"
                    f"{request.id}:{intent.target_request_version}:"
                    f"{fingerprint.version}:{fingerprint.value}",
                ),
                workspace_id=run.workspace_id,
                hold_id=hold.id,
                run_id=run.id,
                projection_snapshot_id=current_projection.projection_id,
                projection_hash=current_projection.content_hash,
                decision_id=intent.decision_id,
                intent_id=intent.id,
                execution_request_id=request.id,
                target_request_version=intent.target_request_version,
                input_fingerprint_version=fingerprint.version,
                input_fingerprint=fingerprint.value,
                reason_code=intent.reason_code,
                summary=assessment.result_summary,
                now=now,
            )
            uow.collaboration_governance.add_proposal(proposal)
            proposals.append(proposal)
            updated.append(
                replace(
                    assessment,
                    status=RuntimeIntentApplicationStatus.DEFERRED,
                    result_code="governance_proposal_pending",
                    result_summary="Release is waiting for human approval",
                    proposal=proposal,
                )
            )
        return GovernanceDispositionResult(tuple(updated), tuple(proposals))
