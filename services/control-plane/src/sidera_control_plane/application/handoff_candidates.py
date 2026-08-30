from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sidera_control_plane.application.errors import Conflict, NotFound
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_policy import (
    AssignmentDecision,
    LowestCostWorkerPolicy,
    WorkerCandidate,
    WorkerPolicy,
)
from sidera_control_plane.application.worker_runtime import (
    ResolvedWorkerRuntime,
    WorkerRuntimeInspectionPort,
    WorkerRuntimeResolver,
)
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextPackageStatus,
    ContextPurpose,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSession,
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.gateway import (
    GatewayOperationStatus,
    GatewayOperationType,
)
from sidera_control_plane.domain.handoffs import (
    HandoffCandidateInspection,
    HandoffCandidateList,
    HandoffTransferMode,
)
from sidera_control_plane.domain.modules import (
    HandoffCheckpointMode,
    StopCapability,
    WorkerAdapterCapabilityProfile,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.plan_transitions import PlanTransitionStatus
from sidera_control_plane.domain.worker import (
    SCHEDULABLE_WORKER_STATUSES,
    Worker,
    WorkerDesiredState,
)
from sidera_control_plane.domain.worker_item import WorkerItem, WorkerItemStatus
from sidera_control_plane.domain.workspace import WorkspaceLifecycle

_BLOCKING_CODE_ORDER = (
    "handoff_source_not_active",
    "handoff_source_ambiguous",
    "handoff_already_active",
    "handoff_plan_transition_active",
    "handoff_input_in_flight",
    "handoff_operation_unknown",
    "handoff_runtime_incompatible",
    "handoff_checkpoint_unsupported",
    "handoff_workspace_incompatible",
    "handoff_trust_boundary_mismatch",
)
_BLOCKING_CODE_RANK = {
    code: index for index, code in enumerate(_BLOCKING_CODE_ORDER)
}
_ACTIVE_GATEWAY_STATUSES = {
    GatewayOperationStatus.REQUESTED,
    GatewayOperationStatus.RUNNING,
    GatewayOperationStatus.UNKNOWN,
}


class HandoffCandidateBlocked(Conflict):
    """The source cannot produce a complete candidate list."""

    code = "handoff_candidate_blocked"

    def __init__(
        self,
        reason_code: str,
        *,
        workspace_id: UUID,
        worker_item_id: UUID,
    ) -> None:
        self.reason_code = reason_code
        super().__init__(
            "handoff candidate inspection is blocked",
            details={
                "reason_code": reason_code,
                "workspace_id": str(workspace_id),
                "worker_item_id": str(worker_item_id),
            },
        )


# Keep a descriptive alias for callers that treat this as an inspection error.
HandoffCandidateInspectionError = HandoffCandidateBlocked


class HandoffCandidateService:
    """Build a deterministic, read-only view of possible handoff targets."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        policy: WorkerPolicy | None = None,
        runtime_resolver: WorkerRuntimeInspectionPort | None = None,
        heartbeat_timeout: timedelta = timedelta(seconds=45),
    ) -> None:
        self._uow_factory = uow_factory
        self._policy = policy or LowestCostWorkerPolicy()
        self._runtime_resolver = runtime_resolver or WorkerRuntimeResolver()
        self._heartbeat_timeout = heartbeat_timeout

    def inspect(
        self,
        *,
        workspace_id: UUID,
        worker_item_id: UUID,
        now: datetime,
    ) -> HandoffCandidateList:
        _aware(now, "handoff candidate inspected_at")
        with self._uow_factory() as uow:
            return self.inspect_in_uow(
                uow,
                workspace_id=workspace_id,
                worker_item_id=worker_item_id,
                now=now,
            )

    def inspect_in_uow(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        workspace_id: UUID,
        worker_item_id: UUID,
        now: datetime,
    ) -> HandoffCandidateList:
        _aware(now, "handoff candidate inspected_at")
        worker_item = uow.worker_items.get(worker_item_id, workspace_id)
        if worker_item is None or worker_item.workspace_id != workspace_id:
            raise NotFound("worker item not found")
        if worker_item.status is not WorkerItemStatus.ACTIVE:
            raise HandoffCandidateBlocked(
                "handoff_source_not_active",
                workspace_id=workspace_id,
                worker_item_id=worker_item_id,
            )

        execution, session, source_worker = self._active_source(
            uow,
            worker_item=worker_item,
            workspace_id=workspace_id,
        )
        manifest = uow.workspaces.get_for_execution(execution.id, workspace_id)
        package = uow.context_packages.get_for_consumer(
            ContextPurpose.EXECUTION,
            execution.id,
            workspace_id,
        )
        plan_head = uow.plans.get_head(worker_item.root_task_id, workspace_id)
        plan_transitions = uow.plan_transitions.list_for_root(
            worker_item.root_task_id,
            workspace_id,
        )
        active_handoff = uow.handoff_transitions.get_active_for_worker_item(
            worker_item_id,
            workspace_id,
        )
        pending_input = uow.execution_inputs.get_pending_for_session(
            session.id,
            workspace_id,
        )
        gateway_operations = uow.gateway_operations.list_for_execution(
            execution.id,
            workspace_id,
        )
        visible_workers = _visible_workers(uow, workspace_id, source_worker)

        runtime_inspections = self._runtime_resolver.inspect_candidates(
            uow,
            visible_workers,
            execution,
        )
        resolved_by_worker = {
            inspection.worker.id: inspection.resolved
            for inspection in runtime_inspections
        }
        source_runtime = resolved_by_worker.get(source_worker.id)

        blocking_codes: set[str] = set()
        if active_handoff is not None:
            blocking_codes.add("handoff_already_active")
        if any(
            _enum_value(item.status) == PlanTransitionStatus.CONVERGING
            for item in plan_transitions
        ):
            blocking_codes.add("handoff_plan_transition_active")
        if pending_input is not None and _enum_value(
            pending_input.status
        ) == "dispatching":
            blocking_codes.add("handoff_input_in_flight")
        for operation in gateway_operations:
            operation_status = _enum_value(operation.status)
            if operation_status not in _ACTIVE_GATEWAY_STATUSES:
                continue
            if operation_status == GatewayOperationStatus.UNKNOWN:
                blocking_codes.add("handoff_operation_unknown")
            elif (
                _enum_value(operation.operation_type)
                == GatewayOperationType.SEND_INPUT
            ):
                blocking_codes.add("handoff_input_in_flight")
            else:
                blocking_codes.add("handoff_operation_unknown")

        if source_runtime is None:
            blocking_codes.add("handoff_runtime_incompatible")
        else:
            if not _source_checkpoint_supported(source_runtime.capability_snapshot):
                blocking_codes.add("handoff_checkpoint_unsupported")
            if not _source_workspace_ready(manifest, execution, package, worker_item):
                blocking_codes.add("handoff_workspace_incompatible")
            if any(value is None for value in _scope_facts(source_runtime).values()):
                blocking_codes.add("handoff_workspace_incompatible")
            if plan_head is None:
                blocking_codes.add("handoff_runtime_incompatible")

        candidates = self._evaluate_targets(
            execution=execution,
            source_worker=source_worker,
            source_runtime=source_runtime,
            workers=visible_workers,
            resolved_by_worker=resolved_by_worker,
            now=now,
        )

        recommendation = None
        recommendation_reason: Mapping[str, object] | None = None
        if not blocking_codes:
            eligible_workers = tuple(
                worker
                for worker, candidate in candidates
                if candidate.eligible
            )
            decision = self._policy.select(
                execution,
                tuple(WorkerCandidate.from_worker(worker) for worker in eligible_workers),
            )
            recommendation = _valid_decision(decision, eligible_workers)
            if recommendation is not None and decision is not None:
                recommendation_reason = decision.reason

        inspected_candidates = tuple(
            _with_recommendation(
                candidate,
                worker.id == recommendation,
                recommendation_reason if worker.id == recommendation else None,
            )
            for worker, candidate in sorted(
                candidates,
                key=lambda item: _candidate_sort_key(
                    item[1], item[0].display_name, item[0].id == recommendation
                ),
            )
        )
        return HandoffCandidateList(
            workspace_id=workspace_id,
            worker_item_id=worker_item_id,
            source_execution_id=execution.id,
            source_session_id=session.id,
            source_worker_id=source_worker.id,
            source_worker_item_version=worker_item.version,
            candidates=inspected_candidates,
            recommended_worker_id=recommendation,
            blocking_codes=_ordered_codes(blocking_codes),
            inspected_at=now,
        )

    def _active_source(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        worker_item: WorkerItem,
        workspace_id: UUID,
    ) -> tuple[Execution, ExecutionSession, Worker]:
        executions = uow.executions.list_for_worker_item(
            worker_item.id,
            workspace_id,
        )
        active: list[tuple[Execution, ExecutionSession]] = []
        for execution in executions:
            if _enum_value(execution.status) != ExecutionStatus.RUNNING:
                continue
            sessions = _active_sessions(uow, execution.id, workspace_id)
            active.extend(
                (execution, session)
                for session in sessions
                if session.execution_id == execution.id
                and session.workspace_id == workspace_id
                and _enum_value(session.status) == ExecutionSessionStatus.ACTIVE
            )
        if not active:
            raise HandoffCandidateBlocked(
                "handoff_source_not_active",
                workspace_id=workspace_id,
                worker_item_id=worker_item.id,
            )
        if len(active) != 1:
            raise HandoffCandidateBlocked(
                "handoff_source_ambiguous",
                workspace_id=workspace_id,
                worker_item_id=worker_item.id,
            )
        execution, session = active[0]
        source_worker = uow.workers.get(session.worker_id, workspace_id)
        if source_worker is None or source_worker.workspace_id != workspace_id:
            raise HandoffCandidateBlocked(
                "handoff_source_not_active",
                workspace_id=workspace_id,
                worker_item_id=worker_item.id,
            )
        return execution, session, source_worker

    def _evaluate_targets(
        self,
        *,
        execution: Execution,
        source_worker: Worker,
        source_runtime: ResolvedWorkerRuntime | None,
        workers: tuple[Worker, ...],
        resolved_by_worker: Mapping[UUID, ResolvedWorkerRuntime | None],
        now: datetime,
    ) -> tuple[tuple[Worker, HandoffCandidateInspection], ...]:
        evaluated: list[tuple[Worker, HandoffCandidateInspection]] = []
        for worker in workers:
            if worker.id == source_worker.id:
                continue
            runtime = resolved_by_worker.get(worker.id)
            code, workspace_compatibility = _target_eligibility(
                worker=worker,
                execution=execution,
                source_runtime=source_runtime,
                target_runtime=runtime,
                now=now,
                heartbeat_timeout=self._heartbeat_timeout,
            )
            runtime_snapshot = (
                _runtime_snapshot(runtime) if runtime is not None else None
            )
            capability_hash = (
                runtime_snapshot.get("capability_snapshot_hash")
                if runtime_snapshot is not None
                else None
            )
            if not isinstance(capability_hash, str):
                capability_hash = None
            if code is None:
                policy_reason: Mapping[str, object] = {
                    "eligible": True,
                    "policy": getattr(self._policy, "name", "worker-policy"),
                }
            else:
                policy_reason = {
                    "eligible": False,
                    "reason_code": code,
                }
            evaluated.append(
                (
                    worker,
                    HandoffCandidateInspection(
                        worker_id=worker.id,
                        display_name=worker.display_name,
                        adapter_type=worker.adapter_type,
                        eligible=code is None,
                        recommended=False,
                        ineligibility_code=code,
                        runtime_snapshot=runtime_snapshot,
                        capability_snapshot_hash=capability_hash,
                        policy_reason=policy_reason,
                        workspace_compatibility=workspace_compatibility,
                    ),
                )
            )
        return tuple(evaluated)


def _active_sessions(
    uow: ControlPlaneUnitOfWork,
    execution_id: UUID,
    workspace_id: UUID,
) -> tuple[ExecutionSession, ...]:
    return uow.sessions.list_active_for_execution(execution_id, workspace_id)


def _visible_workers(
    uow: ControlPlaneUnitOfWork,
    workspace_id: UUID,
    source_worker: Worker,
) -> tuple[Worker, ...]:
    workers = list(uow.workers.list_for_workspace(workspace_id))
    if all(worker.id != source_worker.id for worker in workers):
        workers.append(source_worker)
    return tuple(sorted(workers, key=lambda worker: (worker.display_name.casefold(), str(worker.id))))


def _target_eligibility(
    *,
    worker: Worker,
    execution: Execution,
    source_runtime: ResolvedWorkerRuntime | None,
    target_runtime: ResolvedWorkerRuntime | None,
    now: datetime,
    heartbeat_timeout: timedelta,
) -> tuple[str | None, dict[str, object]]:
    workspace_compatibility = _workspace_compatibility(
        source_runtime=source_runtime,
        target_runtime=target_runtime,
    )
    if worker.workspace_id != execution.workspace_id:
        return "handoff_workspace_incompatible", workspace_compatibility
    if (
        worker.status not in SCHEDULABLE_WORKER_STATUSES
        or worker.desired_state is not WorkerDesiredState.ACTIVE
        or not worker.is_fresh(now=now, timeout=heartbeat_timeout)
    ):
        return "handoff_target_unhealthy", workspace_compatibility
    if worker.current_load >= worker.capacity:
        return "handoff_target_capacity", workspace_compatibility
    if target_runtime is None:
        return "handoff_runtime_incompatible", workspace_compatibility
    if target_runtime.worker_id != worker.id:
        return "handoff_runtime_incompatible", workspace_compatibility
    if not execution.capability_requirements.issubset(worker.capabilities):
        return "handoff_runtime_incompatible", workspace_compatibility
    if worker.adapter_type in execution.adapter_forbidden:
        return "handoff_runtime_incompatible", workspace_compatibility
    target_adapter = target_runtime.capability_snapshot
    if not isinstance(target_adapter, WorkerAdapterCapabilityProfile):
        return "handoff_runtime_incompatible", workspace_compatibility
    if 1 not in target_adapter.handoff_session_context_versions:
        return "handoff_context_binding_unsupported", workspace_compatibility
    if 1 not in target_adapter.handoff_writer_gate_versions:
        return "handoff_writer_gate_unsupported", workspace_compatibility
    if not target_adapter.handoff_inspection:
        return "handoff_runtime_incompatible", workspace_compatibility
    if source_runtime is None:
        return "handoff_runtime_incompatible", workspace_compatibility
    if (
        target_runtime.workspace_module_instance_id
        != source_runtime.workspace_module_instance_id
        or target_runtime.workspace_module_config_version
        != source_runtime.workspace_module_config_version
    ):
        return "handoff_workspace_incompatible", workspace_compatibility
    target_workspace = target_runtime.workspace_capability_snapshot
    if not isinstance(target_workspace, WorkspaceProviderCapabilityProfile):
        return "handoff_workspace_incompatible", workspace_compatibility
    if (
        1 not in target_workspace.workspace_handoff_versions
        or HandoffTransferMode.REUSE_EXACT
        not in target_workspace.workspace_transfer_modes
        or not target_workspace.handoff_inspection
    ):
        return "handoff_workspace_incompatible", workspace_compatibility
    source_scopes = _scope_facts(source_runtime)
    target_scopes = _scope_facts(target_runtime)
    if any(value is None for value in (*source_scopes.values(), *target_scopes.values())):
        return "handoff_workspace_incompatible", workspace_compatibility
    if source_scopes["trust"] != target_scopes["trust"]:
        return "handoff_trust_boundary_mismatch", workspace_compatibility
    if (
        source_scopes["repository"] != target_scopes["repository"]
        or source_scopes["paths"] != target_scopes["paths"]
    ):
        return "handoff_workspace_incompatible", workspace_compatibility
    return None, workspace_compatibility


def _source_checkpoint_supported(profile: object) -> bool:
    return isinstance(profile, WorkerAdapterCapabilityProfile) and (
        profile.handoff_checkpoint_mode is not HandoffCheckpointMode.UNSUPPORTED
        and 1 in profile.handoff_checkpoint_versions
        and profile.stop is StopCapability.EXACT_EXECUTION
        and profile.handoff_inspection
    )


def _source_workspace_ready(
    manifest: object | None,
    execution: Execution,
    package: object | None,
    worker_item: WorkerItem,
) -> bool:
    if manifest is None:
        return False
    if getattr(manifest, "workspace_id", None) != execution.workspace_id:
        return False
    if getattr(manifest, "execution_id", None) != execution.id:
        return False
    if _enum_value(getattr(manifest, "lifecycle", None)) != WorkspaceLifecycle.ACTIVE:
        return False
    if package is None:
        return False
    if _enum_value(getattr(package, "purpose", None)) != ContextPurpose.EXECUTION:
        return False
    if _enum_value(getattr(package, "consumer_type", None)) != ContextConsumerType.WORKER:
        return False
    if getattr(package, "consumer_id", None) != execution.id:
        return False
    if getattr(package, "worker_item_id", None) != worker_item.id:
        return False
    if getattr(package, "context_version", None) != worker_item.context_version:
        return False
    return _enum_value(getattr(package, "status", None)) == ContextPackageStatus.ACTIVE


def _workspace_compatibility(
    *,
    source_runtime: ResolvedWorkerRuntime | None,
    target_runtime: ResolvedWorkerRuntime | None,
) -> dict[str, object]:
    result: dict[str, object] = {"transfer_mode": HandoffTransferMode.REUSE_EXACT.value}
    if source_runtime is None or target_runtime is None:
        result["runtime_resolved"] = False
        return result
    target_workspace = target_runtime.workspace_capability_snapshot
    result.update(
        {
            "runtime_resolved": True,
            "same_module_instance": target_runtime.workspace_module_instance_id
            == source_runtime.workspace_module_instance_id,
            "same_config_version": target_runtime.workspace_module_config_version
            == source_runtime.workspace_module_config_version,
            "handoff_version": 1
            if isinstance(target_workspace, WorkspaceProviderCapabilityProfile)
            and 1 in target_workspace.workspace_handoff_versions
            else None,
            "reuse_exact": isinstance(target_workspace, WorkspaceProviderCapabilityProfile)
            and HandoffTransferMode.REUSE_EXACT
            in target_workspace.workspace_transfer_modes,
            "inspection": isinstance(target_workspace, WorkspaceProviderCapabilityProfile)
            and target_workspace.handoff_inspection,
        }
    )
    source_scopes = _scope_facts(source_runtime)
    target_scopes = _scope_facts(target_runtime)
    result.update(
        {
            "trust_boundary_match": source_scopes["trust"] == target_scopes["trust"],
            "repository_scope_match": source_scopes["repository"]
            == target_scopes["repository"],
            "path_scope_match": source_scopes["paths"] == target_scopes["paths"],
        }
    )
    return result


def _scope_facts(runtime: ResolvedWorkerRuntime) -> dict[str, str | None]:
    facts = runtime.resolution_facts
    return {
        "trust": _fact_text(facts, "workspace_trust_boundary_hash"),
        "repository": _fact_text(facts, "workspace_repository_scope_hash"),
        "paths": _fact_text(facts, "workspace_path_scope_hash"),
    }


def _fact_text(facts: Mapping[str, object], key: str) -> str | None:
    value = facts.get(key)
    return value if isinstance(value, str) else None


def _runtime_snapshot(runtime: ResolvedWorkerRuntime) -> dict[str, object]:
    capability_hash = _capability_hash(runtime.capability_snapshot)
    safe_facts = _json_value(runtime.resolution_facts)
    return {
        "worker_id": str(runtime.worker_id),
        "adapter_type": runtime.adapter_type,
        "adapter_module_instance_id": str(runtime.adapter_module_instance_id),
        "adapter_module_config_version": runtime.adapter_module_config_version,
        "workspace_module_instance_id": str(runtime.workspace_module_instance_id),
        "workspace_module_config_version": runtime.workspace_module_config_version,
        "adapter_definition_id": str(runtime.adapter_definition_id),
        "workspace_definition_id": str(runtime.workspace_definition_id),
        "capability_snapshot_hash": capability_hash,
        "resolution_facts_hash": _sha256(safe_facts),
        **{
            key: value
            for key in (
                "workspace_trust_boundary_hash",
                "workspace_repository_scope_hash",
                "workspace_path_scope_hash",
            )
            if isinstance((value := runtime.resolution_facts.get(key)), str)
        },
    }


def runtime_snapshot_for_handoff(runtime: ResolvedWorkerRuntime) -> dict[str, object]:
    """Return the bounded runtime facts safe to freeze in a handoff proposal."""

    return _runtime_snapshot(runtime)


def _capability_hash(profile: object) -> str:
    if not is_dataclass(profile):
        return _sha256(_json_value(profile))
    document = {
        field.name: _json_value(getattr(profile, field.name))
        for field in fields(profile)
    }
    return _sha256({"profile_type": type(profile).__name__, "fields": document})


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _sha256(value: object) -> str:
    encoded = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _with_recommendation(
    candidate: HandoffCandidateInspection,
    recommended: bool,
    recommendation_reason: Mapping[str, object] | None = None,
) -> HandoffCandidateInspection:
    policy_reason = dict(recommendation_reason or candidate.policy_reason)
    if candidate.eligible:
        policy_reason["selected"] = recommended
    return HandoffCandidateInspection(
        worker_id=candidate.worker_id,
        display_name=candidate.display_name,
        adapter_type=candidate.adapter_type,
        eligible=candidate.eligible,
        recommended=recommended,
        ineligibility_code=candidate.ineligibility_code,
        runtime_snapshot=candidate.runtime_snapshot,
        capability_snapshot_hash=candidate.capability_snapshot_hash,
        policy_reason=policy_reason,
        workspace_compatibility=candidate.workspace_compatibility,
    )


def _candidate_sort_key(
    candidate: HandoffCandidateInspection,
    display_name: str,
    recommended: bool,
) -> tuple[int, int, str, str]:
    return (
        0 if recommended else 1,
        0 if candidate.eligible else 1,
        display_name.casefold(),
        str(candidate.worker_id),
    )


def _valid_decision(
    decision: AssignmentDecision | None,
    eligible_workers: Sequence[Worker],
) -> UUID | None:
    if decision is None:
        return None
    eligible_ids = {worker.id for worker in eligible_workers}
    return decision.worker_id if decision.worker_id in eligible_ids else None


def _ordered_codes(codes: set[str]) -> tuple[str, ...]:
    return tuple(
        sorted(codes, key=lambda code: (_BLOCKING_CODE_RANK.get(code, 10_000), code))
    )


def _enum_value(value: object) -> object:
    if isinstance(value, StrEnum):
        return value
    return value


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must be timezone aware")
