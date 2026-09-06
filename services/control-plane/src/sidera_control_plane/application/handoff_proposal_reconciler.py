from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.context_requests import (
    ExecutionContextAuthorizationError,
    resolve_execution_context_authorization,
)
from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.handoff_candidates import (
    HandoffCandidateBlocked,
    HandoffCandidateService,
    runtime_snapshot_for_handoff,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_runtime import (
    ResolvedWorkerRuntime,
    WorkerRuntimeInspectionPort,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import ContextPurpose
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import (
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.gateway import GatewayOperationStatus
from sidera_control_plane.domain.handoffs import (
    HandoffCandidateList,
    HandoffProposal,
    HandoffProposalStatus,
)
from sidera_control_plane.domain.plan_transitions import PlanTransitionStatus
from sidera_control_plane.domain.worker_item import WorkerItem, WorkerItemStatus


class HandoffBaseInspectionError(Exception):
    """The current control-plane facts cannot support a prepared proposal."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class HandoffBaseSnapshot:
    worker_item_version: int
    worker_item_context_version: int
    execution_id: UUID
    task_id: UUID
    execution_version: int
    execution_status: str
    session_id: UUID
    session_version: int
    session_last_sequence: int
    session_status: str
    source_worker_id: UUID
    source_worker_version: int
    source_runtime_snapshot: Mapping[str, object]
    workspace_manifest_id: UUID
    workspace_manifest_version: int
    workspace_lifecycle: str
    context_package_id: UUID
    context_package_hash: str
    context_package_version: int
    context_package_status: str
    plan_snapshot_id: UUID
    plan_generation: int
    plan_head_version: int
    target_worker_id: UUID
    target_worker_version: int
    target_runtime_snapshot: Mapping[str, object]
    target_capability_snapshot_hash: str | None
    target_eligible: bool
    input_state_hash: str
    blocking_codes: tuple[str, ...]


def collect_handoff_base(
    uow: ControlPlaneUnitOfWork,
    *,
    candidate: HandoffCandidateList,
    worker_item: WorkerItem,
    target_worker_id: UUID,
    runtime_resolver: WorkerRuntimeInspectionPort,
    lock_runtime: bool,
) -> HandoffBaseSnapshot:
    """Load the exact control-plane facts a proposal is based on.

    This helper deliberately stops at control-plane repositories. It never calls
    an adapter or a workspace provider and is shared by Prepare and reconciliation.
    """

    if _enum_value(worker_item.status) != WorkerItemStatus.ACTIVE.value:
        raise HandoffBaseInspectionError("source_execution_changed")
    if candidate.blocking_codes:
        raise HandoffBaseInspectionError(
            _reason_for_candidate_code(candidate.blocking_codes[0])
        )

    execution = uow.executions.get(
        candidate.source_execution_id,
        candidate.workspace_id,
        for_update=lock_runtime,
    )
    if execution is None or execution.worker_item_id != worker_item.id:
        raise HandoffBaseInspectionError("source_execution_changed")
    if _enum_value(execution.status) != ExecutionStatus.RUNNING.value:
        raise HandoffBaseInspectionError("source_execution_changed")

    session = uow.sessions.get(
        candidate.source_session_id,
        candidate.workspace_id,
        for_update=lock_runtime,
    )
    if (
        session is None
        or session.execution_id != execution.id
        or _enum_value(session.status) != ExecutionSessionStatus.ACTIVE.value
    ):
        raise HandoffBaseInspectionError("source_session_changed")

    source_worker = uow.workers.get(
        candidate.source_worker_id,
        candidate.workspace_id,
        for_update=lock_runtime,
    )
    if source_worker is None or source_worker.id != session.worker_id:
        raise HandoffBaseInspectionError("source_worker_changed")

    target_worker = uow.workers.get(
        target_worker_id,
        candidate.workspace_id,
        for_update=lock_runtime,
    )
    if target_worker is None:
        raise HandoffBaseInspectionError("target_worker_changed")

    manifest = uow.workspaces.get_for_execution(
        execution.id,
        candidate.workspace_id,
        for_update=lock_runtime,
    )
    if manifest is None:
        raise HandoffBaseInspectionError("source_workspace_changed")

    try:
        authorization = resolve_execution_context_authorization(uow, execution)
    except ExecutionContextAuthorizationError as exc:
        raise HandoffBaseInspectionError("source_context_package_changed") from exc
    package = uow.context_packages.get_for_consumer(
        ContextPurpose.EXECUTION,
        execution.id,
        candidate.workspace_id,
        manifest_schema_version=(
            2
            if authorization is not None
            and authorization.request.input_selection_id is not None
            else 1
        ),
        for_update=lock_runtime,
    )
    if package is None:
        raise HandoffBaseInspectionError("source_context_package_changed")

    plan_head = uow.plans.get_head(
        worker_item.root_task_id,
        candidate.workspace_id,
        for_update=lock_runtime,
    )
    if plan_head is None:
        raise HandoffBaseInspectionError("plan_head_changed")

    plan_transitions = uow.plan_transitions.list_for_root(
        worker_item.root_task_id,
        candidate.workspace_id,
    )
    if any(
        _enum_value(item.status) == PlanTransitionStatus.CONVERGING.value
        for item in plan_transitions
    ):
        raise HandoffBaseInspectionError("plan_transition_active")

    active_handoff = uow.handoff_transitions.get_active_for_worker_item(
        worker_item.id,
        candidate.workspace_id,
    )
    if active_handoff is not None:
        raise HandoffBaseInspectionError("handoff_already_active")

    pending_input = uow.execution_inputs.get_pending_for_session(
        session.id,
        candidate.workspace_id,
    )
    gateway_operations = uow.gateway_operations.list_for_execution(
        execution.id,
        candidate.workspace_id,
    )
    if any(
        _enum_value(operation.status)
        in {
            GatewayOperationStatus.REQUESTED.value,
            GatewayOperationStatus.RUNNING.value,
            GatewayOperationStatus.UNKNOWN.value,
        }
        for operation in gateway_operations
    ):
        raise HandoffBaseInspectionError("operation_state_changed")

    target_candidate = next(
        (item for item in candidate.candidates if item.worker_id == target_worker_id),
        None,
    )
    if target_candidate is None or not target_candidate.eligible:
        raise HandoffBaseInspectionError("target_ineligible")

    runtimes = _resolve_runtimes(
        uow,
        (source_worker, target_worker),
        execution,
        runtime_resolver,
        lock_runtime=lock_runtime,
    )
    source_runtime = runtimes.get(source_worker.id)
    target_runtime = runtimes.get(target_worker.id)
    if source_runtime is None:
        raise HandoffBaseInspectionError("source_runtime_changed")
    if target_runtime is None:
        raise HandoffBaseInspectionError("target_runtime_changed")

    source_snapshot = runtime_snapshot_for_handoff(source_runtime)
    target_snapshot = runtime_snapshot_for_handoff(target_runtime)
    target_capability_hash = target_candidate.capability_snapshot_hash
    if target_capability_hash is None:
        value = target_snapshot.get("capability_snapshot_hash")
        target_capability_hash = value if isinstance(value, str) else None

    return HandoffBaseSnapshot(
        worker_item_version=worker_item.version,
        worker_item_context_version=worker_item.context_version,
        execution_id=execution.id,
        task_id=execution.task_id,
        execution_version=execution.version,
        execution_status=_enum_value(execution.status),
        session_id=session.id,
        session_version=session.version,
        session_last_sequence=session.last_sequence,
        session_status=_enum_value(session.status),
        source_worker_id=source_worker.id,
        source_worker_version=source_worker.version,
        source_runtime_snapshot=source_snapshot,
        workspace_manifest_id=_uuid_attr(manifest, "id", "source_workspace_changed"),
        workspace_manifest_version=_positive_attr(
            manifest, "version", "source_workspace_changed"
        ),
        workspace_lifecycle=_enum_value(getattr(manifest, "lifecycle", None)),
        context_package_id=_uuid_attr(package, "package_id", "source_context_package_changed"),
        context_package_hash=_hash_attr(
            package, "package_hash", "source_context_package_changed"
        ),
        context_package_version=_positive_attr(
            package, "context_version", "source_context_package_changed"
        ),
        context_package_status=_enum_value(getattr(package, "status", None)),
        plan_snapshot_id=_uuid_attr(
            plan_head, "active_snapshot_id", "plan_head_changed"
        ),
        plan_generation=_positive_attr(
            plan_head, "active_generation", "plan_head_changed"
        ),
        plan_head_version=_positive_attr(plan_head, "version", "plan_head_changed"),
        target_worker_id=target_worker.id,
        target_worker_version=target_worker.version,
        target_runtime_snapshot=target_snapshot,
        target_capability_snapshot_hash=target_capability_hash,
        target_eligible=target_candidate.eligible,
        input_state_hash=input_state_hash(pending_input),
        blocking_codes=tuple(candidate.blocking_codes),
    )


def input_state_hash(value: object | None) -> str:
    """Hash only input identity/state, never input text or attachments."""

    if value is None:
        return "none"
    document = {
        "id": str(getattr(value, "id", "")),
        "version": getattr(value, "version", None),
        "status": _enum_value(getattr(value, "status", None)),
    }
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class HandoffProposalReconciler:
    """CAS a prepared proposal to stale/expired from control-plane facts."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        candidate_service: HandoffCandidateService,
        runtime_resolver: WorkerRuntimeInspectionPort,
        actor_type: str = "system",
        actor_id: str = "handoff-proposal-reconciler",
    ) -> None:
        self._uow_factory = uow_factory
        self._candidate_service = candidate_service
        self._runtime_resolver = runtime_resolver
        self._actor_type = actor_type
        self._actor_id = actor_id

    def reconcile(
        self,
        proposal_id: UUID,
        workspace_id: UUID,
        now: datetime,
    ) -> HandoffProposal:
        with self._uow_factory() as uow:
            return self.reconcile_in_uow(
                uow,
                proposal_id=proposal_id,
                workspace_id=workspace_id,
                now=now,
            )

    def reconcile_in_uow(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        proposal_id: UUID,
        workspace_id: UUID,
        now: datetime,
    ) -> HandoffProposal:
        proposal = uow.handoff_proposals.get(
            proposal_id,
            workspace_id,
            for_update=True,
        )
        if proposal is None:
            raise NotFound("handoff proposal not found")
        if proposal.status is not HandoffProposalStatus.PREPARED:
            return proposal

        if now >= proposal.expires_at:
            proposal.mark_expired(now=now)
            self._save_and_emit(
                uow,
                proposal,
                previous_version=proposal.version - 1,
                event_type="HandoffProposalExpired",
                reason="proposal_expired",
            )
            return proposal

        reason = self._stale_reason(uow, proposal, now)
        if reason is None:
            return proposal

        proposal.mark_stale(reason=reason, now=now)
        self._save_and_emit(
            uow,
            proposal,
            previous_version=proposal.version - 1,
            event_type="HandoffProposalStale",
            reason=reason,
        )
        return proposal

    def _stale_reason(
        self,
        uow: ControlPlaneUnitOfWork,
        proposal: HandoffProposal,
        now: datetime,
    ) -> str | None:
        payload = proposal.payload
        try:
            candidate = self._candidate_service.inspect_in_uow(
                uow,
                workspace_id=payload.workspace_id,
                worker_item_id=payload.worker_item_id,
                now=now,
            )
        except HandoffCandidateBlocked as exc:
            return _reason_for_candidate_code(exc.reason_code)

        worker_item = uow.worker_items.get(
            payload.worker_item_id,
            payload.workspace_id,
            for_update=True,
        )
        if worker_item is None:
            return "worker_item_changed"
        try:
            current = collect_handoff_base(
                uow,
                candidate=candidate,
                worker_item=worker_item,
                target_worker_id=payload.target_worker_id,
                runtime_resolver=self._runtime_resolver,
                lock_runtime=True,
            )
        except HandoffBaseInspectionError as exc:
            return exc.reason
        return _first_changed_reason(proposal, current)

    def _save_and_emit(
        self,
        uow: ControlPlaneUnitOfWork,
        proposal: HandoffProposal,
        *,
        previous_version: int,
        event_type: str,
        reason: str,
    ) -> None:
        uow.handoff_proposals.save_status(proposal, previous_version)
        command_id = uuid5(
            NAMESPACE_URL,
            f"sidera.handoff-proposal-reconcile:{self._actor_id}:"
            f"{proposal.id}:{event_type}:v{proposal.version}",
        )
        command = CommandEnvelope(
            id=command_id,
            workspace_id=proposal.payload.workspace_id,
            command_type="HandoffProposalReconcile",
            target_type="handoff_proposal",
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            target_id=proposal.id,
            payload={
                "proposal_id": str(proposal.id),
                "event_type": event_type,
                "reason": reason,
            },
            expected_version=previous_version,
            idempotency_key=(
                f"handoff-proposal-reconcile:{proposal.id}:{event_type}:"
                f"v{proposal.version}"
            ),
            correlation_id=proposal.id,
        )
        event = EventData(
            id=uuid4(),
            workspace_id=proposal.payload.workspace_id,
            aggregate_type="handoff_proposal",
            aggregate_id=proposal.id,
            aggregate_version=proposal.version,
            event_type=event_type,
            payload=_proposal_event_payload(proposal, reason=reason),
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            causation_id=command.id,
            correlation_id=command.correlation_id,
        )
        uow.commands.record_applied(
            command,
            hash_request(command),
            CommandResult(
                command_id=command.id,
                aggregate_id=proposal.id,
                aggregate_version=proposal.version,
                event_ids=(event.id,),
                data={"event_type": event_type, "reason": reason},
            ),
        )
        uow.events.append_with_outbox(event)


def _resolve_runtimes(
    uow: ControlPlaneUnitOfWork,
    workers: Sequence[Any],
    execution: Any,
    resolver: WorkerRuntimeInspectionPort,
    *,
    lock_runtime: bool,
) -> dict[UUID, ResolvedWorkerRuntime | None]:
    if lock_runtime:
        lock_and_resolve = getattr(resolver, "lock_and_resolve", None)
        if callable(lock_and_resolve):
            resolved: dict[UUID, ResolvedWorkerRuntime | None] = {}
            for worker in workers:
                result = lock_and_resolve(uow, worker, execution)
                resolved[worker.id] = (
                    result if isinstance(result, ResolvedWorkerRuntime) else None
                )
            return resolved

    inspections = resolver.inspect_candidates(uow, workers, execution)
    return {
        inspection.worker.id: inspection.resolved for inspection in inspections
    }


def _first_changed_reason(
    proposal: HandoffProposal,
    current: HandoffBaseSnapshot,
) -> str | None:
    payload = proposal.payload
    impact = payload.impact_summary
    expected_worker_item_version = impact.get("worker_item_version")
    if expected_worker_item_version != current.worker_item_version:
        return "worker_item_changed"
    if payload.source_execution_id != current.execution_id:
        return "source_execution_changed"
    if payload.source_execution_version != current.execution_version:
        return "source_execution_changed"
    if current.execution_status != "running":
        return "source_execution_changed"
    if payload.source_session_id != current.session_id:
        return "source_session_changed"
    if payload.source_session_version != current.session_version:
        return "source_session_changed"
    if payload.source_session_last_sequence != current.session_last_sequence:
        return "source_sequence_changed"
    if current.session_status != "active":
        return "source_session_changed"
    if payload.source_worker_id != current.source_worker_id:
        return "source_worker_changed"
    if payload.source_worker_version != current.source_worker_version:
        return "source_worker_changed"
    if _mapping_intersection_changed(
        payload.source_runtime_snapshot,
        current.source_runtime_snapshot,
    ):
        return "source_runtime_changed"
    if payload.source_workspace_manifest_id != current.workspace_manifest_id:
        return "source_workspace_changed"
    if payload.source_workspace_manifest_version != current.workspace_manifest_version:
        return "source_workspace_changed"
    if (
        "workspace_lifecycle" in impact
        and impact["workspace_lifecycle"] != current.workspace_lifecycle
    ):
        return "source_workspace_changed"
    if payload.source_context_package_id != current.context_package_id:
        return "source_context_package_changed"
    if payload.source_context_package_hash != current.context_package_hash:
        return "source_context_package_changed"
    if payload.source_worker_context_version != current.worker_item_context_version:
        return "worker_item_context_changed"
    if payload.source_worker_context_version != current.context_package_version:
        return "worker_item_context_changed"
    if current.context_package_status != "active":
        return "source_context_package_changed"
    if payload.observed_plan_snapshot_id != current.plan_snapshot_id:
        return "plan_head_changed"
    if payload.observed_plan_generation != current.plan_generation:
        return "plan_head_changed"
    if payload.observed_plan_head_version != current.plan_head_version:
        return "plan_head_changed"
    if payload.target_worker_id != current.target_worker_id:
        return "target_worker_changed"
    if payload.target_worker_version != current.target_worker_version:
        return "target_worker_changed"
    if _mapping_intersection_changed(
        payload.target_runtime_snapshot,
        current.target_runtime_snapshot,
    ):
        return "target_runtime_changed"
    if (
        payload.target_capability_snapshot_hash
        != current.target_capability_snapshot_hash
    ):
        return "target_runtime_changed"
    if not current.target_eligible:
        return "target_ineligible"
    if impact.get("input_state_hash") != current.input_state_hash:
        return "input_changed"
    return None


def _mapping_intersection_changed(
    expected: Mapping[str, object], current: Mapping[str, object]
) -> bool:
    return any(expected[key] != current[key] for key in set(expected).intersection(current))


def _proposal_event_payload(proposal: HandoffProposal, *, reason: str) -> dict[str, object]:
    payload = proposal.payload
    return {
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
        "reason": reason,
        "handoff_reason_hash": hashlib.sha256(payload.handoff_reason.encode()).hexdigest(),
    }


def _reason_for_candidate_code(code: str) -> str:
    return {
        "handoff_source_not_active": "source_execution_changed",
        "handoff_source_ambiguous": "source_session_changed",
        "handoff_already_active": "handoff_already_active",
        "handoff_plan_transition_active": "plan_transition_active",
        "handoff_input_in_flight": "input_changed",
        "handoff_operation_unknown": "operation_state_changed",
        "handoff_runtime_incompatible": "source_runtime_changed",
        "handoff_checkpoint_unsupported": "source_runtime_changed",
        "handoff_workspace_incompatible": "source_workspace_changed",
        "handoff_trust_boundary_mismatch": "target_ineligible",
    }.get(code, code)


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else str(raw)


def _uuid_attr(value: object, name: str, reason: str) -> UUID:
    result = getattr(value, name, None)
    if not isinstance(result, UUID):
        raise HandoffBaseInspectionError(reason)
    return result


def _positive_attr(value: object, name: str, reason: str) -> int:
    result = getattr(value, name, None)
    if not isinstance(result, int) or isinstance(result, bool) or result < 1:
        raise HandoffBaseInspectionError(reason)
    return result


def _hash_attr(value: object, name: str, reason: str) -> str:
    result = getattr(value, name, None)
    if not isinstance(result, str) or len(result) != 64:
        raise HandoffBaseInspectionError(reason)
    return result
