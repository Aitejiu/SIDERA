from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.collaboration import (
    CollaborationRunStatus,
    RuntimeIntentApplication,
    RuntimeIntentApplicationStatus,
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
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import DomainError, VersionConflict
from sidera_control_plane.domain.events import EventData


class CollaborationGovernanceCommandError(DomainError):
    """Stable public rejection codes for human release governance commands."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.code = code


class GovernanceReleaseApprover(Protocol):
    def approve(self, command: CommandEnvelope, *, now: datetime) -> CommandResult:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _CommandPayload:
    transport: GovernanceTransport
    reason: str | None
    target_plan_generation: int | None = None


@dataclass(frozen=True, slots=True)
class _CurrentLineage:
    head: Any
    run: Any
    worker_item: Any
    placement: Any


class CollaborationGovernanceCommandHandler:
    command_types = frozenset(
        {
            "PauseCollaborationRelease",
            "ApproveCollaborationReleaseProposal",
            "RejectCollaborationReleaseProposal",
            "ResumeAutomaticCollaboration",
        }
    )

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        *,
        local_actor_type: str = "local",
        local_actor_id: str = "aitejiu",
        enabled: bool = True,
        approver: GovernanceReleaseApprover | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._local_actor_type = local_actor_type
        self._local_actor_id = local_actor_id
        self._enabled = enabled
        self._approver = approver

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if not self._enabled:
            _raise(
                "collaboration_governance_disabled", "governance commands are disabled"
            )
        self._validate_actor(envelope)

        if envelope.command_type == "PauseCollaborationRelease":
            return self._pause(uow, envelope)
        if envelope.command_type == "RejectCollaborationReleaseProposal":
            return self._reject(uow, envelope)
        if envelope.command_type == "ResumeAutomaticCollaboration":
            return self._resume(uow, envelope)
        if envelope.command_type == "ApproveCollaborationReleaseProposal":
            self._validate_target(
                envelope,
                target_type="collaboration_release_proposal",
            )
            self._parse_payload(envelope.payload, allowed={"transport", "reason"})
            if self._approver is None:
                _raise(
                    "collaboration_governance_disabled",
                    "approval service is not configured",
                )
            transactional = getattr(self._approver, "approve_in_transaction", None)
            if callable(transactional):
                return cast(Callable[..., CommandResult], transactional)(
                    uow, envelope, now=self._clock()
                )
            return self._approver.approve(envelope, now=self._clock())
        _raise(
            "collaboration_governance_target_invalid", "unsupported governance command"
        )

    def _pause(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        self._validate_target(envelope, target_type="worker_item")
        payload = self._parse_payload(
            envelope.payload,
            allowed={"transport", "reason", "target_plan_generation"},
            require_generation=True,
        )
        assert envelope.target_id is not None
        assert payload.target_plan_generation is not None

        # This unlocked read only discovers the owning root. The authoritative
        # WorkerItem is loaded again after Head and Run are locked.
        identity = uow.worker_items.get(envelope.target_id, envelope.workspace_id)
        if identity is None:
            _raise(
                "collaboration_governance_target_invalid", "WorkerItem was not found"
            )
        lineage = self._load_current_lineage_for_pause(
            uow,
            workspace_id=envelope.workspace_id,
            root_task_id=identity.root_task_id,
            worker_item_id=envelope.target_id,
            target_generation=payload.target_plan_generation,
        )
        if lineage.worker_item.version != envelope.expected_version:
            assert envelope.expected_version is not None
            raise VersionConflict(
                envelope.expected_version, lineage.worker_item.version
            )

        active_hold = uow.collaboration_governance.get_active_hold(
            envelope.workspace_id,
            lineage.run.id,
            lineage.worker_item.id,
            for_update=True,
        )
        if active_hold is not None:
            _raise(
                "collaboration_governance_hold_exists",
                "an active hold already exists for this WorkerItem",
                details={"hold_id": str(active_hold.id)},
            )

        now = self._clock()
        hold = CollaborationGovernanceHold.create(
            id=uuid4(),
            workspace_id=envelope.workspace_id,
            root_task_id=lineage.head.root_task_id,
            run_id=lineage.run.id,
            plan_snapshot_id=lineage.head.active_snapshot_id,
            plan_generation=lineage.head.active_generation,
            plan_content_hash=lineage.head.active_snapshot_hash,
            worker_item_id=lineage.worker_item.id,
            created_by_actor_id=envelope.actor_id,
            created_via=payload.transport,
            reason=payload.reason,
            now=now,
        )
        uow.collaboration_governance.add_hold(hold)
        event = self._event(
            envelope,
            aggregate_type="collaboration_governance_hold",
            aggregate_id=hold.id,
            aggregate_version=hold.resource_version,
            event_type="CollaborationReleasePaused",
            payload=_hold_payload(hold, code="collaboration_governance_paused"),
        )
        uow.events.append_with_outbox(event)
        self._publish_governance_change(
            uow,
            envelope,
            root_task_id=hold.root_task_id,
            resource_version=hold.resource_version,
            change_type="collaboration_release_paused",
        )
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=hold.id,
            aggregate_version=hold.resource_version,
            event_ids=(event.id,),
            data={
                "hold_id": str(hold.id),
                "hold_version": hold.resource_version,
                "governance_mode": "paused",
            },
        )

    def _reject(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        self._validate_target(
            envelope,
            target_type="collaboration_release_proposal",
        )
        payload = self._parse_payload(envelope.payload, allowed={"transport", "reason"})
        assert envelope.target_id is not None

        # Read proposal and hold without locks only to discover the lock root.
        proposal_identity = uow.collaboration_governance.get_proposal(
            envelope.workspace_id, envelope.target_id
        )
        if proposal_identity is None:
            _raise("collaboration_governance_target_invalid", "Proposal was not found")
        hold_identity = uow.collaboration_governance.get_hold(
            envelope.workspace_id, proposal_identity.hold_id
        )
        if hold_identity is None:
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal Hold was not found",
            )

        lineage = self._load_current_lineage(
            uow,
            workspace_id=envelope.workspace_id,
            root_task_id=hold_identity.root_task_id,
            run_id=proposal_identity.run_id,
            worker_item_id=hold_identity.worker_item_id,
            plan_snapshot_id=hold_identity.plan_snapshot_id,
            plan_generation=hold_identity.plan_generation,
            plan_content_hash=hold_identity.plan_content_hash,
        )
        request = uow.execution_requests.get(
            proposal_identity.execution_request_id,
            envelope.workspace_id,
            for_update=True,
        )
        if request is None:
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal request was not found",
            )
        if not _request_matches_lineage(request, lineage):
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal request lineage is stale",
            )

        hold = uow.collaboration_governance.get_hold(
            envelope.workspace_id, proposal_identity.hold_id, for_update=True
        )
        proposal = uow.collaboration_governance.get_proposal(
            envelope.workspace_id, proposal_identity.id, for_update=True
        )
        if hold is None or proposal is None:
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal lineage disappeared",
            )
        if not _hold_matches_lineage(hold, lineage) or proposal.run_id != hold.run_id:
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal and Hold lineage disagree",
            )
        if proposal.execution_request_id != request.id:
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal request identity is invalid",
            )
        if hold.status is not GovernanceHoldStatus.ACTIVE:
            _raise(
                "collaboration_governance_hold_not_active",
                "the Proposal Hold is not active",
            )
        self._check_version(envelope.expected_version, proposal.resource_version)
        if proposal.status is not ReleaseProposalStatus.PENDING:
            _raise(
                "collaboration_governance_proposal_not_pending",
                "the Proposal is no longer pending",
            )

        application = self._load_application(uow, envelope.workspace_id, proposal)
        if application.status is not RuntimeIntentApplicationStatus.DEFERRED:
            _raise(
                "collaboration_governance_lineage_invalid",
                "Proposal Application is not deferred",
            )

        now = self._clock()
        rejected_proposal = proposal.reject(now=now)
        uow.collaboration_governance.save_proposal(
            rejected_proposal, proposal.resource_version
        )
        rejected_application = application.resolve(
            status=RuntimeIntentApplicationStatus.REJECTED,
            result_code="governance_rejected",
            result_summary="the human release proposal was rejected",
            release_id=None,
            now=now,
        )
        uow.collaboration_ledger.save_application(
            rejected_application, application.resource_version
        )
        resolution = CollaborationGovernanceResolution.create(
            id=uuid4(),
            workspace_id=envelope.workspace_id,
            proposal_id=proposal.id,
            action=GovernanceResolutionAction.REJECT,
            result=GovernanceResolutionResult.APPLIED,
            actor_id=envelope.actor_id,
            transport=payload.transport,
            reason=payload.reason,
            command_id=envelope.id,
            now=now,
        )
        uow.collaboration_governance.add_resolution(resolution)
        event = self._event(
            envelope,
            aggregate_type="collaboration_release_proposal",
            aggregate_id=rejected_proposal.id,
            aggregate_version=rejected_proposal.resource_version,
            event_type="CollaborationReleaseProposalRejected",
            payload=_proposal_payload(
                rejected_proposal,
                hold=hold,
                worker_item_id=lineage.worker_item.id,
                code="governance_rejected",
            ),
        )
        uow.events.append_with_outbox(event)
        self._publish_governance_change(
            uow,
            envelope,
            root_task_id=lineage.head.root_task_id,
            resource_version=rejected_proposal.resource_version,
            change_type="collaboration_release_proposal_rejected",
        )
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=rejected_proposal.id,
            aggregate_version=rejected_proposal.resource_version,
            event_ids=(event.id,),
            data={
                "proposal_id": str(rejected_proposal.id),
                "proposal_status": rejected_proposal.status.value,
                "application_id": str(rejected_application.id),
                "application_status": rejected_application.status.value,
                "hold_id": str(hold.id),
            },
        )

    def _resume(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        self._validate_target(
            envelope,
            target_type="collaboration_governance_hold",
        )
        payload = self._parse_payload(envelope.payload, allowed={"transport", "reason"})
        del payload
        assert envelope.target_id is not None

        hold_identity = uow.collaboration_governance.get_hold(
            envelope.workspace_id, envelope.target_id
        )
        if hold_identity is None:
            _raise("collaboration_governance_target_invalid", "Hold was not found")
        lineage = self._load_current_lineage(
            uow,
            workspace_id=envelope.workspace_id,
            root_task_id=hold_identity.root_task_id,
            run_id=hold_identity.run_id,
            worker_item_id=hold_identity.worker_item_id,
            plan_snapshot_id=hold_identity.plan_snapshot_id,
            plan_generation=hold_identity.plan_generation,
            plan_content_hash=hold_identity.plan_content_hash,
        )
        hold = uow.collaboration_governance.get_hold(
            envelope.workspace_id, hold_identity.id, for_update=True
        )
        if hold is None:
            _raise("collaboration_governance_target_invalid", "Hold was not found")
        if not _hold_matches_lineage(hold, lineage):
            _raise(
                "collaboration_governance_lineage_invalid",
                "Hold lineage changed while acquiring its lock",
            )
        self._check_version(envelope.expected_version, hold.resource_version)
        if hold.status is not GovernanceHoldStatus.ACTIVE:
            _raise("collaboration_governance_hold_not_active", "Hold is not active")

        pending = uow.collaboration_governance.list_pending_proposals_for_hold(
            envelope.workspace_id, hold.id, for_update=True
        )
        applications: list[
            tuple[CollaborationReleaseProposal, RuntimeIntentApplication]
        ] = []
        for proposal in pending:
            if (
                proposal.run_id != hold.run_id
                or proposal.workspace_id != hold.workspace_id
            ):
                _raise(
                    "collaboration_governance_lineage_invalid",
                    "pending Proposal lineage is invalid",
                )
            application = self._load_application(uow, envelope.workspace_id, proposal)
            if application.status is not RuntimeIntentApplicationStatus.DEFERRED:
                _raise(
                    "collaboration_governance_lineage_invalid",
                    "pending Proposal Application is not deferred",
                )
            applications.append((proposal, application))

        now = self._clock()
        resumed_hold = hold.resume(now=now)
        uow.collaboration_governance.save_hold(resumed_hold, hold.resource_version)
        event_ids: list[UUID] = []
        resume_event = self._event(
            envelope,
            aggregate_type="collaboration_governance_hold",
            aggregate_id=resumed_hold.id,
            aggregate_version=resumed_hold.resource_version,
            event_type="CollaborationAutomaticReleaseResumed",
            payload=_hold_payload(
                resumed_hold,
                code="collaboration_governance_resumed",
            ),
        )
        event_ids.append(resume_event.id)

        stale_events: list[EventData] = []
        for proposal, application in applications:
            stale_proposal = proposal.stale(now=now)
            uow.collaboration_governance.save_proposal(
                stale_proposal, proposal.resource_version
            )
            stale_application = application.resolve(
                status=RuntimeIntentApplicationStatus.STALE,
                result_code="governance_resumed",
                result_summary="automatic collaboration was resumed",
                release_id=None,
                now=now,
            )
            uow.collaboration_ledger.save_application(
                stale_application, application.resource_version
            )
            stale_event = self._event(
                envelope,
                aggregate_type="collaboration_release_proposal",
                aggregate_id=stale_proposal.id,
                aggregate_version=stale_proposal.resource_version,
                event_type="CollaborationReleaseProposalStale",
                payload=_proposal_payload(
                    stale_proposal,
                    hold=resumed_hold,
                    worker_item_id=lineage.worker_item.id,
                    code="governance_resumed",
                ),
            )
            stale_events.append(stale_event)
            event_ids.append(stale_event.id)

        # EventRepository enforces causation_id -> commands.id. Keep every
        # state update ahead of event insertion so CommandService can record
        # the public command before the transaction flushes these events.
        uow.events.append_with_outbox(resume_event)
        for stale_event in stale_events:
            uow.events.append_with_outbox(stale_event)

        self._publish_governance_change(
            uow,
            envelope,
            root_task_id=lineage.head.root_task_id,
            resource_version=resumed_hold.resource_version,
            change_type="collaboration_automatic_release_resumed",
        )
        self._publish_runtime_invalidation(
            uow,
            envelope,
            root_task_id=lineage.head.root_task_id,
            resource_version=resumed_hold.resource_version,
        )
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=resumed_hold.id,
            aggregate_version=resumed_hold.resource_version,
            event_ids=tuple(event_ids),
            data={
                "hold_id": str(resumed_hold.id),
                "hold_version": resumed_hold.resource_version,
                "governance_mode": "automatic",
                "staled_proposal_count": len(applications),
            },
        )

    def _load_current_lineage_for_pause(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        workspace_id: UUID,
        root_task_id: UUID,
        worker_item_id: UUID,
        target_generation: int,
    ) -> _CurrentLineage:
        head = uow.plans.get_head(root_task_id, workspace_id, for_update=True)
        if head is None:
            _raise(
                "collaboration_governance_target_invalid",
                "current Plan Head was not found",
            )
        if head.active_generation != target_generation:
            _raise(
                "collaboration_governance_generation_stale",
                "target plan generation is stale",
            )
        run = uow.collaboration_runs.get_for_plan(
            workspace_id,
            head.active_snapshot_id,
            head.active_generation,
            for_update=True,
        )
        if run is None or not _run_is_current(run, head):
            _raise(
                "collaboration_governance_generation_stale",
                "current collaboration Run is stale",
            )
        worker_item = uow.worker_items.get(
            worker_item_id, workspace_id, for_update=True
        )
        if worker_item is None or worker_item.root_task_id != root_task_id:
            _raise(
                "collaboration_governance_target_invalid",
                "WorkerItem is outside the current root",
            )
        placement = uow.worker_items.get_current_placement(worker_item_id, workspace_id)
        if not _placement_is_current(placement, head, root_task_id, worker_item_id):
            _raise(
                "collaboration_governance_target_invalid", "WorkerItem is not current"
            )
        return _CurrentLineage(head, run, worker_item, placement)

    def _load_current_lineage(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        workspace_id: UUID,
        root_task_id: UUID,
        run_id: UUID,
        worker_item_id: UUID,
        plan_snapshot_id: UUID,
        plan_generation: int,
        plan_content_hash: str,
    ) -> _CurrentLineage:
        head = uow.plans.get_head(root_task_id, workspace_id, for_update=True)
        if head is None:
            _raise(
                "collaboration_governance_target_invalid",
                "current Plan Head was not found",
            )
        if (
            head.active_snapshot_id != plan_snapshot_id
            or head.active_generation != plan_generation
            or head.active_snapshot_hash != plan_content_hash
        ):
            _raise(
                "collaboration_governance_generation_stale",
                "governance lineage is stale",
            )
        run = uow.collaboration_runs.get(workspace_id, run_id, for_update=True)
        if run is None or not _run_is_current(run, head):
            _raise(
                "collaboration_governance_generation_stale",
                "collaboration Run is stale",
            )
        worker_item = uow.worker_items.get(
            worker_item_id, workspace_id, for_update=True
        )
        if worker_item is None or worker_item.root_task_id != root_task_id:
            _raise(
                "collaboration_governance_target_invalid",
                "WorkerItem is outside the current root",
            )
        placement = uow.worker_items.get_current_placement(worker_item_id, workspace_id)
        if not _placement_is_current(placement, head, root_task_id, worker_item_id):
            _raise(
                "collaboration_governance_target_invalid", "WorkerItem is not current"
            )
        return _CurrentLineage(head, run, worker_item, placement)

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

    def _validate_actor(self, envelope: CommandEnvelope) -> None:
        if (
            envelope.actor_type != self._local_actor_type
            or envelope.actor_id != self._local_actor_id
        ):
            _raise(
                "collaboration_governance_actor_forbidden",
                "governance commands require the configured local actor",
            )

    @staticmethod
    def _validate_target(envelope: CommandEnvelope, *, target_type: str) -> None:
        if (
            envelope.target_type != target_type
            or envelope.target_id is None
            or envelope.expected_version is None
            or envelope.expected_version < 1
        ):
            _raise(
                "collaboration_governance_target_invalid",
                "governance command target and expected version are invalid",
            )

    @staticmethod
    def _check_version(expected: int | None, actual: int) -> None:
        if expected is None or expected < 1:
            _raise(
                "collaboration_governance_target_invalid", "expected version is invalid"
            )
        if expected != actual:
            raise VersionConflict(expected, actual)

    @staticmethod
    def _parse_payload(
        payload: Mapping[str, object],
        *,
        allowed: set[str],
        require_generation: bool = False,
    ) -> _CommandPayload:
        if not isinstance(payload, Mapping) or set(payload) - allowed:
            _raise(
                "collaboration_governance_target_invalid",
                "governance payload keys are invalid",
            )
        raw_transport = payload.get("transport")
        try:
            transport = GovernanceTransport(raw_transport)
        except (TypeError, ValueError) as exc:
            raise CollaborationGovernanceCommandError(
                "collaboration_governance_target_invalid",
                "governance transport is invalid",
            ) from exc
        reason = _reason(payload.get("reason"))
        generation: int | None = None
        if require_generation:
            raw_generation = payload.get("target_plan_generation")
            if (
                not isinstance(raw_generation, int)
                or isinstance(raw_generation, bool)
                or raw_generation < 1
            ):
                _raise(
                    "collaboration_governance_target_invalid",
                    "target plan generation is invalid",
                )
            generation = raw_generation
        return _CommandPayload(transport, reason, generation)

    @staticmethod
    def _event(
        envelope: CommandEnvelope,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        aggregate_version: int,
        event_type: str,
        payload: dict[str, object],
    ) -> EventData:
        return EventData(
            id=uuid4(),
            workspace_id=envelope.workspace_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
            event_type=event_type,
            payload=payload,
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )

    @staticmethod
    def _publish_governance_change(
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
        *,
        root_task_id: UUID,
        resource_version: int,
        change_type: str,
    ) -> None:
        uow.projection_changes.changed(
            workspace_id=envelope.workspace_id,
            projection_type="collaboration_governance",
            resource_id=root_task_id,
            change_type=change_type,
            resource_version=resource_version,
            correlation_id=envelope.correlation_id,
        )

    @staticmethod
    def _publish_runtime_invalidation(
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
        *,
        root_task_id: UUID,
        resource_version: int,
    ) -> None:
        uow.projection_changes.changed(
            workspace_id=envelope.workspace_id,
            projection_type="collaboration_runtime",
            resource_id=root_task_id,
            change_type="collaboration_governance_resumed",
            resource_version=resource_version,
            correlation_id=envelope.correlation_id,
        )


def _raise(
    code: str,
    message: str,
    *,
    details: dict[str, object] | None = None,
) -> NoReturn:
    raise CollaborationGovernanceCommandError(code, message, details=details)


def _reason(value: object) -> str | None:
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


def _run_is_current(run: Any, head: Any) -> bool:
    status = (
        run.status.value
        if isinstance(run.status, CollaborationRunStatus)
        else str(run.status)
    )
    return (
        run.workspace_id == head.workspace_id
        and run.root_task_id == head.root_task_id
        and run.plan_snapshot_id == head.active_snapshot_id
        and run.plan_generation == head.active_generation
        and run.plan_content_hash == head.active_snapshot_hash
        and status
        not in {
            CollaborationRunStatus.COMPLETED.value,
            CollaborationRunStatus.SUPERSEDED.value,
        }
    )


def _placement_is_current(
    placement: Any,
    head: Any,
    root_task_id: UUID,
    worker_item_id: UUID,
) -> bool:
    return placement is not None and (
        placement.workspace_id == head.workspace_id
        and placement.root_task_id == root_task_id
        and placement.worker_item_id == worker_item_id
        and placement.plan_snapshot_id == head.active_snapshot_id
        and placement.plan_generation == head.active_generation
    )


def _request_matches_lineage(request: Any, lineage: _CurrentLineage) -> bool:
    return (
        request.workspace_id == lineage.head.workspace_id
        and request.root_task_id == lineage.head.root_task_id
        and request.plan_snapshot_id == lineage.head.active_snapshot_id
        and request.plan_generation == lineage.head.active_generation
        and request.worker_item_id == lineage.worker_item.id
    )


def _hold_matches_lineage(
    hold: CollaborationGovernanceHold, lineage: _CurrentLineage
) -> bool:
    return (
        hold.workspace_id == lineage.head.workspace_id
        and hold.root_task_id == lineage.head.root_task_id
        and hold.run_id == lineage.run.id
        and hold.plan_snapshot_id == lineage.head.active_snapshot_id
        and hold.plan_generation == lineage.head.active_generation
        and hold.plan_content_hash == lineage.head.active_snapshot_hash
        and hold.worker_item_id == lineage.worker_item.id
    )


def _hold_payload(hold: CollaborationGovernanceHold, *, code: str) -> dict[str, object]:
    return {
        "root_task_id": str(hold.root_task_id),
        "run_id": str(hold.run_id),
        "plan_snapshot_id": str(hold.plan_snapshot_id),
        "plan_generation": hold.plan_generation,
        "worker_item_id": str(hold.worker_item_id),
        "hold_id": str(hold.id),
        "status": hold.status.value,
        "resource_version": hold.resource_version,
        "code": code,
    }


def _proposal_payload(
    proposal: CollaborationReleaseProposal,
    *,
    hold: CollaborationGovernanceHold,
    worker_item_id: UUID,
    code: str,
) -> dict[str, object]:
    return {
        "root_task_id": str(hold.root_task_id),
        "run_id": str(proposal.run_id),
        "plan_snapshot_id": str(hold.plan_snapshot_id),
        "plan_generation": hold.plan_generation,
        "worker_item_id": str(worker_item_id),
        "hold_id": str(hold.id),
        "proposal_id": str(proposal.id),
        "intent_id": str(proposal.intent_id),
        "execution_request_id": str(proposal.execution_request_id),
        "status": proposal.status.value,
        "resource_version": proposal.resource_version,
        "code": code,
    }
