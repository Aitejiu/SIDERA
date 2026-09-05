from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, NoReturn
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.application.collaboration_decisions import (
    PlanTransitionCheck,
    update_collaboration_request_gates,
)
from sidera_control_plane.application.collaboration_governance_commands import (
    CollaborationGovernanceCommandError,
)
from sidera_control_plane.application.collaboration_governance_fingerprint import (
    build_release_proposal_fingerprint,
)
from sidera_control_plane.application.collaboration_projection import (
    CollaborationFacts,
    CollaborationProjectionAssembler,
)
from sidera_control_plane.application.collaboration_release_validation import (
    CollaborationReleaseEligibilityValidator,
    ReleaseEligibilityAssessment,
)
from sidera_control_plane.application.ports import (
    CollaborationFactStorePort,
    ControlPlaneUnitOfWork,
)
from sidera_control_plane.application.result_input_evaluation import (
    validate_selected_sources,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationGateMode,
    CollaborationRun,
    CollaborationRunStatus,
    RuntimeIntent,
    RuntimeIntentApplication,
    RuntimeIntentApplicationStatus,
    RuntimeRelease,
)
from sidera_control_plane.domain.collaboration_governance import (
    CollaborationGovernanceHold,
    CollaborationGovernanceResolution,
    CollaborationReleaseProposal,
    GovernanceHoldStatus,
    GovernanceResolutionAction,
    GovernanceResolutionResult,
    GovernanceTransport,
    ReleaseProposalStatus,
)
from sidera_control_plane.domain.collaboration_runtime import (
    RuntimeEnvelopeError,
    RuntimeProjectionV1,
    RuntimeProjectionV2,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
)
from sidera_control_plane.domain.plan_revisions import RootTaskPlanHead

FactStoreFactory = Callable[[ControlPlaneUnitOfWork], CollaborationFactStorePort]


@dataclass(frozen=True, slots=True)
class _ApprovalLineage:
    head: RootTaskPlanHead
    run: CollaborationRun
    worker_item: Any
    request: ExecutionRequest | None
    hold: CollaborationGovernanceHold
    proposal: CollaborationReleaseProposal
    application: RuntimeIntentApplication
    decision: Any
    intent: RuntimeIntent
    original_projection: RuntimeProjectionV1 | RuntimeProjectionV2
    input_selection: CollaborationInputSelectionV1 | None


class CollaborationGovernanceReleaseApplier:
    """Revalidate one held intent and resolve its proposal atomically."""

    def __init__(
        self,
        *,
        fact_store_factory: FactStoreFactory,
        projection_assembler: CollaborationProjectionAssembler | None = None,
        plan_transition_allows: PlanTransitionCheck | None = None,
        owner_id: str = "collaboration-governance",
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        self._fact_store_factory = fact_store_factory
        self._projection_assembler = (
            projection_assembler or CollaborationProjectionAssembler()
        )
        self._plan_transition_allows = plan_transition_allows or (
            lambda _uow, _request: True
        )
        self._owner_id = owner_id.strip()
        self._release_validator = CollaborationReleaseEligibilityValidator()

    def approve_in_transaction(
        self,
        uow: ControlPlaneUnitOfWork,
        command: CommandEnvelope,
        *,
        now: datetime,
    ) -> CommandResult:
        _validate_now(now)
        assert command.target_id is not None

        proposal_identity = uow.collaboration_governance.get_proposal(
            command.workspace_id, command.target_id
        )
        if proposal_identity is None:
            _raise("collaboration_governance_target_invalid", "Proposal was not found")
        hold_identity = uow.collaboration_governance.get_hold(
            command.workspace_id, proposal_identity.hold_id
        )
        if hold_identity is None:
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal Hold was not found",
            )

        lineage = self._lock_lineage(
            uow,
            workspace_id=command.workspace_id,
            proposal_identity=proposal_identity,
            hold_identity=hold_identity,
            expected_version=command.expected_version,
        )
        _validate_selection_expectation(command.payload, lineage.proposal)

        # The mutable lineage is locked before immutable ledger rows are read.
        # A superseded generation cannot be loaded by the fact store, so resolve
        # that fence before attempting to assemble a current Projection.
        if not self._can_assemble_current_projection(lineage):
            return self._resolve_stale(
                uow,
                command,
                lineage,
                reason=self._lineage_stale_reason(lineage),
                now=now,
                wake_runtime=False,
            )
        current_projection, current_facts = self._current_projection(
            uow, lineage.run, now=now
        )
        stale_reason = self._stale_reason(
            uow,
            lineage,
            current_projection=current_projection,
        )
        if stale_reason is not None:
            return self._resolve_stale(
                uow,
                command,
                lineage,
                reason=stale_reason,
                now=now,
                wake_runtime=self._can_form_current_recommendation(
                    uow, lineage, current_projection
                ),
            )

        assert lineage.request is not None
        if lineage.input_selection is None:
            assessment = self._release_validator.assess(
                lineage.intent,
                request=lineage.request,
                run=lineage.run,
                plan_head=lineage.head,
                current_projection=current_projection,
                existing_release=None,
                current_facts=current_facts,
            )
        else:
            target_entry = next(
                (
                    item
                    for item in current_projection.execution_requests
                    if item.get("id") == str(lineage.request.id)
                ),
                None,
            )
            if target_entry is None or target_entry.get("eligible") is not True:
                return self._resolve_stale(
                    uow,
                    command,
                    lineage,
                    reason="join_condition_unsatisfied",
                    now=now,
                    wake_runtime=False,
                )
            selected_source_reason = validate_selected_sources(
                selection=lineage.input_selection,
                facts=current_facts,
            )
            if selected_source_reason is not None:
                return self._resolve_stale(
                    uow,
                    command,
                    lineage,
                    reason=selected_source_reason,
                    now=now,
                    wake_runtime=True,
                )
            assessment = ReleaseEligibilityAssessment(
                status=RuntimeIntentApplicationStatus.APPLIED,
                result_code="governance_approved",
                result_summary="the fixed result inputs remain valid",
                request=lineage.request,
                input_groups=lineage.input_selection.groups,
                hard_inputs=lineage.input_selection.hard_inputs,
            )
        if assessment.status is not RuntimeIntentApplicationStatus.APPLIED:
            return self._resolve_stale(
                uow,
                command,
                lineage,
                reason=assessment.result_code,
                now=now,
                wake_runtime=False,
            )

        fingerprint = build_release_proposal_fingerprint(
            current_projection,
            lineage.intent,
            worker_item_id=lineage.request.worker_item_id,
            selection=lineage.input_selection,
            governance_fence=_governance_fence(lineage.hold),
        )
        if (
            fingerprint.version != lineage.proposal.input_fingerprint_version
            or fingerprint.value != lineage.proposal.input_fingerprint
        ):
            return self._resolve_stale(
                uow,
                command,
                lineage,
                reason="governance_fingerprint_stale",
                now=now,
                wake_runtime=True,
            )

        release = RuntimeRelease(
            id=uuid5(
                NAMESPACE_URL,
                f"sidera:collaboration-release:{lineage.intent.id}",
            ),
            workspace_id=lineage.run.workspace_id,
            run_id=lineage.run.id,
            plan_snapshot_id=lineage.run.plan_snapshot_id,
            plan_generation=lineage.run.plan_generation,
            plan_content_hash=lineage.run.plan_content_hash,
            decision_id=lineage.intent.decision_id,
            intent_id=lineage.intent.id,
            execution_request_id=lineage.intent.target_execution_request_id,
            request_version_at_release=lineage.intent.target_request_version,
            issued_at=now,
            input_selection_id=lineage.proposal.input_selection_id,
            input_selection_hash=lineage.proposal.input_selection_hash,
        )
        uow.collaboration_ledger.add_releases((release,))
        uow.flush()

        approved_application = lineage.application.resolve(
            status=RuntimeIntentApplicationStatus.APPLIED,
            result_code="governance_approved",
            result_summary="the human release proposal was approved",
            release_id=release.id,
            now=now,
        )
        uow.collaboration_ledger.save_application(
            approved_application,
            lineage.application.resource_version,
        )
        approved_proposal = lineage.proposal.approve(now=now)
        uow.collaboration_governance.save_proposal(
            approved_proposal,
            lineage.proposal.resource_version,
        )
        resolution = CollaborationGovernanceResolution.create(
            id=uuid4(),
            workspace_id=command.workspace_id,
            proposal_id=lineage.proposal.id,
            action=GovernanceResolutionAction.APPROVE,
            result=GovernanceResolutionResult.APPLIED,
            actor_id=command.actor_id,
            transport=_transport(command.payload),
            reason=_reason(command.payload),
            command_id=command.id,
            now=now,
        )
        uow.collaboration_governance.add_resolution(resolution)

        if lineage.input_selection is not None:
            previous_request_version = lineage.request.version
            changed = lineage.request.bind_input_selection(
                selection_id=lineage.input_selection.id,
                selection_hash=lineage.input_selection.selection_hash,
                now=now,
            )
            if changed:
                uow.execution_requests.save(lineage.request, previous_request_version)

        updated_assessment = replace(assessment, proposal=approved_proposal)
        update_collaboration_request_gates(
            uow,
            (updated_assessment,),
            releases={lineage.request.id: None},
            new_releases=(release,),
            run=lineage.run,
            head=lineage.head,
            current_projection=current_projection,
            now=now,
            plan_transition_allows=self._plan_transition_allows,
        )

        released_event = self._event(
            command,
            aggregate_type="execution_request",
            aggregate_id=lineage.request.id,
            aggregate_version=lineage.request.version,
            event_type="ExecutionRequestReleased",
            run=lineage.run,
            payload={
                "release_id": str(release.id),
                "decision_id": str(lineage.intent.decision_id),
                "intent_id": str(lineage.intent.id),
                "execution_request_id": str(lineage.request.id),
                "request_version_at_release": release.request_version_at_release,
                "input_selection_id": (
                    str(release.input_selection_id)
                    if release.input_selection_id is not None
                    else None
                ),
                "input_selection_hash": release.input_selection_hash,
                "status": lineage.request.status.value,
                "gate_reason": lineage.request.gate_reason,
            },
        )
        approved_event = self._event(
            command,
            aggregate_type="collaboration_release_proposal",
            aggregate_id=approved_proposal.id,
            aggregate_version=approved_proposal.resource_version,
            event_type="CollaborationReleaseProposalApproved",
            run=lineage.run,
            payload={
                "proposal_id": str(approved_proposal.id),
                "hold_id": str(approved_proposal.hold_id),
                "execution_request_id": str(approved_proposal.execution_request_id),
                "intent_id": str(approved_proposal.intent_id),
                "status": approved_proposal.status.value,
                "result": GovernanceResolutionResult.APPLIED.value,
                "release_id": str(release.id),
                "input_selection_id": (
                    str(approved_proposal.input_selection_id)
                    if approved_proposal.input_selection_id is not None
                    else None
                ),
                "input_selection_hash": approved_proposal.input_selection_hash,
            },
        )
        uow.events.append_with_outbox(released_event)
        uow.events.append_with_outbox(approved_event)
        self._publish_governance_change(
            uow,
            command,
            root_task_id=lineage.head.root_task_id,
            resource_version=approved_proposal.resource_version,
            change_type="collaboration_release_proposal_approved",
        )
        return CommandResult(
            command_id=command.id,
            aggregate_id=approved_proposal.id,
            aggregate_version=approved_proposal.resource_version,
            event_ids=(released_event.id, approved_event.id),
            data={
                "proposal_id": str(approved_proposal.id),
                "proposal_status": approved_proposal.status.value,
                "application_id": str(approved_application.id),
                "application_status": approved_application.status.value,
                "hold_id": str(lineage.hold.id),
                "release_id": str(release.id),
                "result": GovernanceResolutionResult.APPLIED.value,
            },
        )

    def _lock_lineage(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        workspace_id: UUID,
        proposal_identity: CollaborationReleaseProposal,
        hold_identity: CollaborationGovernanceHold,
        expected_version: int | None,
    ) -> _ApprovalLineage:
        if (
            proposal_identity.workspace_id != workspace_id
            or hold_identity.workspace_id != workspace_id
            or proposal_identity.hold_id != hold_identity.id
            or proposal_identity.run_id != hold_identity.run_id
        ):
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal and Hold identity is invalid",
            )
        head = uow.plans.get_head(
            hold_identity.root_task_id,
            workspace_id,
            for_update=True,
        )
        if head is None:
            _raise(
                "collaboration_governance_lineage_invalid",
                "current Plan Head was not found",
            )
        run = uow.collaboration_runs.get(
            workspace_id,
            proposal_identity.run_id,
            for_update=True,
        )
        if run is None:
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal Run was not found",
            )
        if (
            run.workspace_id != hold_identity.workspace_id
            or run.root_task_id != hold_identity.root_task_id
            or run.plan_snapshot_id != hold_identity.plan_snapshot_id
            or run.plan_generation != hold_identity.plan_generation
            or run.plan_content_hash != hold_identity.plan_content_hash
        ):
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal Run and Hold lineage is inconsistent",
            )
        worker_item = uow.worker_items.get(
            hold_identity.worker_item_id,
            workspace_id,
            for_update=True,
        )
        if (
            worker_item is None
            or worker_item.root_task_id != hold_identity.root_task_id
        ):
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal WorkerItem was not found",
            )
        request = uow.execution_requests.get(
            proposal_identity.execution_request_id,
            workspace_id,
            for_update=True,
        )
        hold = uow.collaboration_governance.get_hold(
            workspace_id,
            hold_identity.id,
            for_update=True,
        )
        proposal = uow.collaboration_governance.get_proposal(
            workspace_id,
            proposal_identity.id,
            for_update=True,
        )
        if hold is None or proposal is None:
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal lineage disappeared",
            )
        if (
            not _hold_identity_matches(hold, hold_identity)
            or not _proposal_identity_matches(proposal, proposal_identity)
            or proposal.hold_id != hold.id
            or proposal.run_id != run.id
        ):
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal lineage is invalid",
            )
        if expected_version != proposal.resource_version:
            raise VersionConflict(expected_version or 0, proposal.resource_version)
        if proposal.status is not ReleaseProposalStatus.PENDING:
            _raise(
                "collaboration_governance_proposal_not_pending",
                "the Proposal is no longer pending",
            )
        if hold.status is not GovernanceHoldStatus.ACTIVE:
            _raise(
                "collaboration_governance_hold_not_active",
                "the Proposal Hold is not active",
            )

        application = self._load_application(uow, workspace_id, proposal)
        if application.status is not RuntimeIntentApplicationStatus.DEFERRED:
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal Application is not deferred",
            )
        if (
            application.expected_request_version != proposal.target_request_version
            or application.expected_request_version
            != proposal_identity.target_request_version
        ):
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal Application request fence is inconsistent",
            )
        decision = uow.collaboration_ledger.get_decision(
            workspace_id, proposal.decision_id
        )
        intent = uow.collaboration_ledger.get_intent(workspace_id, proposal.intent_id)
        stored_projection = uow.collaboration_ledger.get_projection(
            workspace_id, proposal.projection_snapshot_id
        )
        if decision is None or intent is None or stored_projection is None:
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal immutable lineage is incomplete",
            )
        original_projection = _rehydrate_projection(stored_projection)
        input_selection = (
            uow.input_selections.get(workspace_id, proposal.input_selection_id)
            if proposal.input_selection_id is not None
            else None
        )
        if (
            decision.workspace_id != workspace_id
            or decision.run_id != run.id
            or decision.projection_snapshot_id != proposal.projection_snapshot_id
            or decision.projection_hash != proposal.projection_hash
            or intent.workspace_id != workspace_id
            or intent.run_id != run.id
            or intent.decision_id != decision.id
            or intent.target_execution_request_id != proposal.execution_request_id
            or intent.target_request_version != proposal.target_request_version
            or application.intent_id != intent.id
            or original_projection.projection_id != proposal.projection_snapshot_id
            or original_projection.content_hash != proposal.projection_hash
            or original_projection.workspace_id != workspace_id
            or original_projection.run_id != run.id
            or original_projection.root_task_id != run.root_task_id
            or original_projection.plan_snapshot_id != run.plan_snapshot_id
            or original_projection.plan_generation != run.plan_generation
            or original_projection.plan_content_hash != run.plan_content_hash
            or (
                proposal.input_selection_id is not None
                and (
                    input_selection is None
                    or input_selection.id != proposal.input_selection_id
                    or input_selection.selection_hash != proposal.input_selection_hash
                    or input_selection.workspace_id != workspace_id
                    or input_selection.root_task_id != run.root_task_id
                    or input_selection.run_id != run.id
                    or input_selection.plan_snapshot_id != run.plan_snapshot_id
                    or input_selection.plan_generation != run.plan_generation
                    or input_selection.plan_content_hash != run.plan_content_hash
                    or input_selection.target_worker_item_id != worker_item.id
                    or input_selection.target_execution_request_id
                    != proposal.execution_request_id
                    or input_selection.target_request_version
                    != proposal.target_request_version
                    or input_selection.projection_snapshot_id
                    != proposal.projection_snapshot_id
                    or input_selection.projection_hash != proposal.projection_hash
                    or input_selection.decision_id != proposal.decision_id
                    or input_selection.intent_id != proposal.intent_id
                )
            )
        ):
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal immutable lineage is inconsistent",
            )
        return _ApprovalLineage(
            head=head,
            run=run,
            worker_item=worker_item,
            request=request,
            hold=hold,
            proposal=proposal,
            application=application,
            decision=decision,
            intent=intent,
            original_projection=original_projection,
            input_selection=input_selection,
        )

    def _stale_reason(
        self,
        uow: ControlPlaneUnitOfWork,
        lineage: _ApprovalLineage,
        *,
        current_projection: RuntimeProjectionV1 | RuntimeProjectionV2,
    ) -> str | None:
        head = lineage.head
        run = lineage.run
        hold = lineage.hold
        if not _head_matches_hold(head, hold):
            return "plan_generation_stale"
        if (
            run.workspace_id != head.workspace_id
            or run.root_task_id != head.root_task_id
            or run.plan_snapshot_id != head.active_snapshot_id
            or run.plan_generation != head.active_generation
            or run.plan_content_hash != head.active_snapshot_hash
        ):
            return "plan_generation_stale"
        if run.status is not CollaborationRunStatus.ACTIVE:
            return "collaboration_run_not_active"
        placement = uow.worker_items.get_current_placement(
            lineage.worker_item.id, head.workspace_id
        )
        if not _placement_matches_head(placement, head, lineage.worker_item.id):
            return "worker_item_placement_stale"
        request = lineage.request
        if request is None:
            return "intent_target_invalid"
        if (
            request.workspace_id != head.workspace_id
            or request.root_task_id != head.root_task_id
            or request.plan_snapshot_id != head.active_snapshot_id
            or request.plan_generation != head.active_generation
            or request.worker_item_id != lineage.worker_item.id
        ):
            return "intent_target_invalid"
        if request.status not in {
            ExecutionRequestStatus.BLOCKED,
            ExecutionRequestStatus.READY,
        }:
            return "intent_target_invalid"
        if (
            request.collaboration_gate_mode
            is not CollaborationGateMode.RELEASE_REQUIRED
        ):
            return "intent_target_invalid"
        if request.version != lineage.intent.target_request_version:
            return "projection_stale"
        existing_release = uow.collaboration_ledger.get_release_for_request(
            request.workspace_id,
            request.id,
            for_update=True,
        )
        if existing_release is not None:
            return "decision_conflict"
        if (
            current_projection.workspace_id != head.workspace_id
            or current_projection.root_task_id != head.root_task_id
            or current_projection.run_id != run.id
            or current_projection.plan_snapshot_id != head.active_snapshot_id
            or current_projection.plan_generation != head.active_generation
            or current_projection.plan_content_hash != head.active_snapshot_hash
        ):
            return "projection_stale"
        return None

    @staticmethod
    def _can_assemble_current_projection(lineage: _ApprovalLineage) -> bool:
        head = lineage.head
        run = lineage.run
        return (
            _head_matches_hold(head, lineage.hold)
            and run.workspace_id == head.workspace_id
            and run.root_task_id == head.root_task_id
            and run.plan_snapshot_id == head.active_snapshot_id
            and run.plan_generation == head.active_generation
            and run.plan_content_hash == head.active_snapshot_hash
            and run.status is CollaborationRunStatus.ACTIVE
        )

    @staticmethod
    def _lineage_stale_reason(lineage: _ApprovalLineage) -> str:
        if not CollaborationGovernanceReleaseApplier._can_assemble_current_projection(
            lineage
        ):
            if lineage.run.status is not CollaborationRunStatus.ACTIVE:
                return "collaboration_run_not_active"
            return "plan_generation_stale"
        return "projection_stale"

    def _current_projection(
        self,
        uow: ControlPlaneUnitOfWork,
        run: CollaborationRun,
        *,
        now: datetime,
    ) -> tuple[RuntimeProjectionV1 | RuntimeProjectionV2, CollaborationFacts]:
        cursor = uow.projection_changes.latest_target_cursor(
            run.workspace_id,
            "collaboration_runtime",
            run.root_task_id,
        )
        facts = self._fact_store_factory(uow).load(
            run.workspace_id,
            run,
            fact_cursor=cursor,
        )
        if facts.plan_head.run_resource_version != run.resource_version:
            facts = replace(
                facts,
                plan_head=replace(
                    facts.plan_head,
                    run_resource_version=run.resource_version,
                ),
            )
        projection = self._projection_assembler.assemble(
            facts,
            fact_cursor=cursor,
            now=now,
        )
        if not isinstance(projection, (RuntimeProjectionV1, RuntimeProjectionV2)):
            raise RuntimeEnvelopeError("unsupported governance release Projection")
        return projection, facts

    def _can_form_current_recommendation(
        self,
        uow: ControlPlaneUnitOfWork,
        lineage: _ApprovalLineage,
        current_projection: RuntimeProjectionV1 | RuntimeProjectionV2,
    ) -> bool:
        request = lineage.request
        placement = (
            uow.worker_items.get_current_placement(
                lineage.worker_item.id,
                lineage.head.workspace_id,
            )
            if request is not None
            else None
        )
        return (
            lineage.run.status is CollaborationRunStatus.ACTIVE
            and request is not None
            and request.worker_item_id == lineage.worker_item.id
            and _placement_matches_head(placement, lineage.head, lineage.worker_item.id)
            and request.status
            in {ExecutionRequestStatus.BLOCKED, ExecutionRequestStatus.READY}
            and request.collaboration_gate_mode
            is CollaborationGateMode.RELEASE_REQUIRED
            and current_projection.run_id == lineage.run.id
            and current_projection.plan_generation == lineage.run.plan_generation
            and any(
                item.get("id") == str(request.id) and item.get("eligible") is True
                for item in current_projection.execution_requests
            )
        )

    def _resolve_stale(
        self,
        uow: ControlPlaneUnitOfWork,
        command: CommandEnvelope,
        lineage: _ApprovalLineage,
        *,
        reason: str,
        now: datetime,
        wake_runtime: bool,
    ) -> CommandResult:
        stale_proposal = lineage.proposal.stale(now=now)
        uow.collaboration_governance.save_proposal(
            stale_proposal,
            lineage.proposal.resource_version,
        )
        stale_application = lineage.application.resolve(
            status=RuntimeIntentApplicationStatus.STALE,
            result_code="governance_approval_stale",
            result_summary=f"the human approval became stale: {reason}",
            release_id=None,
            now=now,
        )
        uow.collaboration_ledger.save_application(
            stale_application,
            lineage.application.resource_version,
        )
        resolution = CollaborationGovernanceResolution.create(
            id=uuid4(),
            workspace_id=command.workspace_id,
            proposal_id=lineage.proposal.id,
            action=GovernanceResolutionAction.APPROVE,
            result=GovernanceResolutionResult.STALE,
            actor_id=command.actor_id,
            transport=_transport(command.payload),
            reason=_reason(command.payload),
            command_id=command.id,
            now=now,
        )
        uow.collaboration_governance.add_resolution(resolution)
        event = self._event(
            command,
            aggregate_type="collaboration_release_proposal",
            aggregate_id=stale_proposal.id,
            aggregate_version=stale_proposal.resource_version,
            event_type="CollaborationReleaseProposalStale",
            run=lineage.run,
            payload={
                "proposal_id": str(stale_proposal.id),
                "hold_id": str(stale_proposal.hold_id),
                "execution_request_id": str(stale_proposal.execution_request_id),
                "intent_id": str(stale_proposal.intent_id),
                "status": stale_proposal.status.value,
                "result": GovernanceResolutionResult.STALE.value,
                "code": reason,
            },
        )
        uow.events.append_with_outbox(event)
        self._publish_governance_change(
            uow,
            command,
            root_task_id=lineage.head.root_task_id,
            resource_version=stale_proposal.resource_version,
            change_type="collaboration_release_proposal_stale",
        )
        if wake_runtime:
            uow.projection_changes.changed(
                workspace_id=command.workspace_id,
                projection_type="collaboration_runtime",
                resource_id=lineage.head.root_task_id,
                change_type="collaboration_governance_approval_stale",
                resource_version=lineage.run.resource_version,
                correlation_id=command.correlation_id,
            )
        return CommandResult(
            command_id=command.id,
            aggregate_id=stale_proposal.id,
            aggregate_version=stale_proposal.resource_version,
            event_ids=(event.id,),
            data={
                "proposal_id": str(stale_proposal.id),
                "proposal_status": stale_proposal.status.value,
                "application_id": str(stale_application.id),
                "application_status": stale_application.status.value,
                "hold_id": str(lineage.hold.id),
                "result": GovernanceResolutionResult.STALE.value,
                "stale_reason": reason,
            },
        )

    @staticmethod
    def _load_application(
        uow: ControlPlaneUnitOfWork,
        workspace_id: UUID,
        proposal: CollaborationReleaseProposal,
    ) -> RuntimeIntentApplication:
        application_id = uuid5(
            NAMESPACE_URL, f"sidera:collaboration-application:{proposal.intent_id}"
        )
        application = uow.collaboration_ledger.get_application(
            workspace_id, application_id, for_update=True
        )
        if application is None or application.intent_id != proposal.intent_id:
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal Application was not found",
            )
        return application

    def _event(
        self,
        command: CommandEnvelope,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        aggregate_version: int,
        event_type: str,
        run: CollaborationRun,
        payload: dict[str, object],
    ) -> EventData:
        return EventData(
            id=uuid4(),
            workspace_id=command.workspace_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
            event_type=event_type,
            payload={
                "root_task_id": str(run.root_task_id),
                "run_id": str(run.id),
                "plan_snapshot_id": str(run.plan_snapshot_id),
                "plan_generation": run.plan_generation,
                **payload,
            },
            actor_type=command.actor_type,
            actor_id=command.actor_id,
            causation_id=command.id,
            correlation_id=command.correlation_id,
        )

    @staticmethod
    def _publish_governance_change(
        uow: ControlPlaneUnitOfWork,
        command: CommandEnvelope,
        *,
        root_task_id: UUID,
        resource_version: int,
        change_type: str,
    ) -> None:
        uow.projection_changes.changed(
            workspace_id=command.workspace_id,
            projection_type="collaboration_governance",
            resource_id=root_task_id,
            change_type=change_type,
            resource_version=resource_version,
            correlation_id=command.correlation_id,
        )


def _rehydrate_projection(
    snapshot: Any,
) -> RuntimeProjectionV1 | RuntimeProjectionV2:
    try:
        schema_version = snapshot.payload.get("schema_version")
        projection_type = (
            RuntimeProjectionV2 if schema_version == 2 else RuntimeProjectionV1
        )
        projection = projection_type.from_mapping(
            snapshot.payload, created_at=snapshot.created_at
        )
    except (RuntimeEnvelopeError, TypeError, ValueError) as exc:
        raise CollaborationGovernanceCommandError(
            "collaboration_governance_lineage_invalid",
            "stored Proposal Projection is invalid",
        ) from exc
    if (
        projection.projection_id != snapshot.id
        or projection.workspace_id != snapshot.workspace_id
        or projection.run_id != snapshot.run_id
        or projection.content_hash != snapshot.content_hash
    ):
        _raise(
            "collaboration_governance_lineage_invalid",
            "stored Proposal Projection identity is invalid",
        )
    return projection


def _head_matches_hold(
    head: RootTaskPlanHead, hold: CollaborationGovernanceHold
) -> bool:
    return (
        head.workspace_id == hold.workspace_id
        and head.root_task_id == hold.root_task_id
        and head.active_snapshot_id == hold.plan_snapshot_id
        and head.active_generation == hold.plan_generation
        and head.active_snapshot_hash == hold.plan_content_hash
    )


def _hold_identity_matches(
    hold: CollaborationGovernanceHold,
    identity: CollaborationGovernanceHold,
) -> bool:
    return (
        hold.id == identity.id
        and hold.workspace_id == identity.workspace_id
        and hold.root_task_id == identity.root_task_id
        and hold.run_id == identity.run_id
        and hold.plan_snapshot_id == identity.plan_snapshot_id
        and hold.plan_generation == identity.plan_generation
        and hold.plan_content_hash == identity.plan_content_hash
        and hold.worker_item_id == identity.worker_item_id
        and hold.created_by_actor_id == identity.created_by_actor_id
        and hold.created_via == identity.created_via
        and hold.created_at == identity.created_at
    )


def _proposal_identity_matches(
    proposal: CollaborationReleaseProposal,
    identity: CollaborationReleaseProposal,
) -> bool:
    return (
        proposal.id == identity.id
        and proposal.workspace_id == identity.workspace_id
        and proposal.hold_id == identity.hold_id
        and proposal.run_id == identity.run_id
        and proposal.projection_snapshot_id == identity.projection_snapshot_id
        and proposal.projection_hash == identity.projection_hash
        and proposal.decision_id == identity.decision_id
        and proposal.intent_id == identity.intent_id
        and proposal.execution_request_id == identity.execution_request_id
        and proposal.target_request_version == identity.target_request_version
        and proposal.input_fingerprint_version == identity.input_fingerprint_version
        and proposal.input_fingerprint == identity.input_fingerprint
        and proposal.input_selection_id == identity.input_selection_id
        and proposal.input_selection_hash == identity.input_selection_hash
        and proposal.reason_code == identity.reason_code
        and proposal.summary == identity.summary
        and proposal.created_at == identity.created_at
    )


def _placement_matches_head(
    placement: Any,
    head: RootTaskPlanHead,
    worker_item_id: UUID,
) -> bool:
    return placement is not None and (
        placement.workspace_id == head.workspace_id
        and placement.root_task_id == head.root_task_id
        and placement.worker_item_id == worker_item_id
        and placement.plan_snapshot_id == head.active_snapshot_id
        and placement.plan_generation == head.active_generation
    )


def _transport(payload: Mapping[str, object]) -> GovernanceTransport:
    try:
        return GovernanceTransport(payload.get("transport"))
    except (TypeError, ValueError):
        _raise(
            "collaboration_governance_target_invalid", "governance transport is invalid"
        )


def _reason(payload: Mapping[str, object]) -> str | None:
    value = payload.get("reason")
    if value is None:
        return None
    if not isinstance(value, str):
        _raise("collaboration_governance_target_invalid", "reason is invalid")
    clean = value.strip()
    if not clean:
        return None
    if len(clean) > 2000 or any(
        ord(char) < 32 and char not in "\n\t" for char in clean
    ):
        _raise("collaboration_governance_target_invalid", "reason is invalid")
    return clean


def _validate_selection_expectation(
    payload: Mapping[str, object],
    proposal: CollaborationReleaseProposal,
) -> None:
    raw_id = payload.get("expected_input_selection_id")
    raw_hash = payload.get("expected_input_selection_hash")
    if proposal.input_selection_id is None:
        if raw_id is not None or raw_hash is not None:
            _raise(
                "collaboration_governance_selection_unexpected",
                "legacy Proposal does not have an input Selection",
            )
        return
    if raw_id is None or raw_hash is None:
        _raise(
            "collaboration_governance_selection_expected",
            "approval requires the reviewed input Selection identity",
        )
    try:
        expected_id = UUID(str(raw_id))
    except (TypeError, ValueError) as exc:
        raise CollaborationGovernanceCommandError(
            "collaboration_governance_selection_expected",
            "approval input Selection id is invalid",
        ) from exc
    if (
        expected_id != proposal.input_selection_id
        or raw_hash != proposal.input_selection_hash
    ):
        _raise(
            "collaboration_governance_selection_conflict",
            "approval input Selection no longer matches the Proposal",
        )


def _governance_fence(hold: CollaborationGovernanceHold) -> dict[str, object]:
    return {
        "hold_id": str(hold.id),
        "hold_resource_version": hold.resource_version,
        "hold_status": hold.status.value,
        "worker_item_id": str(hold.worker_item_id),
    }


def _raise(code: str, message: str) -> NoReturn:
    raise CollaborationGovernanceCommandError(code, message)


def _validate_now(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
