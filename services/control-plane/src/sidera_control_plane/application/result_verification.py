from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.code_results import (
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.collaboration import CollaborationGateMode
from sidera_control_plane.domain.collaboration_runtime import canonical_json_bytes
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.modules import (
    LOCAL_VERIFICATION_PROVIDER,
    VERIFICATION_CONTEXT_PACKAGE_VERSION,
    VERIFICATION_RESULT_VERSION,
    WORKER_ADAPTER_MODULE_TYPE,
    WORKSPACE_PROVIDER_MODULE_TYPE,
    ModuleLifecycle,
    WorkerAdapterCapabilityProfile,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.result_integration import IntegrationSpecV1
from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.domain.result_verification import (
    EnvironmentBindingV1,
    VerificationCheckRefV1,
    VerificationManifestV1,
)

_REQUEST_NAMESPACE = uuid5(NAMESPACE_URL, "sidera:result-verification-request:v1")
_EVENT_NAMESPACE = uuid5(NAMESPACE_URL, "sidera:result-verification-event:v1")


class ResultVerificationService:
    """Derive one authorized verification request from an immutable candidate."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def schedule(
        self,
        *,
        workspace_id: UUID,
        candidate_id: UUID,
        command_id: UUID,
    ) -> UUID:
        _require_uuid(workspace_id, "workspace_id")
        _require_uuid(candidate_id, "candidate_id")
        _require_uuid(command_id, "command_id")
        with self._uow_factory() as uow:
            candidate = _load_candidate(uow, workspace_id, candidate_id)
            head = uow.results.get_head(
                workspace_id,
                candidate.root_task_id,
                for_update=True,
            )
            if head is None:
                raise ResultError("verification_result_head_not_found", {})
            if head.current_candidate_id != candidate.id:
                raise ResultError(
                    "verification_candidate_not_current",
                    {"candidate_id": str(candidate.id)},
                )

            plan, spec = _load_authorized_spec(uow, candidate, workspace_id)
            integration_execution = uow.executions.get(
                candidate.integration_execution_id,
                workspace_id,
                for_update=True,
            )
            if integration_execution is None:
                raise ResultError(
                    "verification_source_execution_not_found",
                    {"execution_id": str(candidate.integration_execution_id)},
                )
            if integration_execution.status is not ExecutionStatus.SUCCEEDED:
                raise ResultError(
                    "verification_source_not_terminal",
                    {
                        "execution_id": str(integration_execution.id),
                        "status": integration_execution.status.value,
                    },
                )
            source_request = uow.execution_requests.get_by_execution_lineage(
                integration_execution.lineage_id,
                workspace_id,
                for_update=True,
            )
            source_request = _validate_source_request(
                source_request,
                candidate=candidate,
                execution=integration_execution,
                spec=spec,
            )
            code_snapshot = _load_code_snapshot(uow, candidate, workspace_id)
            manifest = _build_verification_manifest(
                candidate=candidate,
                code_snapshot=code_snapshot,
                spec=spec,
            )
            _validate_verification_binding(uow, spec, workspace_id)

            request_id = verification_request_id(candidate.id)
            existing = uow.execution_requests.get(
                request_id,
                workspace_id,
                for_update=True,
            )
            if existing is not None:
                _validate_existing_request(existing, candidate, source_request)
                return existing.id

            if uow.commands.find_by_id(workspace_id, command_id) is not None:
                raise ResultError(
                    "verification_command_conflict",
                    {"command_id": str(command_id)},
                )
            now = self._now()
            request = ExecutionRequest.create(
                request_id=request_id,
                workspace_id=workspace_id,
                planning_request_id=source_request.planning_request_id,
                proposal_revision_id=source_request.proposal_revision_id,
                plan_snapshot_id=plan.id,
                plan_generation=plan.generation,
                root_task_id=candidate.root_task_id,
                subtask_id=source_request.subtask_id,
                worker_item_id=source_request.worker_item_id,
                worker_item_key=source_request.worker_item_key,
                intent_key=f"verification:{candidate.id}",
                purpose="verification",
                capability_requirements=frozenset({"verification"}),
                adapter_preferences=(LOCAL_VERIFICATION_PROVIDER,),
                adapter_forbidden=(),
                budget_class=source_request.budget_class,
                not_before=now,
                max_attempts=source_request.max_attempts,
                expected_artifact_refs=(),
                expected_evidence_refs=(),
                expected_outputs=tuple(
                    f"verification:{check.key}" for check in spec.checks
                ),
                collaboration_gate_mode=CollaborationGateMode.UNMANAGED,
                input_selection_id=None,
                input_selection_hash=None,
                result_derivation_kind="verification",
                source_candidate_id=candidate.id,
                now=now,
            )
            request.mark_ready(now=now)
            uow.execution_requests.add(request)
            uow.flush()

            event_id = uuid5(_EVENT_NAMESPACE, str(request.id))
            command = CommandEnvelope(
                id=command_id,
                workspace_id=workspace_id,
                command_type="ScheduleResultVerification",
                target_type="execution_request",
                actor_type="system",
                actor_id="result-verification",
                target_id=request.id,
                payload={
                    "execution_request_id": str(request.id),
                    "candidate_id": str(candidate.id),
                    "candidate_hash": candidate.content_hash,
                    "verification_basis_hash": manifest.verification_basis_hash,
                    "result_derivation_kind": request.result_derivation_kind,
                },
                expected_version=None,
                idempotency_key=f"result-verification:{candidate.id}",
                correlation_id=candidate.root_task_id,
            )
            uow.commands.record_applied(
                command,
                hash_request(command),
                CommandResult(
                    command_id=command.id,
                    aggregate_id=request.id,
                    aggregate_version=request.version,
                    event_ids=(event_id,),
                    data={
                        "execution_request_id": str(request.id),
                        "candidate_id": str(candidate.id),
                    },
                ),
            )
            uow.events.append_with_outbox(
                EventData(
                    id=event_id,
                    workspace_id=workspace_id,
                    aggregate_type="execution_request",
                    aggregate_id=request.id,
                    aggregate_version=request.version,
                    event_type="VerificationExecutionScheduled",
                    payload={
                        "execution_request_id": str(request.id),
                        "root_task_id": str(candidate.root_task_id),
                        "worker_item_id": str(request.worker_item_id),
                        "worker_item_key": request.worker_item_key,
                        "candidate_id": str(candidate.id),
                        "candidate_hash": candidate.content_hash,
                        "verification_basis_hash": manifest.verification_basis_hash,
                        "purpose": request.purpose,
                        "provider": LOCAL_VERIFICATION_PROVIDER,
                    },
                    actor_type="system",
                    actor_id="result-verification",
                    causation_id=command_id,
                    correlation_id=candidate.root_task_id,
                )
            )
            return request.id

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ResultError("verification_clock_invalid", {})
        return value.astimezone(UTC)


def verification_request_id(candidate_id: UUID) -> UUID:
    _require_uuid(candidate_id, "candidate_id")
    return uuid5(_REQUEST_NAMESPACE, str(candidate_id))


def verification_manifest_for_request(
    uow: ControlPlaneUnitOfWork,
    request: Any,
) -> VerificationManifestV1:
    """Rebuild the candidate-bound manifest when activation creates its context."""

    if getattr(request, "result_derivation_kind", None) != "verification":
        raise ResultError("verification_request_required", {})
    candidate_id = getattr(request, "source_candidate_id", None)
    if not isinstance(candidate_id, UUID):
        raise ResultError("verification_candidate_required", {})
    candidate = _load_candidate(uow, request.workspace_id, candidate_id)
    if candidate.root_task_id != request.root_task_id:
        raise ResultError("verification_candidate_scope_changed", {})
    if candidate.plan_snapshot_id != request.plan_snapshot_id:
        raise ResultError("verification_plan_changed", {})
    plan, spec = _load_authorized_spec(uow, candidate, request.workspace_id)
    if plan.generation != request.plan_generation:
        raise ResultError("verification_plan_changed", {})
    integration_execution = uow.executions.get(
        candidate.integration_execution_id,
        request.workspace_id,
    )
    if integration_execution is None:
        raise ResultError("verification_source_execution_not_found", {})
    source_request = uow.execution_requests.get_by_execution_lineage(
        integration_execution.lineage_id,
        request.workspace_id,
    )
    source_request = _validate_source_request(
        source_request,
        candidate=candidate,
        execution=integration_execution,
        spec=spec,
    )
    code_snapshot = _load_code_snapshot(uow, candidate, request.workspace_id)
    return _build_verification_manifest(
        candidate=candidate,
        code_snapshot=code_snapshot,
        spec=spec,
    )


def _load_candidate(
    uow: ControlPlaneUnitOfWork,
    workspace_id: UUID,
    candidate_id: UUID,
) -> IntegrationCandidateV1:
    record = uow.results.get_record("candidate", candidate_id, workspace_id)
    if record is None:
        raise ResultError(
            "verification_candidate_not_found",
            {"candidate_id": str(candidate_id)},
        )
    if not isinstance(record, IntegrationCandidateV1):
        raise ResultError(
            "verification_candidate_conflict",
            {"candidate_id": str(candidate_id)},
        )
    if record.workspace_id != workspace_id:
        raise ResultError("verification_candidate_scope_changed", {})
    return record


def _load_authorized_spec(
    uow: ControlPlaneUnitOfWork,
    candidate: IntegrationCandidateV1,
    workspace_id: UUID,
) -> tuple[TaskPlanSnapshot, IntegrationSpecV1]:
    plan = uow.plans.get(candidate.plan_snapshot_id, workspace_id)
    if plan is None:
        raise ResultError("verification_plan_not_found", {})
    if (
        plan.workspace_id != workspace_id
        or plan.root_task_id != candidate.root_task_id
        or plan_snapshot_content_hash(plan) != candidate.plan_content_hash
    ):
        raise ResultError("verification_plan_changed", {})
    current = uow.plans.get_current_for_root(candidate.root_task_id, workspace_id)
    if current is None or (
        current.id != candidate.plan_snapshot_id
        or current.generation != plan.generation
        or plan_snapshot_content_hash(current) != candidate.plan_content_hash
    ):
        raise ResultError("verification_plan_changed", {})
    spec = plan.integration_spec
    if not isinstance(spec, IntegrationSpecV1):
        raise ResultError("verification_spec_required", {})
    return plan, spec


def _validate_source_request(
    request: ExecutionRequest | None,
    *,
    candidate: IntegrationCandidateV1,
    execution: Any,
    spec: IntegrationSpecV1,
) -> ExecutionRequest:
    if request is None:
        raise ResultError("verification_source_request_not_found", {})
    if (
        request.workspace_id != candidate.workspace_id
        or request.root_task_id != candidate.root_task_id
        or request.subtask_id != execution.task_id
        or request.worker_item_id != execution.worker_item_id
        or request.execution_lineage_id != execution.lineage_id
        or request.result_derivation_kind is not None
        or request.worker_item_key != spec.integration_worker_item_key
    ):
        raise ResultError("verification_source_scope_changed", {})
    return request


def _load_code_snapshot(
    uow: ControlPlaneUnitOfWork,
    candidate: IntegrationCandidateV1,
    workspace_id: UUID,
) -> CodeResultSnapshotV1:
    record = uow.results.get_record("code_snapshot", candidate.code_result_id, workspace_id)
    if not isinstance(record, CodeResultSnapshotV1):
        raise ResultError("verification_code_result_not_found", {})
    if (
        record.workspace_id != workspace_id
        or record.root_task_id != candidate.root_task_id
        or record.id != candidate.code_result_id
        or record.execution_id != candidate.integration_execution_id
    ):
        raise ResultError("verification_code_result_scope_changed", {})
    return record


def _build_verification_manifest(
    *,
    candidate: IntegrationCandidateV1,
    code_snapshot: CodeResultSnapshotV1,
    spec: IntegrationSpecV1,
) -> VerificationManifestV1:
    environment_refs = {check.environment_ref for check in spec.checks}
    if len(environment_refs) != 1:
        raise ResultError("verification_environment_ambiguous", {})
    check_refs = tuple(
        VerificationCheckRefV1(
            key=check.key,
            version=check.version,
            spec_hash=sha256(canonical_json_bytes(check.to_mapping())).hexdigest(),
        )
        for check in spec.checks
    )
    environment_binding = EnvironmentBindingV1(
        ref=next(iter(environment_refs)),
        version=1,
    )
    basis = {
        "schema_version": 1,
        "candidate_id": str(candidate.id),
        "candidate_hash": candidate.content_hash,
        "result_commit": code_snapshot.result_commit,
        "result_tree": code_snapshot.result_tree,
        "checks": [item.to_mapping() for item in check_refs],
        "environment_binding": environment_binding.to_mapping(),
    }
    return VerificationManifestV1(
        schema_version=1,
        candidate_id=candidate.id,
        candidate_hash=candidate.content_hash,
        result_commit=code_snapshot.result_commit,
        result_tree=code_snapshot.result_tree,
        checks=check_refs,
        environment_binding=environment_binding,
        verification_basis_hash=sha256(canonical_json_bytes(basis)).hexdigest(),
    )


def _validate_verification_binding(
    uow: ControlPlaneUnitOfWork,
    spec: IntegrationSpecV1,
    workspace_id: UUID,
) -> None:
    binding = spec.verification_binding
    adapter_instance = uow.modules.get_instance(binding.adapter_module_ref, workspace_id)
    workspace_instance = uow.modules.get_instance(
        binding.workspace_module_ref,
        workspace_id,
    )
    if adapter_instance is None or workspace_instance is None:
        raise ResultError("verification_runtime_binding_missing", {})
    if (
        adapter_instance.config_version != binding.adapter_config_version
        or workspace_instance.config_version != binding.workspace_config_version
        or adapter_instance.lifecycle is not ModuleLifecycle.ACTIVE
        or workspace_instance.lifecycle is not ModuleLifecycle.ACTIVE
    ):
        raise ResultError("verification_runtime_binding_changed", {})
    adapter_definition = uow.modules.get_definition(
        adapter_instance.definition_id,
        workspace_id,
    )
    workspace_definition = uow.modules.get_definition(
        workspace_instance.definition_id,
        workspace_id,
    )
    if adapter_definition is None or workspace_definition is None:
        raise ResultError("verification_runtime_binding_missing", {})
    if (
        adapter_definition.module_type != WORKER_ADAPTER_MODULE_TYPE
        or workspace_definition.module_type != WORKSPACE_PROVIDER_MODULE_TYPE
        or adapter_definition.lifecycle is not ModuleLifecycle.ACTIVE
        or workspace_definition.lifecycle is not ModuleLifecycle.ACTIVE
    ):
        raise ResultError("verification_runtime_binding_changed", {})
    if adapter_definition.provider != LOCAL_VERIFICATION_PROVIDER:
        raise ResultError("verification_provider_required", {})
    capabilities = adapter_definition.capabilities
    if not isinstance(capabilities, WorkerAdapterCapabilityProfile):
        raise ResultError("verification_capability_unsupported", {})
    if not capabilities.supports_context_package_version(
        VERIFICATION_CONTEXT_PACKAGE_VERSION
    ) or not capabilities.supports_verification_version(VERIFICATION_RESULT_VERSION):
        raise ResultError("verification_capability_unsupported", {})
    workspace_capabilities = workspace_definition.capabilities
    if not isinstance(workspace_capabilities, WorkspaceProviderCapabilityProfile):
        raise ResultError("verification_workspace_unsupported", {})
    if not workspace_capabilities.git_worktree:
        raise ResultError("verification_workspace_unsupported", {})


def _validate_existing_request(
    request: Any,
    candidate: IntegrationCandidateV1,
    source_request: Any,
) -> None:
    if (
        request.result_derivation_kind != "verification"
        or request.source_candidate_id != candidate.id
        or request.workspace_id != candidate.workspace_id
        or request.root_task_id != candidate.root_task_id
        or request.worker_item_id != source_request.worker_item_id
        or request.worker_item_key != source_request.worker_item_key
        or request.input_selection_id is not None
        or request.source_execution_id is not None
        or request.source_session_id is not None
        or request.handoff_transition_id is not None
    ):
        raise ResultError("verification_request_conflict", {})
    if request.status not in {
        ExecutionRequestStatus.BLOCKED,
        ExecutionRequestStatus.READY,
        ExecutionRequestStatus.MATERIALIZED,
    }:
        raise ResultError("verification_request_closed", {})


def _require_uuid(value: object, label: str) -> None:
    if not isinstance(value, UUID):
        raise ResultError("verification_identity_invalid", {"field": label})


__all__ = [
    "ResultVerificationService",
    "verification_manifest_for_request",
    "verification_request_id",
]
