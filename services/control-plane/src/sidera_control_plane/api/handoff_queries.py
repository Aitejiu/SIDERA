from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder

from sidera_control_plane.api.schemas import (
    ActiveHandoffProjectionResponse,
    HandoffCandidateListResponse,
    HandoffProposalResponse,
    HandoffTransitionResponse,
)
from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.handoff_candidates import (
    HandoffCandidateBlocked,
    HandoffCandidateService,
)
from sidera_control_plane.application.handoff_proposal_reconciler import (
    HandoffProposalReconciler,
)
from sidera_control_plane.application.handoff_queries import (
    HandoffQueryService as ActiveHandoffQueryService,
)
from sidera_control_plane.application.handoff_recovery import (
    HandoffRecoveryError,
    HandoffRecoveryService,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.handoffs import (
    HandoffCandidateList,
    HandoffProposal,
    HandoffTransition,
)
from sidera_control_plane.domain.worker_item_projections import (
    ActiveHandoffProjection,
)

router = APIRouter()


class HandoffPublicQueryError(DomainError):
    """A stable, user-facing handoff inspection error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.code = code


class HandoffPublicQueryService:
    """Compose bounded handoff views from authoritative control-plane facts."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        candidate_service: HandoffCandidateService,
        active_handoff_service: ActiveHandoffQueryService,
        recovery_service: HandoffRecoveryService,
        runtime_resolver: object,
    ) -> None:
        self._uow_factory = uow_factory
        self._candidate_service = candidate_service
        self._active_handoff_service = active_handoff_service
        self._recovery_service = recovery_service
        self._runtime_resolver = runtime_resolver

    def get_candidates(
        self,
        workspace_id: UUID,
        worker_item_id: UUID,
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        try:
            candidates = self._candidate_service.inspect(
                workspace_id=workspace_id,
                worker_item_id=worker_item_id,
                now=now,
            )
        except HandoffCandidateBlocked as exc:
            raise HandoffPublicQueryError(
                exc.reason_code,
                "handoff candidate inspection is blocked",
                details=exc.details,
            ) from exc

        with self._uow_factory() as uow:
            execution = uow.executions.get(
                candidates.source_execution_id,
                workspace_id,
            )
            workers = {
                worker.id: worker
                for worker in uow.workers.list_for_workspace(workspace_id)
            }
        return _candidate_list_document(candidates, execution, workers, now=now)

    def get_proposal(
        self,
        workspace_id: UUID,
        proposal_id: UUID,
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        reconciler = HandoffProposalReconciler(
            self._uow_factory,
            candidate_service=self._candidate_service,
            runtime_resolver=self._runtime_resolver,  # type: ignore[arg-type]
        )
        reconciler.reconcile(proposal_id, workspace_id, now)
        with self._uow_factory() as uow:
            proposal = uow.handoff_proposals.get(proposal_id, workspace_id)
            if proposal is None:
                raise NotFound("handoff proposal not found")
            return _proposal_document(uow, proposal)

    def get_transition(
        self,
        workspace_id: UUID,
        transition_id: UUID,
    ) -> dict[str, object]:
        with self._uow_factory() as uow:
            transition = uow.handoff_transitions.get(transition_id, workspace_id)
            if transition is None:
                raise NotFound("handoff transition not found")
            return self._transition_document(uow, transition)

    def get_worker_item_handoff(
        self,
        workspace_id: UUID,
        worker_item_id: UUID,
    ) -> ActiveHandoffProjection:
        projection = self._active_handoff_service.get_for_worker_item(
            workspace_id,
            worker_item_id,
        )
        if projection is None:
            raise NotFound("active handoff not found")
        return projection

    def _transition_document(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: HandoffTransition,
    ) -> dict[str, object]:
        proposal = uow.handoff_proposals.get(
            transition.proposal_id,
            transition.workspace_id,
        )
        if proposal is None:
            raise NotFound("handoff proposal not found")
        payload = proposal.payload
        worker_item = _require_worker_item(
            uow,
            transition.worker_item_id,
            transition.workspace_id,
        )
        source_worker = _require_worker(
            uow,
            payload.source_worker_id,
            transition.workspace_id,
        )
        target_worker = _require_worker(
            uow,
            transition.target_worker_id,
            transition.workspace_id,
        )
        source_execution = uow.executions.get(
            transition.source_execution_id,
            transition.workspace_id,
        )
        target_execution = (
            uow.executions.get(transition.target_execution_id, transition.workspace_id)
            if transition.target_execution_id is not None
            else None
        )
        source_session = uow.sessions.get(
            transition.source_session_id,
            transition.workspace_id,
        )
        if source_session is None:
            source_session = uow.sessions.get_any(
                transition.source_session_id,
                transition.workspace_id,
            )
        target_session = (
            uow.sessions.get(transition.target_session_id, transition.workspace_id)
            if transition.target_session_id is not None
            else None
        )
        if target_session is None and transition.target_session_id is not None:
            target_session = uow.sessions.get_any(
                transition.target_session_id,
                transition.workspace_id,
            )
        source_manifest = uow.workspaces.get_for_execution(
            transition.source_execution_id,
            transition.workspace_id,
        )
        target_manifest = (
            uow.workspaces.get_for_execution(
                transition.target_execution_id,
                transition.workspace_id,
            )
            if transition.target_execution_id is not None
            else None
        )
        source = _participant_document(
            uow,
            worker_item_id=transition.worker_item_id,
            worker=source_worker,
            execution=source_execution,
            session=source_session,
            runtime_snapshot=payload.source_runtime_snapshot,
            capability_snapshot_hash=_snapshot_hash(payload.source_runtime_snapshot),
            context_version=payload.source_worker_context_version,
            manifest_id=transition.source_workspace_manifest_id,
            manifest_version=payload.source_workspace_manifest_version,
            root_task_id=payload.root_task_id,
        )
        target = _participant_document(
            uow,
            worker_item_id=transition.worker_item_id,
            worker=target_worker,
            execution=target_execution,
            session=target_session,
            runtime_snapshot=transition.target_runtime_snapshot,
            capability_snapshot_hash=payload.target_capability_snapshot_hash,
            context_version=transition.target_context_version,
            manifest_id=transition.target_workspace_manifest_id,
            manifest_version=(
                target_manifest.version if target_manifest is not None else None
            ),
            root_task_id=payload.root_task_id,
        )
        strategies, evidence_refs, inspected_at = self._recovery_options(transition)
        task = uow.tasks.get(payload.root_task_id, transition.workspace_id)
        subtask = uow.tasks.get(payload.subtask_id, transition.workspace_id)
        compatibility = _workspace_compatibility(payload.target_policy_facts)
        return {
            "id": transition.id,
            "workspace_id": transition.workspace_id,
            "root_task_id": payload.root_task_id,
            "subtask_id": payload.subtask_id,
            "task_title": task.title if task is not None else None,
            "subtask_title": subtask.title if subtask is not None else None,
            "worker_item": _worker_item_document(worker_item),
            "proposal_id": transition.proposal_id,
            "proposal_status": _value(proposal.status),
            "proposal_hash": proposal.proposal_hash,
            "version": transition.version,
            "status": _value(transition.status),
            "phase": _value(transition.phase),
            "writer_state": _value(transition.writer_state),
            "logical_attempt": transition.logical_attempt,
            "source_input_cutoff_sequence": transition.source_input_cutoff_sequence,
            "target_context_version": transition.target_context_version,
            "failure_code": transition.failure_code,
            "blocked_operation_id": transition.blocked_operation_id,
            "next_attempt_at": transition.next_attempt_at,
            "confirmed_at": transition.confirmed_at,
            "started_at": transition.started_at,
            "completed_at": transition.completed_at,
            "attention_required": bool(
                _value(transition.status) == "blocked_unknown"
                or (
                    _value(transition.status) in {"blocked", "failed"}
                    and not strategies
                )
            ),
            "available_recovery_strategies": strategies,
            "source": source,
            "target": target,
            "workspace": _workspace_document(
                transfer_mode=_value(payload.transfer_mode),
                source_manifest=source_manifest,
                source_manifest_id=transition.source_workspace_manifest_id,
                source_manifest_version=payload.source_workspace_manifest_version,
                target_manifest=target_manifest,
                compatibility=compatibility,
            ),
            "context": {
                "source_package_id": payload.source_context_package_id,
                "source_package_hash": payload.source_context_package_hash,
                "source_context_version": payload.source_worker_context_version,
                "target_package_id": transition.target_context_package_id,
                "target_package_hash": transition.target_context_package_hash,
                "target_context_version": transition.target_context_version,
            },
            "source_trace_descriptor": _trace_descriptor(
                payload.root_task_id,
                transition.source_execution_id,
                transition.source_session_id,
            ),
            "target_trace_descriptor": _trace_descriptor_optional(
                payload.root_task_id,
                transition.target_execution_id,
                transition.target_session_id,
            ),
            "recovery_evidence_refs": evidence_refs,
            "recovery_inspected_at": inspected_at,
        }

    def _recovery_options(
        self,
        transition: HandoffTransition,
    ) -> tuple[tuple[str, ...], tuple[str, ...], datetime | None]:
        if _value(transition.status) not in {"blocked", "blocked_unknown", "failed"}:
            return (), (), None
        now = datetime.now(UTC)
        try:
            inspection = self._recovery_service.inspect_unknown(
                transition=transition,
                now=now,
            )
            strategies = self._recovery_service.available_strategies(
                transition=transition,
                inspection=inspection,
            )
            return (
                tuple(item.value for item in strategies),
                inspection.evidence_refs,
                inspection.inspected_at,
            )
        except (DomainError, HandoffRecoveryError):
            return _fallback_recovery_options(transition), (), None


def _candidate_list_document(
    candidates: HandoffCandidateList,
    execution: Any,
    workers: Mapping[UUID, Any],
    *,
    now: datetime,
) -> dict[str, object]:
    return {
        "workspace_id": candidates.workspace_id,
        "worker_item_id": candidates.worker_item_id,
        "source_execution_id": candidates.source_execution_id,
        "source_session_id": candidates.source_session_id,
        "source_worker_id": candidates.source_worker_id,
        "source_worker_item_version": candidates.source_worker_item_version,
        "candidates": tuple(
            _candidate_document(item, workers.get(item.worker_id), execution, now=now)
            for item in candidates.candidates
        ),
        "recommended_worker_id": candidates.recommended_worker_id,
        "blocking_codes": candidates.blocking_codes,
        "inspected_at": candidates.inspected_at,
    }


def _candidate_document(
    candidate: Any,
    worker: Any,
    execution: Any,
    *,
    now: datetime,
) -> dict[str, object]:
    capabilities = ()
    health_summary = "unknown"
    if worker is not None:
        requirements = getattr(execution, "capability_requirements", frozenset())
        capabilities = tuple(sorted(set(requirements) & set(worker.capabilities)))
        health_summary = (
            f"{_value(worker.status)}; load {worker.current_load}/{worker.capacity}"
        )
    return {
        **candidate.to_mapping(),
        "runtime_profile_label": candidate.adapter_type,
        "capabilities_satisfied": capabilities,
        "health_summary": health_summary,
        "policy_reasons": (dict(candidate.policy_reason),),
        "inspected_at": now,
    }


def _proposal_document(
    uow: ControlPlaneUnitOfWork,
    proposal: HandoffProposal,
) -> dict[str, object]:
    payload = proposal.payload
    worker_item = _require_worker_item(uow, payload.worker_item_id, payload.workspace_id)
    source_worker = _require_worker(uow, payload.source_worker_id, payload.workspace_id)
    target_worker = _require_worker(uow, payload.target_worker_id, payload.workspace_id)
    source_execution = uow.executions.get(payload.source_execution_id, payload.workspace_id)
    source_session = uow.sessions.get(payload.source_session_id, payload.workspace_id)
    if source_session is None:
        source_session = uow.sessions.get_any(
            payload.source_session_id,
            payload.workspace_id,
        )
    source_manifest = uow.workspaces.get_for_execution(
        payload.source_execution_id,
        payload.workspace_id,
    )
    source = _participant_document(
        uow,
        worker_item_id=payload.worker_item_id,
        worker=source_worker,
        execution=source_execution,
        session=source_session,
        runtime_snapshot=payload.source_runtime_snapshot,
        capability_snapshot_hash=_snapshot_hash(payload.source_runtime_snapshot),
        context_version=payload.source_worker_context_version,
        manifest_id=payload.source_workspace_manifest_id,
        manifest_version=payload.source_workspace_manifest_version,
        root_task_id=payload.root_task_id,
    )
    target = _participant_document(
        uow,
        worker_item_id=payload.worker_item_id,
        worker=target_worker,
        execution=None,
        session=None,
        runtime_snapshot=payload.target_runtime_snapshot,
        capability_snapshot_hash=payload.target_capability_snapshot_hash,
        context_version=payload.source_worker_context_version,
        manifest_id=None,
        manifest_version=None,
        root_task_id=payload.root_task_id,
    )
    root_task = uow.tasks.get(payload.root_task_id, payload.workspace_id)
    subtask = uow.tasks.get(payload.subtask_id, payload.workspace_id)
    return {
        "id": proposal.id,
        "workspace_id": payload.workspace_id,
        "root_task_id": payload.root_task_id,
        "subtask_id": payload.subtask_id,
        "task_title": root_task.title if root_task is not None else None,
        "subtask_title": subtask.title if subtask is not None else None,
        "worker_item": _worker_item_document(worker_item),
        "status": _value(proposal.status),
        "stale_reason": proposal.stale_reason,
        "version": proposal.version,
        "proposal_hash": proposal.proposal_hash,
        "source_channel": _value(proposal.source_channel),
        "created_by_type": proposal.created_by_type,
        "created_by_id": proposal.created_by_id,
        "created_at": proposal.created_at,
        "expires_at": proposal.expires_at,
        "confirmed_at": proposal.confirmed_at,
        "cancelled_at": proposal.cancelled_at,
        "handoff_reason": payload.handoff_reason,
        "handoff_instruction": payload.handoff_instruction,
        "transfer_mode": _value(payload.transfer_mode),
        "source": source,
        "target": target,
        "workspace": _workspace_document(
            transfer_mode=_value(payload.transfer_mode),
            source_manifest=source_manifest,
            source_manifest_id=payload.source_workspace_manifest_id,
            source_manifest_version=payload.source_workspace_manifest_version,
            target_manifest=None,
            compatibility=_workspace_compatibility(payload.target_policy_facts),
        ),
        "context": {
            "source_package_id": payload.source_context_package_id,
            "source_package_hash": payload.source_context_package_hash,
            "source_context_version": payload.source_worker_context_version,
            "target_package_id": None,
            "target_package_hash": None,
            "target_context_version": payload.source_worker_context_version,
        },
        "plan_base": {
            "snapshot_id": payload.observed_plan_snapshot_id,
            "generation": payload.observed_plan_generation,
            "head_version": payload.observed_plan_head_version,
        },
        "input_plan": dict(payload.input_plan),
        "impact_summary": dict(payload.impact_summary),
    }


def _worker_item_document(worker_item: Any) -> dict[str, object]:
    return {
        "worker_item_id": worker_item.id,
        "title": worker_item.title,
        "responsibility": worker_item.responsibility,
        "status": _value(worker_item.status),
        "version": worker_item.version,
        "context_version": worker_item.context_version,
    }


def _participant_document(
    uow: ControlPlaneUnitOfWork,
    *,
    worker_item_id: UUID,
    worker: Any,
    execution: Any,
    session: Any,
    runtime_snapshot: Mapping[str, object],
    capability_snapshot_hash: str | None,
    context_version: int | None,
    manifest_id: UUID | None,
    manifest_version: int | None,
    root_task_id: UUID,
) -> dict[str, object]:
    return {
        "worker": {
            "worker_id": worker.id,
            "display_name": worker.display_name,
            "adapter_type": worker.adapter_type,
            "version": worker.version,
            "status": _value(worker.status),
            "desired_state": _value(worker.desired_state),
            "current_load": worker.current_load,
            "capacity": worker.capacity,
            "capabilities": tuple(sorted(worker.capabilities)),
            "runtime_profile_label": worker.adapter_type,
            "capability_snapshot_hash": capability_snapshot_hash,
        },
        "runtime": _runtime_document(runtime_snapshot, capability_snapshot_hash),
        "execution": _execution_document(
            uow,
            execution=execution,
            session=session,
            root_task_id=root_task_id,
        ),
        "worker_item_id": worker_item_id,
        "context_version": context_version,
        "workspace_manifest_id": manifest_id,
        "workspace_manifest_version": manifest_version,
    }


def _runtime_document(
    snapshot: Mapping[str, object],
    capability_snapshot_hash: str | None,
) -> dict[str, object]:
    return {
        "adapter_module_instance_id": _snapshot_uuid(
            snapshot,
            "adapter_module_instance_id",
        ),
        "adapter_module_config_version": _snapshot_int(
            snapshot,
            "adapter_module_config_version",
        ),
        "workspace_module_instance_id": _snapshot_uuid(
            snapshot,
            "workspace_module_instance_id",
        ),
        "workspace_module_config_version": _snapshot_int(
            snapshot,
            "workspace_module_config_version",
        ),
        "capability_snapshot_hash": capability_snapshot_hash
        or _snapshot_hash(snapshot),
    }


def _execution_document(
    uow: ControlPlaneUnitOfWork,
    *,
    execution: Any,
    session: Any,
    root_task_id: UUID,
) -> dict[str, object] | None:
    if execution is None:
        return None
    session_id = session.id if session is not None else None
    return {
        "execution_id": execution.id,
        "task_id": execution.task_id,
        "status": _value(execution.status),
        "attempt": execution.attempt,
        "version": execution.version,
        "session_id": session_id,
        "session_status": _value(session.status) if session is not None else None,
        "session_version": session.version if session is not None else None,
        "last_sequence": session.last_sequence if session is not None else None,
        "trace_descriptor": _trace_descriptor_optional(
            root_task_id,
            execution.id,
            session_id,
        ),
    }


def _workspace_document(
    *,
    transfer_mode: str,
    source_manifest: Any,
    source_manifest_id: UUID,
    source_manifest_version: int,
    target_manifest: Any,
    compatibility: Mapping[str, object],
) -> dict[str, object]:
    return {
        "transfer_mode": transfer_mode,
        "source_manifest_id": source_manifest_id,
        "source_manifest_version": source_manifest_version,
        "source_lifecycle": (
            _value(source_manifest.lifecycle) if source_manifest is not None else None
        ),
        "target_manifest_id": target_manifest.id if target_manifest is not None else None,
        "target_manifest_version": (
            target_manifest.version if target_manifest is not None else None
        ),
        "target_lifecycle": (
            _value(target_manifest.lifecycle) if target_manifest is not None else None
        ),
        "compatibility": dict(compatibility),
    }


def _workspace_compatibility(facts: Mapping[str, object]) -> dict[str, object]:
    value = facts.get("workspace_compatibility")
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _trace_descriptor(
    root_task_id: UUID,
    execution_id: UUID,
    session_id: UUID,
) -> dict[str, object]:
    return {
        "root_task_id": root_task_id,
        "execution_id": execution_id,
        "session_id": session_id,
        "route": (
            f"/api/v1/tasks/{root_task_id}/executions/{execution_id}/"
            f"sessions/{session_id}/trace"
        ),
        "raw_ref": None,
    }


def _trace_descriptor_optional(
    root_task_id: UUID,
    execution_id: UUID | None,
    session_id: UUID | None,
) -> dict[str, object] | None:
    if execution_id is None or session_id is None:
        return None
    return _trace_descriptor(root_task_id, execution_id, session_id)


def _require_worker_item(
    uow: ControlPlaneUnitOfWork,
    worker_item_id: UUID,
    workspace_id: UUID,
) -> Any:
    worker_item = uow.worker_items.get(worker_item_id, workspace_id)
    if worker_item is None:
        raise NotFound("worker item not found")
    return worker_item


def _require_worker(
    uow: ControlPlaneUnitOfWork,
    worker_id: UUID,
    workspace_id: UUID,
) -> Any:
    worker = uow.workers.get(worker_id, workspace_id)
    if worker is None:
        raise NotFound("handoff worker not found")
    return worker


def _snapshot_hash(snapshot: Mapping[str, object]) -> str | None:
    value = snapshot.get("capability_snapshot_hash")
    return value if isinstance(value, str) else None


def _snapshot_uuid(snapshot: Mapping[str, object], name: str) -> UUID | None:
    value = snapshot.get(name)
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _snapshot_int(snapshot: Mapping[str, object], name: str) -> int | None:
    value = snapshot.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _fallback_recovery_options(transition: HandoffTransition) -> tuple[str, ...]:
    if (
        _value(transition.status) == "blocked"
        and transition.failure_code
        not in {"gateway_recovery_required", "recovery_required", "recovery_decision_required"}
    ):
        return ("retry_current_phase",)
    return ()


def _value(value: object) -> str:
    return value.value if isinstance(value, StrEnum) else str(value)


def _get_handoff_service(request: Request) -> HandoffPublicQueryService:
    # Import lazily so dependencies.py can type the service without a module cycle.
    from sidera_control_plane.api.dependencies import (
        get_handoff_public_query_service,
    )

    return get_handoff_public_query_service(
        request.app.state.database,
        request.app.state.settings,
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/handoff-candidates",
    response_model=HandoffCandidateListResponse,
    operation_id="get_handoff_candidates",
)
def get_handoff_candidates(
    workspace_id: UUID,
    worker_item_id: UUID,
    service: Annotated[
        HandoffPublicQueryService, Depends(_get_handoff_service)
    ],
) -> HandoffCandidateListResponse:
    return HandoffCandidateListResponse.model_validate(
        jsonable_encoder(service.get_candidates(workspace_id, worker_item_id))
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/handoff-proposals/{proposal_id}",
    response_model=HandoffProposalResponse,
    operation_id="get_handoff_proposal",
)
def get_handoff_proposal(
    workspace_id: UUID,
    proposal_id: UUID,
    service: Annotated[
        HandoffPublicQueryService, Depends(_get_handoff_service)
    ],
) -> HandoffProposalResponse:
    return HandoffProposalResponse.model_validate(
        jsonable_encoder(service.get_proposal(workspace_id, proposal_id))
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/handoff-transitions/{transition_id}",
    response_model=HandoffTransitionResponse,
    operation_id="get_handoff_transition",
)
def get_handoff_transition(
    workspace_id: UUID,
    transition_id: UUID,
    service: Annotated[
        HandoffPublicQueryService, Depends(_get_handoff_service)
    ],
) -> HandoffTransitionResponse:
    return HandoffTransitionResponse.model_validate(
        jsonable_encoder(service.get_transition(workspace_id, transition_id))
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/handoff",
    response_model=ActiveHandoffProjectionResponse,
    operation_id="get_worker_item_handoff",
)
def get_worker_item_handoff(
    workspace_id: UUID,
    worker_item_id: UUID,
    service: Annotated[
        HandoffPublicQueryService, Depends(_get_handoff_service)
    ],
) -> ActiveHandoffProjectionResponse:
    projection = service.get_worker_item_handoff(workspace_id, worker_item_id)
    return ActiveHandoffProjectionResponse.model_validate(
        jsonable_encoder(asdict(projection))
    )
