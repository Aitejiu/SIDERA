from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NoReturn
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.collaboration import (
    CollaborationRun,
    RuntimeIntentApplication,
    RuntimeIntentApplicationStatus,
)
from sidera_control_plane.domain.collaboration_governance import (
    CollaborationGovernanceHold,
    CollaborationReleaseProposal,
    GovernanceHoldStatus,
    ReleaseProposalStatus,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.events import EventData


class CollaborationGovernanceReconciliationError(DomainError):
    """A governance lineage cannot be safely converged."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class GovernanceSupersedeResult:
    superseded_holds: int = 0
    staled_proposals: int = 0
    staled_applications: int = 0


@dataclass(frozen=True, slots=True)
class GovernanceRecoveryResult:
    candidates: int = 0
    repaired: int = 0
    failed: int = 0
    failure_codes: tuple[str, ...] = ()


class CollaborationGovernanceReconciler:
    """Converge generation-scoped governance without invoking Runtime."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], ControlPlaneUnitOfWork] | None = None,
    ) -> None:
        self._uow_factory = uow_factory

    def supersede_run(
        self,
        uow: ControlPlaneUnitOfWork,
        run: CollaborationRun,
        *,
        now: datetime,
        actor_id: str,
        correlation_id: UUID,
    ) -> GovernanceSupersedeResult:
        _validate_context(now, actor_id, correlation_id)

        # All callers use the same order as governance commands:
        # Plan Head -> Run -> WorkerItem -> ExecutionRequest -> Hold -> Proposal -> Application.
        head = uow.plans.get_head(
            run.root_task_id,
            run.workspace_id,
            for_update=True,
        )
        if head is None:
            _raise(
                "collaboration_governance_plan_head_missing",
                "the current Plan Head was not found",
            )
        locked_run = uow.collaboration_runs.get(
            run.workspace_id,
            run.id,
            for_update=True,
        )
        if locked_run is None or not _run_identity_matches(locked_run, run):
            _raise(
                "collaboration_governance_run_lineage_invalid",
                "the collaboration Run lineage changed",
            )

        hold_identities = tuple(
            sorted(
                uow.collaboration_governance.list_active_holds_for_run(
                    run.workspace_id,
                    run.id,
                ),
                key=lambda item: item.id,
            )
        )
        proposal_identities = tuple(
            sorted(
                uow.collaboration_governance.list_pending_proposals_for_run(
                    run.workspace_id,
                    run.id,
                ),
                key=lambda item: item.id,
            )
        )
        hold_by_id = {item.id: item for item in hold_identities}
        for proposal in proposal_identities:
            if (
                proposal.workspace_id != run.workspace_id
                or proposal.run_id != run.id
                or proposal.hold_id not in hold_by_id
            ):
                _raise(
                    "collaboration_governance_lineage_invalid",
                    "a pending Proposal is outside the active Hold lineage",
                )

        # Lock every WorkerItem before any request, Hold, Proposal, or Application.
        for worker_item_id in sorted({hold.worker_item_id for hold in hold_identities}):
            worker_item = uow.worker_items.get(
                worker_item_id,
                run.workspace_id,
                for_update=True,
            )
            if worker_item is None or worker_item.root_task_id != run.root_task_id:
                _raise(
                    "collaboration_governance_lineage_invalid",
                    "a Hold WorkerItem is outside the Run root",
                )

        # Lock each request once, even when corrupted data points multiple rows at it.
        requests: dict[UUID, Any] = {}
        request_ids = tuple(
            proposal.execution_request_id for proposal in proposal_identities
        )
        if len(request_ids) != len(set(request_ids)):
            _raise(
                "collaboration_governance_lineage_invalid",
                "multiple pending Proposals point at one ExecutionRequest",
            )
        proposal_by_request = {
            proposal.execution_request_id: proposal for proposal in proposal_identities
        }
        for request_id in sorted(proposal_by_request):
            request = uow.execution_requests.get(
                request_id,
                run.workspace_id,
                for_update=True,
            )
            proposal = proposal_by_request[request_id]
            hold = hold_by_id[proposal.hold_id]
            if (
                request is None
                or request.workspace_id != run.workspace_id
                or request.root_task_id != run.root_task_id
                or request.worker_item_id != hold.worker_item_id
            ):
                _raise(
                    "collaboration_governance_lineage_invalid",
                    "a pending Proposal request is outside the Hold lineage",
                )
            requests[request_id] = request

        # Re-read mutable governance rows after their parent lineage is locked.
        holds: dict[UUID, CollaborationGovernanceHold] = {}
        for identity in hold_identities:
            hold = uow.collaboration_governance.get_hold(
                run.workspace_id,
                identity.id,
                for_update=True,
            )
            if hold is None:
                _raise(
                    "collaboration_governance_lineage_invalid",
                    "a discovered Hold disappeared",
                )
            if hold.status is not GovernanceHoldStatus.ACTIVE:
                continue
            if not _hold_identity_matches(hold, identity, run):
                _raise(
                    "collaboration_governance_lineage_invalid",
                    "a Hold identity changed while acquiring its lock",
                )
            holds[hold.id] = hold

        proposals: dict[UUID, CollaborationReleaseProposal] = {}
        for identity in proposal_identities:
            if identity.hold_id not in holds:
                continue
            proposal = uow.collaboration_governance.get_proposal(
                run.workspace_id,
                identity.id,
                for_update=True,
            )
            if proposal is None:
                _raise(
                    "collaboration_governance_lineage_invalid",
                    "a discovered Proposal disappeared",
                )
            if proposal.status is not ReleaseProposalStatus.PENDING:
                continue
            if not _proposal_identity_matches(proposal, identity, run):
                _raise(
                    "collaboration_governance_lineage_invalid",
                    "a Proposal identity changed while acquiring its lock",
                )
            if proposal.execution_request_id not in requests:
                _raise(
                    "collaboration_governance_lineage_invalid",
                    "a Proposal request was not locked",
                )
            proposals[proposal.id] = proposal

        # Applications are the last mutable rows in the fixed lock order.
        applications: dict[UUID, RuntimeIntentApplication] = {}
        for proposal in sorted(proposals.values(), key=lambda item: item.id):
            application = uow.collaboration_ledger.get_application(
                run.workspace_id,
                _application_id(proposal.intent_id),
                for_update=True,
            )
            if (
                application is None
                or application.workspace_id != run.workspace_id
                or application.intent_id != proposal.intent_id
                or application.status is not RuntimeIntentApplicationStatus.DEFERRED
            ):
                _raise(
                    "collaboration_governance_lineage_invalid",
                    "a pending Proposal does not have a deferred Application",
                )
            applications[proposal.id] = application

        staled_proposals = 0
        staled_applications = 0
        for proposal in sorted(proposals.values(), key=lambda item: item.id):
            application = applications[proposal.id]
            stale_proposal = proposal.stale(now=now)
            stale_application = application.resolve(
                status=RuntimeIntentApplicationStatus.STALE,
                result_code="plan_generation_superseded",
                result_summary="the plan generation was superseded",
                release_id=None,
                now=now,
            )
            uow.collaboration_governance.save_proposal(
                stale_proposal,
                proposal.resource_version,
            )
            uow.collaboration_ledger.save_application(
                stale_application,
                application.resource_version,
            )
            self._emit_event(
                uow,
                workspace_id=run.workspace_id,
                aggregate_type="collaboration_release_proposal",
                aggregate_id=stale_proposal.id,
                aggregate_version=stale_proposal.resource_version,
                event_type="CollaborationReleaseProposalStale",
                actor_id=actor_id,
                correlation_id=correlation_id,
                payload={
                    "proposal_id": str(stale_proposal.id),
                    "hold_id": str(stale_proposal.hold_id),
                    "run_id": str(run.id),
                    "root_task_id": str(run.root_task_id),
                    "status": stale_proposal.status.value,
                    "reason_code": "plan_generation_superseded",
                },
            )
            uow.projection_changes.changed(
                workspace_id=run.workspace_id,
                projection_type="collaboration_governance",
                resource_id=run.root_task_id,
                change_type="collaboration_release_proposal_stale",
                resource_version=stale_proposal.resource_version,
                correlation_id=correlation_id,
            )
            staled_proposals += 1
            staled_applications += 1

        superseded_holds = 0
        for hold in sorted(holds.values(), key=lambda item: item.id):
            superseded = hold.supersede(now=now)
            uow.collaboration_governance.save_hold(superseded, hold.resource_version)
            self._emit_event(
                uow,
                workspace_id=run.workspace_id,
                aggregate_type="collaboration_governance_hold",
                aggregate_id=superseded.id,
                aggregate_version=superseded.resource_version,
                event_type="CollaborationGovernanceHoldSuperseded",
                actor_id=actor_id,
                correlation_id=correlation_id,
                payload={
                    "hold_id": str(superseded.id),
                    "run_id": str(run.id),
                    "root_task_id": str(run.root_task_id),
                    "worker_item_id": str(superseded.worker_item_id),
                    "status": superseded.status.value,
                    "reason_code": "plan_generation_superseded",
                },
            )
            uow.projection_changes.changed(
                workspace_id=run.workspace_id,
                projection_type="collaboration_governance",
                resource_id=run.root_task_id,
                change_type="collaboration_governance_hold_superseded",
                resource_version=superseded.resource_version,
                correlation_id=correlation_id,
            )
            superseded_holds += 1

        return GovernanceSupersedeResult(
            superseded_holds=superseded_holds,
            staled_proposals=staled_proposals,
            staled_applications=staled_applications,
        )

    def recover(
        self,
        uow: ControlPlaneUnitOfWork | None,
        *,
        now: datetime,
        actor_id: str,
        correlation_id: UUID,
        limit: int,
        workspace_id: UUID | None = None,
    ) -> GovernanceRecoveryResult:
        _validate_context(now, actor_id, correlation_id)
        if limit < 1:
            raise ValueError("limit must be positive")
        if self._uow_factory is None:
            if uow is None:
                raise ValueError("uow is required when no uow_factory is configured")
            candidates = uow.collaboration_governance.list_supersede_candidates(
                now=now,
                limit=limit,
                workspace_id=workspace_id,
            )
        else:
            with self._uow_factory() as discovery_uow:
                candidates = (
                    discovery_uow.collaboration_governance.list_supersede_candidates(
                        now=now,
                        limit=limit,
                        workspace_id=workspace_id,
                    )
                )
        repaired = 0
        failed = 0
        failure_codes: set[str] = set()
        for candidate in candidates:
            try:
                if self._uow_factory is None:
                    assert uow is not None
                    # A savepoint keeps one corrupt lineage from poisoning the batch.
                    with uow.savepoint():
                        outcome = self._reconcile_candidate(
                            uow,
                            candidate,
                            now=now,
                            actor_id=actor_id,
                            correlation_id=correlation_id,
                        )
                else:
                    # Candidate discovery locks Hold rows. Release those locks before
                    # acquiring the standard Head -> Run -> ... order for repair.
                    with self._uow_factory() as candidate_uow:
                        outcome = self._reconcile_candidate(
                            candidate_uow,
                            candidate,
                            now=now,
                            actor_id=actor_id,
                            correlation_id=correlation_id,
                        )
                if outcome.superseded_holds or outcome.staled_proposals:
                    repaired += 1
            except DomainError as exc:
                failed += 1
                failure_codes.add(getattr(exc, "code", "domain_error"))
            except Exception:  # noqa: BLE001
                failed += 1
                failure_codes.add("collaboration_governance_reconciliation_failed")
        return GovernanceRecoveryResult(
            candidates=len(candidates),
            repaired=repaired,
            failed=failed,
            failure_codes=tuple(sorted(failure_codes)),
        )

    def _reconcile_candidate(
        self,
        uow: ControlPlaneUnitOfWork,
        candidate: CollaborationGovernanceHold,
        *,
        now: datetime,
        actor_id: str,
        correlation_id: UUID,
    ) -> GovernanceSupersedeResult:
        run = uow.collaboration_runs.get(
            candidate.workspace_id,
            candidate.run_id,
        )
        if run is None:
            _raise(
                "collaboration_governance_run_missing",
                "the candidate Run was not found",
            )
        return self.supersede_run(
            uow,
            run,
            now=now,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )

    @staticmethod
    def _emit_event(
        uow: ControlPlaneUnitOfWork,
        *,
        workspace_id: UUID,
        aggregate_type: str,
        aggregate_id: UUID,
        aggregate_version: int,
        event_type: str,
        actor_id: str,
        correlation_id: UUID,
        payload: dict[str, object],
    ) -> None:
        command_id = uuid5(
            NAMESPACE_URL,
            "sidera:collaboration-governance-reconcile:"
            f"{actor_id}:{correlation_id}:{aggregate_type}:{aggregate_id}:"
            f"{aggregate_version}:{event_type}",
        )
        command = CommandEnvelope(
            id=command_id,
            workspace_id=workspace_id,
            command_type="collaboration.governance.reconcile",
            target_type=aggregate_type,
            actor_type="system",
            actor_id=actor_id,
            target_id=aggregate_id,
            payload=payload,
            expected_version=aggregate_version - 1,
            idempotency_key=(
                "collaboration-governance-reconcile:"
                f"{aggregate_id}:{aggregate_version}:{event_type}"
            ),
            correlation_id=correlation_id,
        )
        event = EventData(
            id=uuid4(),
            workspace_id=workspace_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
            event_type=event_type,
            payload=payload,
            actor_type="system",
            actor_id=actor_id,
            causation_id=command.id,
            correlation_id=correlation_id,
        )
        uow.commands.record_applied(
            command,
            hash_request(command),
            CommandResult(
                command_id=command.id,
                aggregate_id=aggregate_id,
                aggregate_version=aggregate_version,
                event_ids=(event.id,),
            ),
        )
        uow.events.append_with_outbox(event)


def _application_id(intent_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"sidera:collaboration-application:{intent_id}")


def _run_identity_matches(current: Any, expected: Any) -> bool:
    return all(
        getattr(current, field, None) == getattr(expected, field, None)
        for field in (
            "id",
            "workspace_id",
            "root_task_id",
            "plan_snapshot_id",
            "plan_generation",
            "plan_content_hash",
        )
    )


def _hold_identity_matches(
    current: CollaborationGovernanceHold,
    expected: CollaborationGovernanceHold,
    run: Any,
) -> bool:
    return (
        current.workspace_id == run.workspace_id
        and current.root_task_id == run.root_task_id
        and current.run_id == run.id
        and current.plan_snapshot_id == expected.plan_snapshot_id
        and current.plan_generation == expected.plan_generation
        and current.plan_content_hash == expected.plan_content_hash
        and current.worker_item_id == expected.worker_item_id
    )


def _proposal_identity_matches(
    current: CollaborationReleaseProposal,
    expected: CollaborationReleaseProposal,
    run: Any,
) -> bool:
    return (
        current.workspace_id == run.workspace_id
        and current.run_id == run.id
        and current.hold_id == expected.hold_id
        and current.projection_snapshot_id == expected.projection_snapshot_id
        and current.projection_hash == expected.projection_hash
        and current.decision_id == expected.decision_id
        and current.intent_id == expected.intent_id
        and current.execution_request_id == expected.execution_request_id
        and current.target_request_version == expected.target_request_version
        and current.input_fingerprint_version == expected.input_fingerprint_version
        and current.input_fingerprint == expected.input_fingerprint
    )


def _validate_context(now: datetime, actor_id: str, correlation_id: UUID) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not actor_id.strip():
        raise ValueError("actor_id is required")
    if not isinstance(correlation_id, UUID):
        raise TypeError("correlation_id must be a UUID")


def _raise(code: str, message: str) -> NoReturn:
    raise CollaborationGovernanceReconciliationError(code, message)
