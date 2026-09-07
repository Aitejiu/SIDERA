from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.result_ports import (
    CheckRunObservation,
    CheckRunStatus,
    ResultOperationAttemptV1,
    ResultOperationStatus,
)
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
    VerificationResultV1,
    VerificationStatus,
)

_REQUEST_NAMESPACE = uuid5(NAMESPACE_URL, "sidera:result-verification-request:v1")
_EVENT_NAMESPACE = uuid5(NAMESPACE_URL, "sidera:result-verification-event:v1")
_RESULT_NAMESPACE = uuid5(NAMESPACE_URL, "sidera:result-verification-result:v1")
_RETRY_NAMESPACE = uuid5(NAMESPACE_URL, "sidera:result-verification-retry:v1")

_RETRYABLE_FAILURE_CODES = frozenset(
    {
        "deadline_exceeded",
        "log_limit_exceeded",
        "log_read_error",
        "prepare_start_failed",
        "prepare_failed",
        "process_state_unknown",
        "process_exit_code_unavailable",
        "source_state_unavailable",
        "stopped_by_request",
        "verification_observation_unavailable",
    }
)


class ResultVerificationService:
    """Derive one authorized verification request from an immutable candidate."""

    command_types = frozenset({"RetryResultVerification"})

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        return self.retry(uow, envelope)

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

    def finish(
        self,
        *,
        workspace_id: UUID,
        operation_id: UUID,
    ) -> tuple[VerificationResultV1, ...]:
        """Persist facts from one terminal local-check observation.

        The operation payload is the durable boundary between the runner and
        the control plane.  No worker-provided summary is consulted here.
        """

        _require_uuid(workspace_id, "workspace_id")
        _require_uuid(operation_id, "operation_id")
        with self._uow_factory() as uow:
            return finish_verification_in_uow(
                uow,
                workspace_id=workspace_id,
                operation_id=operation_id,
            )

    def retry(
        self,
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
    ) -> CommandResult:
        """Create one bounded retry attempt for a proven local-check failure."""

        if not isinstance(envelope, CommandEnvelope):
            raise ResultError("verification_retry_command_invalid", {})
        if envelope.command_type != "RetryResultVerification":
            raise ResultError(
                "verification_retry_command_invalid",
                {"command_type": envelope.command_type},
            )
        payload = envelope.payload
        operation_id = _payload_uuid(payload, "verification_operation_id")
        expected_operation_version = _payload_positive_int(
            payload, "expected_operation_version"
        )
        expected_result_version = _payload_positive_int(
            payload, "expected_result_version"
        )
        candidate_id = _payload_uuid(payload, "candidate_id")
        candidate_hash = _payload_text(payload, "candidate_hash")
        reason = _payload_text(payload, "reason")

        operation = uow.results.get_operation(
            operation_id,
            envelope.workspace_id,
            for_update=True,
        )
        if not isinstance(operation, ResultOperationAttemptV1):
            raise ResultError(
                "verification_operation_not_found",
                {"operation_id": str(operation_id)},
            )
        if operation.operation_kind != "verification":
            raise ResultError(
                "verification_operation_kind_invalid",
                {"operation_id": str(operation_id)},
            )
        if operation.version != expected_operation_version:
            raise ResultError(
                "verification_operation_version_conflict",
                {
                    "operation_id": str(operation_id),
                    "expected_version": expected_operation_version,
                    "actual_version": operation.version,
                },
            )
        candidate = _load_candidate(uow, envelope.workspace_id, candidate_id)
        if candidate.content_hash != candidate_hash:
            raise ResultError(
                "verification_candidate_changed",
                {"candidate_id": str(candidate.id)},
            )
        root_task_id = _operation_root_task_id(operation)
        if root_task_id != candidate.root_task_id:
            raise ResultError("verification_candidate_scope_changed", {})
        head = uow.results.get_head(
            envelope.workspace_id,
            root_task_id,
            for_update=True,
        )
        if head is None:
            raise ResultError("verification_result_head_not_found", {})
        if head.result_version != expected_result_version:
            raise ResultError(
                "verification_result_version_conflict",
                {
                    "expected_version": expected_result_version,
                    "actual_version": head.result_version,
                },
            )
        if head.current_candidate_id != candidate.id:
            raise ResultError(
                "verification_candidate_not_current",
                {"candidate_id": str(candidate.id)},
            )

        now = self._now()
        if head.auto_deadline is not None and now >= head.auto_deadline:
            raise ResultError("rework_deadline_exceeded", {})

        observation = _observation_from_operation(operation)
        if observation is None:
            raise ResultError(
                "verification_observation_not_terminal",
                {"operation_id": str(operation.operation_id)},
            )
        if observation.status is CheckRunStatus.UNKNOWN:
            # An unknown process outcome must be inspected by its owner.  It
            # never creates a second process from this command.
            return CommandResult(
                command_id=envelope.id,
                aggregate_id=operation.operation_id,
                aggregate_version=operation.version,
                event_ids=(),
                data={
                    "operation_id": str(operation.operation_id),
                    "attempt": operation.attempt,
                    "status": "unknown",
                    "inspection_required": True,
                },
            )
        if observation.status not in {
            CheckRunStatus.INTERRUPTED,
            CheckRunStatus.ENVIRONMENT_BLOCKED,
            CheckRunStatus.TIMED_OUT,
            CheckRunStatus.FAILED,
        }:
            raise ResultError(
                "verification_retry_not_allowed",
                {"operation_id": str(operation.operation_id)},
            )
        failure_code = observation.failure_code or operation.failure_code
        if (
            observation.status is not CheckRunStatus.INTERRUPTED
            and failure_code not in _RETRYABLE_FAILURE_CODES
        ):
            raise ResultError(
                "verification_retry_not_proven",
                {"operation_id": str(operation.operation_id)},
            )

        next_attempt = operation.attempt + 1
        next_operation_id = uuid5(
            _RETRY_NAMESPACE,
            f"{operation.operation_id}:{next_attempt}",
        )
        existing = uow.results.get_operation(
            next_operation_id,
            envelope.workspace_id,
            for_update=True,
        )
        if existing is not None:
            if not isinstance(existing, ResultOperationAttemptV1):
                raise ResultError("verification_retry_operation_conflict", {})
            if not _retry_matches(
                existing,
                previous=operation,
                request_id=envelope.id,
                reason=reason,
            ):
                raise ResultError("verification_retry_operation_conflict", {})
            return CommandResult(
                command_id=envelope.id,
                aggregate_id=existing.operation_id,
                aggregate_version=existing.version,
                event_ids=(),
                data={
                    "operation_id": str(existing.operation_id),
                    "previous_operation_id": str(operation.operation_id),
                    "attempt": existing.attempt,
                    "status": existing.status.value,
                },
            )

        next_operation = ResultOperationAttemptV1(
            operation_id=next_operation_id,
            workspace_id=operation.workspace_id,
            root_task_id=operation.root_task_id,
            operation_kind="verification",
            attempt=next_attempt,
            status=ResultOperationStatus.PENDING,
            payload={
                **dict(operation.payload),
                "previous_operation_id": str(operation.operation_id),
                "retry_reason": reason,
                "retry_requested_by": str(envelope.id),
                "attempt": next_attempt,
                "observation": None,
            },
            lease_owner=None,
            lease_token=None,
            lease_expiry=None,
            receipt_ref=None,
            failure_code=None,
            version=1,
            created_at=now,
            updated_at=now,
        )
        uow.results.save_operation(next_operation, expected_version=0)
        event_id = uuid5(_EVENT_NAMESPACE, f"retry:{next_operation.operation_id}")
        uow.events.append_with_outbox(
            EventData(
                id=event_id,
                workspace_id=envelope.workspace_id,
                aggregate_type="verification_operation",
                aggregate_id=next_operation.operation_id,
                aggregate_version=next_operation.version,
                event_type="VerificationRetryRequested",
                payload={
                    "operation_id": str(next_operation.operation_id),
                    "previous_operation_id": str(operation.operation_id),
                    "candidate_id": str(candidate.id),
                    "attempt": next_attempt,
                    "reason": reason,
                },
                actor_type=envelope.actor_type,
                actor_id=envelope.actor_id,
                causation_id=envelope.id,
                correlation_id=candidate.root_task_id,
            )
        )
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=next_operation.operation_id,
            aggregate_version=next_operation.version,
            event_ids=(event_id,),
            data={
                "operation_id": str(next_operation.operation_id),
                "previous_operation_id": str(operation.operation_id),
                "attempt": next_attempt,
                "status": next_operation.status.value,
            },
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ResultError("verification_clock_invalid", {})
        return value.astimezone(UTC)


def finish_verification_in_uow(
    uow: ControlPlaneUnitOfWork,
    *,
    workspace_id: UUID,
    operation_id: UUID,
) -> tuple[VerificationResultV1, ...]:
    """Finish a verification operation without opening a nested transaction."""

    _require_uuid(workspace_id, "workspace_id")
    _require_uuid(operation_id, "operation_id")
    existing = uow.results.get_verification_by_operation(
        operation_id,
        workspace_id,
        for_update=True,
    )
    if existing is not None:
        return uow.results.list_verification_results(existing.candidate_id, workspace_id)

    operation = uow.results.get_operation(operation_id, workspace_id, for_update=True)
    if not isinstance(operation, ResultOperationAttemptV1):
        raise ResultError(
            "verification_operation_not_found",
            {"operation_id": str(operation_id)},
        )
    if operation.operation_kind != "verification":
        raise ResultError(
            "verification_operation_kind_invalid",
            {"operation_id": str(operation_id)},
        )
    observation = _observation_from_operation(operation)
    if observation is None or observation.status is CheckRunStatus.RUNNING:
        raise ResultError(
            "verification_observation_not_terminal",
            {"operation_id": str(operation_id)},
        )

    execution_id = _payload_uuid(operation.payload, "execution_id")
    execution = uow.executions.get(execution_id, workspace_id, for_update=True)
    if execution is None:
        raise ResultError(
            "verification_execution_not_found",
            {"execution_id": str(execution_id)},
        )
    request = uow.execution_requests.get_by_execution_lineage(
        execution.lineage_id,
        workspace_id,
        for_update=True,
    )
    if request is None or request.result_derivation_kind != "verification":
        raise ResultError(
            "verification_request_not_found",
            {"execution_id": str(execution_id)},
        )
    if request.first_execution_id not in (None, execution.id):
        raise ResultError("verification_execution_scope_changed", {})

    candidate_id = _payload_uuid(operation.payload, "candidate_id")
    if request.source_candidate_id != candidate_id:
        raise ResultError("verification_candidate_scope_changed", {})
    candidate = _load_candidate(uow, workspace_id, candidate_id)
    if candidate.root_task_id != request.root_task_id:
        raise ResultError("verification_candidate_scope_changed", {})
    manifest = verification_manifest_for_request(uow, request)
    check_key = _payload_text(operation.payload, "check_key")
    check_ref = next((item for item in manifest.checks if item.key == check_key), None)
    if check_ref is None:
        raise ResultError(
            "verification_check_not_declared",
            {"check_key": check_key},
        )
    check_hash = _payload_optional_text(operation.payload, "check_hash")
    if check_hash is not None and check_hash != check_ref.spec_hash:
        raise ResultError("verification_check_changed", {"check_key": check_key})
    if observation.actual_commit != manifest.result_commit or observation.actual_tree != manifest.result_tree:
        raise ResultError(
            "verification_source_changed",
            {"candidate_id": str(candidate.id)},
        )

    status, failure_code = _normalize_observation(observation)
    result = VerificationResultV1(
        id=uuid5(_RESULT_NAMESPACE, f"{operation.operation_id}:{operation.attempt}"),
        schema_version=1,
        workspace_id=workspace_id,
        root_task_id=candidate.root_task_id,
        candidate_id=candidate.id,
        execution_id=execution.id,
        check_key=check_ref.key,
        check_version=check_ref.version,
        check_hash=check_ref.spec_hash,
        environment_ref=manifest.environment_binding.ref,
        environment_hash=_environment_hash(manifest.environment_binding),
        actual_commit=observation.actual_commit,
        actual_tree=observation.actual_tree,
        operation_id=operation.operation_id,
        attempt=operation.attempt,
        status=status,
        exit_code=observation.exit_code,
        started_at=observation.started_at,
        finished_at=observation.finished_at,
        artifact_ids=_artifact_ids_from_payload(operation.payload),
        failure_code=failure_code,
    )
    stored = uow.results.add_immutable("verification_result", result)
    if not isinstance(stored, VerificationResultV1):
        raise ResultError("verification_result_conflict", {})

    operation_status = _operation_status_for_result(status)
    updated_payload = {
        **dict(operation.payload),
        "verification_result_id": str(stored.id),
        "observation": observation.to_mapping(),
    }
    if operation.status is not operation_status or operation.payload != updated_payload:
        updated = replace(
            operation,
            status=operation_status,
            payload=updated_payload,
            failure_code=failure_code,
            version=operation.version + 1,
            updated_at=observation.finished_at or datetime.now(UTC),
        )
        uow.results.save_operation(updated, expected_version=operation.version)

    head = uow.results.get_head(workspace_id, candidate.root_task_id, for_update=True)
    result_version = 1
    if head is not None and head.current_candidate_id == candidate.id:
        result_version = head.result_version + 1
        uow.results.save_head(
            replace(head, result_version=result_version),
            expected_version=head.result_version,
        )
    event_id = uuid5(_EVENT_NAMESPACE, f"result:{stored.id}")
    finish_command_id = uuid5(
        _EVENT_NAMESPACE,
        f"finish-command:{operation.operation_id}",
    )
    finish_command = CommandEnvelope(
        id=finish_command_id,
        workspace_id=workspace_id,
        command_type="FinishResultVerification",
        target_type="verification_result",
        actor_type="system",
        actor_id="result-verification",
        target_id=stored.id,
        payload={
            "verification_result_id": str(stored.id),
            "operation_id": str(operation.operation_id),
            "candidate_id": str(candidate.id),
            "attempt": stored.attempt,
        },
        expected_version=None,
        idempotency_key=f"finish-result-verification:{operation.operation_id}",
        correlation_id=candidate.root_task_id,
    )
    if uow.commands.find_by_id(workspace_id, finish_command_id) is None:
        uow.commands.record_applied(
            finish_command,
            hash_request(finish_command),
            CommandResult(
                command_id=finish_command_id,
                aggregate_id=stored.id,
                aggregate_version=result_version,
                event_ids=(event_id,),
                data={"verification_result_id": str(stored.id)},
            ),
        )
    uow.events.append_with_outbox(
        EventData(
            id=event_id,
            workspace_id=workspace_id,
            aggregate_type="verification_result",
            aggregate_id=stored.id,
            aggregate_version=result_version,
            event_type="VerificationResultFinished",
            payload={
                "verification_result_id": str(stored.id),
                "operation_id": str(operation.operation_id),
                "root_task_id": str(candidate.root_task_id),
                "candidate_id": str(candidate.id),
                "candidate_hash": candidate.content_hash,
                "check_key": stored.check_key,
                "attempt": stored.attempt,
                "status": stored.status.value,
                "actual_commit": stored.actual_commit,
                "actual_tree": stored.actual_tree,
                "failure_code": stored.failure_code,
                "result_version": result_version,
            },
            actor_type="system",
            actor_id="result-verification",
            causation_id=finish_command_id,
            correlation_id=candidate.root_task_id,
        )
    )
    return uow.results.list_verification_results(candidate.id, workspace_id)


def record_verification_observation(
    uow: ControlPlaneUnitOfWork,
    *,
    execution: Any,
    event: Any,
) -> tuple[VerificationResultV1, ...] | None:
    """Persist a terminal adapter observation and project its result atomically."""

    payload = getattr(event, "payload", None)
    if not isinstance(payload, Mapping) or payload.get("purpose") != "verification":
        return None
    observation = _observation_from_payload(payload)
    if observation is None or observation.status is CheckRunStatus.RUNNING:
        return None
    workspace_id = execution.workspace_id
    request = uow.execution_requests.get_by_execution_lineage(
        execution.lineage_id,
        workspace_id,
        for_update=True,
    )
    if request is None or request.result_derivation_kind != "verification":
        raise ResultError("verification_request_not_found", {})
    candidate_id = _payload_uuid(payload, "candidate_id")
    check_key = _payload_text(payload, "check_key")
    attempt = payload.get("attempt", 1)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ResultError("verification_attempt_invalid", {})
    operation_payload: dict[str, object] = {
        "execution_id": str(execution.id),
        "request_id": str(request.id),
        "candidate_id": str(candidate_id),
        "candidate_hash": payload.get("candidate_hash"),
        "check_key": check_key,
        "check_hash": payload.get("check_hash"),
        "attempt": attempt,
        "observation": observation.to_mapping(),
    }
    existing = uow.results.get_operation(
        observation.operation_id,
        workspace_id,
        for_update=True,
    )
    if existing is None:
        now = getattr(event, "occurred_at", None)
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            now = datetime.now(UTC)
        operation = ResultOperationAttemptV1(
            operation_id=observation.operation_id,
            workspace_id=workspace_id,
            root_task_id=request.root_task_id,
            operation_kind="verification",
            attempt=attempt,
            status=_operation_status_for_observation(observation.status),
            payload=operation_payload,
            lease_owner=None,
            lease_token=None,
            lease_expiry=None,
            receipt_ref=None,
            failure_code=observation.failure_code,
            version=1,
            created_at=now.astimezone(UTC),
            updated_at=now.astimezone(UTC),
        )
        uow.results.save_operation(operation, expected_version=0)
    elif not isinstance(existing, ResultOperationAttemptV1):
        raise ResultError("verification_operation_conflict", {})
    else:
        if (
            existing.operation_kind != "verification"
            or existing.root_task_id != request.root_task_id
            or existing.attempt != attempt
            or existing.payload.get("candidate_id") != str(candidate_id)
            or existing.payload.get("check_key") != check_key
        ):
            raise ResultError("verification_operation_conflict", {})
    return finish_verification_in_uow(
        uow,
        workspace_id=workspace_id,
        operation_id=observation.operation_id,
    )


def _observation_from_operation(
    operation: ResultOperationAttemptV1,
) -> CheckRunObservation | None:
    return _observation_from_payload(operation.payload.get("observation"))


def _observation_from_payload(value: object) -> CheckRunObservation | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return CheckRunObservation.from_mapping(value)
    except (TypeError, ValueError):
        raise ResultError("verification_observation_invalid", {}) from None


def _normalize_observation(
    observation: CheckRunObservation,
) -> tuple[VerificationStatus, str | None]:
    if observation.source_dirty is True:
        return VerificationStatus.BLOCKED, "source_modified_during_check"
    if observation.status is CheckRunStatus.PASSED:
        if observation.exit_code != 0:
            return VerificationStatus.FAILED, "check_failed"
        return VerificationStatus.PASSED, None
    if observation.status is CheckRunStatus.FAILED:
        return VerificationStatus.FAILED, observation.failure_code or "check_failed"
    if observation.status is CheckRunStatus.INTERRUPTED:
        return VerificationStatus.INTERRUPTED, observation.failure_code or "interrupted"
    if observation.status is CheckRunStatus.UNKNOWN:
        return VerificationStatus.UNKNOWN, observation.failure_code or "process_state_unknown"
    return VerificationStatus.BLOCKED, observation.failure_code or "verification_environment_blocked"


def _operation_status_for_observation(status: CheckRunStatus) -> ResultOperationStatus:
    if status is CheckRunStatus.PASSED:
        return ResultOperationStatus.SUCCEEDED
    if status is CheckRunStatus.UNKNOWN:
        return ResultOperationStatus.UNKNOWN
    return ResultOperationStatus.FAILED


def _operation_status_for_result(status: VerificationStatus) -> ResultOperationStatus:
    if status is VerificationStatus.PASSED:
        return ResultOperationStatus.SUCCEEDED
    if status is VerificationStatus.UNKNOWN:
        return ResultOperationStatus.UNKNOWN
    return ResultOperationStatus.FAILED


def _environment_hash(binding: EnvironmentBindingV1) -> str:
    return sha256(canonical_json_bytes(binding.to_mapping())).hexdigest()


def _artifact_ids_from_payload(payload: Mapping[str, object]) -> tuple[UUID, ...]:
    value = payload.get("artifact_ids", ())
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[UUID] = []
    for item in value:
        try:
            parsed = item if isinstance(item, UUID) else UUID(str(item))
        except (TypeError, ValueError):
            raise ResultError("verification_artifact_ids_invalid", {}) from None
        if parsed in result:
            raise ResultError("verification_artifact_ids_invalid", {})
        result.append(parsed)
    return tuple(result)


def _operation_root_task_id(operation: ResultOperationAttemptV1) -> UUID:
    return operation.root_task_id


def _payload_uuid(payload: Mapping[str, object], name: str) -> UUID:
    value = payload.get(name)
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    raise ResultError("verification_payload_invalid", {"field": name})


def _payload_text(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ResultError("verification_payload_invalid", {"field": name})
    return value.strip()


def _payload_optional_text(payload: Mapping[str, object], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    return _payload_text(payload, name)


def _payload_positive_int(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ResultError("verification_payload_invalid", {"field": name})
    return value


def _retry_matches(
    existing: ResultOperationAttemptV1,
    *,
    previous: ResultOperationAttemptV1,
    request_id: UUID,
    reason: str,
) -> bool:
    return (
        existing.workspace_id == previous.workspace_id
        and existing.root_task_id == previous.root_task_id
        and existing.operation_kind == "verification"
        and existing.attempt == previous.attempt + 1
        and existing.payload.get("previous_operation_id") == str(previous.operation_id)
        and existing.payload.get("retry_reason") == reason
        and existing.payload.get("retry_requested_by") == str(request_id)
    )


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
    is_reintegration_request = (
        request.purpose == "reintegrate"
        and request.result_derivation_kind == "rework"
    )
    if (
        request.workspace_id != candidate.workspace_id
        or request.root_task_id != candidate.root_task_id
        or request.subtask_id != execution.task_id
        or request.worker_item_id != execution.worker_item_id
        or request.execution_lineage_id != execution.lineage_id
        or (
            request.result_derivation_kind is not None
            and not is_reintegration_request
        )
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
    "finish_verification_in_uow",
    "record_verification_observation",
    "verification_manifest_for_request",
    "verification_request_id",
]
