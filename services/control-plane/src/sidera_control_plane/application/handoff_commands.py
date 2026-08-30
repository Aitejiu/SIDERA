from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.handoff_candidates import (
    HandoffCandidateBlocked,
    HandoffCandidateService,
)
from sidera_control_plane.application.handoff_proposal_reconciler import (
    HandoffBaseInspectionError,
    collect_handoff_base,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_runtime import (
    WorkerRuntimeInspectionPort,
    WorkerRuntimeResolver,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import DomainError, VersionConflict
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.handoffs import (
    HandoffCandidateInspection,
    HandoffProposal,
    HandoffProposalPayload,
    HandoffProposalStatus,
    HandoffSourceChannel,
    HandoffTransferMode,
)
from sidera_control_plane.domain.worker_item import WorkerItemStatus

_PREPARE_KEYS = frozenset(
    {
        "target_worker_id",
        "handoff_reason",
        "handoff_instruction",
        "input_plan",
        "proposal_ttl_seconds",
    }
)
_CANCEL_KEYS = frozenset({"reason"})


class HandoffCommandError(DomainError):
    """Stable domain rejection for the Web handoff command surface."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.code = code


class HandoffCommandHandler:
    command_types = frozenset({"PrepareWorkerHandoff", "CancelWorkerHandoff"})

    def __init__(
        self,
        clock: Callable[[], datetime],
        *,
        candidate_service: HandoffCandidateService,
        runtime_resolver: WorkerRuntimeInspectionPort | None = None,
        default_proposal_ttl_seconds: int = 900,
        max_proposal_ttl_seconds: int = 86_400,
    ) -> None:
        self._clock = clock
        self._candidate_service = candidate_service
        self._runtime_resolver = runtime_resolver or WorkerRuntimeResolver()
        self._default_proposal_ttl_seconds = default_proposal_ttl_seconds
        self._max_proposal_ttl_seconds = max_proposal_ttl_seconds

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.command_type == "PrepareWorkerHandoff":
            return self._prepare(uow, envelope)
        if envelope.command_type == "CancelWorkerHandoff":
            return self._cancel(uow, envelope)
        raise HandoffCommandError(
            "validation_error",
            f"unsupported handoff command: {envelope.command_type}",
        )

    def _prepare(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.target_type != "worker_item":
            raise HandoffCommandError(
                "validation_error", "PrepareWorkerHandoff targets a worker_item"
            )
        worker_item_id = _required_target(envelope)
        payload = _prepare_payload(
            envelope.payload,
            self._max_proposal_ttl_seconds,
            self._default_proposal_ttl_seconds,
        )
        now = self._clock()

        worker_item = uow.worker_items.get(
            worker_item_id,
            envelope.workspace_id,
            for_update=True,
        )
        if worker_item is None:
            raise HandoffCommandError("not_found", "worker item not found")
        if envelope.expected_version is None:
            raise HandoffCommandError(
                "validation_error", "worker item version is required"
            )
        if envelope.expected_version < 1:
            raise HandoffCommandError(
                "validation_error", "expected_version must be positive"
            )
        if worker_item.version != envelope.expected_version:
            raise VersionConflict(envelope.expected_version, worker_item.version)
        if worker_item.status is not WorkerItemStatus.ACTIVE:
            raise HandoffCommandError(
                "handoff_source_not_active", "worker item is not active"
            )

        try:
            candidates = self._candidate_service.inspect_in_uow(
                uow,
                workspace_id=envelope.workspace_id,
                worker_item_id=worker_item.id,
                now=now,
            )
        except HandoffCandidateBlocked as exc:
            raise HandoffCommandError(
                exc.reason_code,
                "handoff candidate inspection is blocked",
                details=exc.details,
            ) from exc
        except NotFound as exc:
            raise HandoffCommandError("not_found", str(exc), details=exc.details) from exc

        if candidates.blocking_codes:
            code = candidates.blocking_codes[0]
            raise HandoffCommandError(
                code,
                "handoff preparation is blocked",
                details={"blocking_codes": list(candidates.blocking_codes)},
            )

        target_worker_id = payload["target_worker_id"]
        target_candidate = _target_candidate(candidates, target_worker_id)
        if target_candidate is None or not target_candidate.eligible:
            raise HandoffCommandError(
                "handoff_target_ineligible",
                "requested handoff target is not eligible",
                details={
                    "target_worker_id": str(target_worker_id),
                    "ineligibility_code": (
                        target_candidate.ineligibility_code
                        if target_candidate is not None
                        else "target_not_listed"
                    ),
                },
            )

        target_worker = uow.workers.get(
            target_worker_id,
            envelope.workspace_id,
            for_update=True,
        )
        if target_worker is None:
            raise HandoffCommandError(
                "handoff_target_ineligible", "handoff target worker not found"
            )

        try:
            current = collect_handoff_base(
                uow,
                candidate=candidates,
                worker_item=worker_item,
                target_worker_id=target_worker.id,
                runtime_resolver=self._runtime_resolver,
                lock_runtime=True,
            )
        except HandoffBaseInspectionError as exc:
            raise HandoffCommandError(
                _prepare_error_code(exc.reason),
                "handoff preparation facts are no longer eligible",
                details={"reason": exc.reason},
            ) from exc

        # The first inspection identifies the source and target. After the
        # source item, workers, and runtime facts are locked, inspect again so
        # the payload is built from the same authoritative view as the locks.
        try:
            candidates = self._candidate_service.inspect_in_uow(
                uow,
                workspace_id=envelope.workspace_id,
                worker_item_id=worker_item.id,
                now=now,
            )
        except HandoffCandidateBlocked as exc:
            raise HandoffCommandError(
                exc.reason_code,
                "handoff candidate inspection is blocked",
                details=exc.details,
            ) from exc
        if candidates.blocking_codes:
            raise HandoffCommandError(
                candidates.blocking_codes[0],
                "handoff preparation is blocked",
                details={"blocking_codes": list(candidates.blocking_codes)},
            )
        target_candidate = _target_candidate(candidates, target_worker_id)
        if target_candidate is None or not target_candidate.eligible:
            raise HandoffCommandError(
                "handoff_target_ineligible",
                "requested handoff target is not eligible",
                details={"target_worker_id": str(target_worker_id)},
            )
        try:
            current = collect_handoff_base(
                uow,
                candidate=candidates,
                worker_item=worker_item,
                target_worker_id=target_worker.id,
                runtime_resolver=self._runtime_resolver,
                lock_runtime=True,
            )
        except HandoffBaseInspectionError as exc:
            raise HandoffCommandError(
                _prepare_error_code(exc.reason),
                "handoff preparation facts are no longer eligible",
                details={"reason": exc.reason},
            ) from exc

        proposal_payload = HandoffProposalPayload(
            proposal_schema_version=1,
            workspace_id=envelope.workspace_id,
            root_task_id=worker_item.root_task_id,
            subtask_id=current.task_id,
            worker_item_id=worker_item.id,
            source_execution_id=current.execution_id,
            source_execution_version=current.execution_version,
            source_session_id=current.session_id,
            source_session_version=current.session_version,
            source_session_last_sequence=current.session_last_sequence,
            source_worker_id=current.source_worker_id,
            source_worker_version=current.source_worker_version,
            source_runtime_snapshot=current.source_runtime_snapshot,
            source_workspace_manifest_id=current.workspace_manifest_id,
            source_workspace_manifest_version=current.workspace_manifest_version,
            source_context_package_id=current.context_package_id,
            source_context_package_hash=current.context_package_hash,
            source_worker_context_version=current.context_package_version,
            observed_plan_snapshot_id=current.plan_snapshot_id,
            observed_plan_generation=current.plan_generation,
            observed_plan_head_version=current.plan_head_version,
            target_worker_id=target_worker.id,
            target_worker_version=current.target_worker_version,
            target_runtime_snapshot=current.target_runtime_snapshot,
            target_capability_snapshot_hash=(
                current.target_capability_snapshot_hash
                or target_candidate.capability_snapshot_hash
                or "0" * 64
            ),
            target_policy_reason=target_candidate.policy_reason,
            target_policy_facts={
                "eligible": target_candidate.eligible,
                "recommended": target_candidate.recommended,
                "workspace_compatibility": target_candidate.workspace_compatibility,
            },
            transfer_mode=HandoffTransferMode.REUSE_EXACT,
            handoff_reason=payload["handoff_reason"],
            handoff_instruction=payload["handoff_instruction"],
            input_plan=payload["input_plan"],
            impact_summary={
                "worker_item_version": current.worker_item_version,
                "input_state_hash": current.input_state_hash,
                "workspace_lifecycle": current.workspace_lifecycle,
                "candidate_inspected_at": candidates.inspected_at.isoformat(),
            },
        )
        proposal = HandoffProposal.create(
            id=uuid4(),
            payload=proposal_payload,
            created_by_type=envelope.actor_type,
            created_by_id=envelope.actor_id,
            source_channel=HandoffSourceChannel.WEB,
            created_at=now,
            expires_at=now + timedelta(seconds=payload["proposal_ttl_seconds"]),
        )

        event_ids: list[UUID] = []
        prior = uow.handoff_proposals.find_prepared_for_worker_item(
            worker_item.id,
            envelope.workspace_id,
            for_update=True,
        )
        if prior is not None:
            previous_version = prior.version
            prior.mark_stale(reason="superseded_by_new_proposal", now=now)
            uow.handoff_proposals.save_status(prior, previous_version)
            stale_event = _proposal_event(
                prior,
                event_type="HandoffProposalStale",
                envelope=envelope,
                reason="superseded_by_new_proposal",
            )
            uow.events.append_with_outbox(stale_event)
            event_ids.append(stale_event.id)

        uow.handoff_proposals.add(proposal)
        prepared_event = _proposal_event(
            proposal,
            event_type="HandoffProposalPrepared",
            envelope=envelope,
        )
        uow.events.append_with_outbox(prepared_event)
        event_ids.append(prepared_event.id)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=proposal.id,
            aggregate_version=proposal.version,
            event_ids=tuple(event_ids),
            data={"proposal": proposal.to_mapping()},
        )

    def _cancel(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.target_type != "handoff_proposal":
            raise HandoffCommandError(
                "validation_error", "CancelWorkerHandoff targets a handoff_proposal"
            )
        proposal_id = _required_target(envelope)
        if envelope.expected_version is None or envelope.expected_version < 1:
            raise HandoffCommandError(
                "validation_error", "proposal expected_version must be positive"
            )
        _require_exact_keys(envelope.payload, _CANCEL_KEYS)
        reason = _required_text(envelope.payload, "reason")
        proposal = uow.handoff_proposals.get(
            proposal_id,
            envelope.workspace_id,
            for_update=True,
        )
        if proposal is None:
            raise HandoffCommandError("not_found", "handoff proposal not found")
        if proposal.version != envelope.expected_version:
            raise VersionConflict(envelope.expected_version, proposal.version)
        if proposal.status is not HandoffProposalStatus.PREPARED:
            raise HandoffCommandError(
                "handoff_proposal_not_prepared",
                "only a prepared handoff proposal can be cancelled",
                details={"status": proposal.status.value},
            )
        previous_version = proposal.version
        proposal.mark_cancelled(now=self._clock())
        uow.handoff_proposals.save_status(proposal, previous_version)
        event = _proposal_event(
            proposal,
            event_type="HandoffProposalCancelled",
            envelope=envelope,
            reason=reason,
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=proposal.id,
            aggregate_version=proposal.version,
            event_ids=(event.id,),
            data={"proposal": proposal.to_mapping()},
        )


def _prepare_payload(
    payload: Mapping[str, object],
    max_ttl_seconds: int,
    default_ttl_seconds: int,
) -> dict[str, Any]:
    _require_exact_keys(payload, _PREPARE_KEYS)
    target_worker_id = _parse_uuid(payload.get("target_worker_id"), "target_worker_id")
    reason = _required_text(payload, "handoff_reason")
    instruction = _required_text(payload, "handoff_instruction")
    input_plan = payload.get("input_plan")
    if not isinstance(input_plan, Mapping):
        raise HandoffCommandError("validation_error", "input_plan must be a mapping")
    ttl = payload.get("proposal_ttl_seconds")
    if ttl is None:
        ttl = default_ttl_seconds
    if not isinstance(ttl, int) or isinstance(ttl, bool):
        raise HandoffCommandError(
            "validation_error", "proposal_ttl_seconds must be an integer"
        )
    if ttl < 1 or ttl > max_ttl_seconds:
        raise HandoffCommandError(
            "validation_error", "proposal_ttl_seconds is outside the allowed range"
        )
    return {
        "target_worker_id": target_worker_id,
        "handoff_reason": reason,
        "handoff_instruction": instruction,
        "input_plan": dict(input_plan),
        "proposal_ttl_seconds": ttl,
    }


def _required_target(envelope: CommandEnvelope) -> UUID:
    if envelope.target_id is None:
        raise HandoffCommandError("validation_error", "target_id is required")
    return envelope.target_id


def _require_exact_keys(payload: Mapping[str, object], expected: frozenset[str]) -> None:
    actual = frozenset(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise HandoffCommandError(
            "validation_error",
            "handoff command payload keys are not exact",
            details={"missing": missing, "extra": extra},
        )


def _required_text(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise HandoffCommandError("validation_error", f"{name} is required")
    return value.strip()


def _parse_uuid(value: object, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise HandoffCommandError("validation_error", f"{name} must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise HandoffCommandError("validation_error", f"{name} must be a UUID") from exc


def _target_candidate(
    candidate_list: Any, target_worker_id: UUID
) -> HandoffCandidateInspection | None:
    return next(
        (candidate for candidate in candidate_list.candidates if candidate.worker_id == target_worker_id),
        None,
    )


def _prepare_error_code(reason: str) -> str:
    return {
        "target_ineligible": "handoff_target_ineligible",
        "source_workspace_changed": "handoff_workspace_incompatible",
        "source_context_package_changed": "handoff_context_package_changed",
        "plan_head_changed": "handoff_plan_changed",
        "plan_transition_active": "handoff_plan_transition_active",
        "handoff_already_active": "handoff_already_active",
        "operation_state_changed": "handoff_operation_unknown",
        "source_runtime_changed": "handoff_runtime_incompatible",
        "target_runtime_changed": "handoff_runtime_incompatible",
    }.get(reason, reason)


def _proposal_event(
    proposal: HandoffProposal,
    *,
    event_type: str,
    envelope: CommandEnvelope,
    reason: str | None = None,
) -> EventData:
    payload = proposal.payload
    event_payload: dict[str, object] = {
        "proposal_id": str(proposal.id),
        "workspace_id": str(payload.workspace_id),
        "root_task_id": str(payload.root_task_id),
        "subtask_id": str(payload.subtask_id),
        "worker_item_id": str(payload.worker_item_id),
        "source_execution_id": str(payload.source_execution_id),
        "source_session_id": str(payload.source_session_id),
        "source_worker_id": str(payload.source_worker_id),
        "target_worker_id": str(payload.target_worker_id),
        "proposal_hash": proposal.proposal_hash,
        "status": proposal.status.value,
    }
    if reason is not None:
        event_payload["reason"] = reason
    event_payload["handoff_reason_hash"] = _sha256_text(payload.handoff_reason)
    return EventData(
        id=uuid4(),
        workspace_id=payload.workspace_id,
        aggregate_type="handoff_proposal",
        aggregate_id=proposal.id,
        aggregate_version=proposal.version,
        event_type=event_type,
        payload=event_payload,
        actor_type=envelope.actor_type,
        actor_id=envelope.actor_id,
        causation_id=envelope.id,
        correlation_id=envelope.correlation_id,
    )


def _sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()
