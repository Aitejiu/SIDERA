from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.result_finalization import (
    FinalizationOutcome,
    ResultFinalizationService,
)
from sidera_control_plane.application.result_gate import (
    ResultGateAction,
    require_result_gate,
)
from sidera_control_plane.application.result_ports import (
    ApplyOutcome,
    ApplyReceipt,
    CodeResultApplyPort,
)
from sidera_control_plane.domain.code_results import (
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.result_operations import (
    ResultApplyOperationV1,
    ResultError,
    ResultOperationStatus,
    TaskResultHead,
    transition_operation,
)
from sidera_control_plane.domain.result_verification import TaskAcceptanceDecisionV1

_EVENT_NAMESPACE = uuid5(NAMESPACE_URL, "sidera:result-apply-events:v1")
_RESULT_APPLY_LEASE_TTL = timedelta(minutes=5)
_UNCERTAIN_FAILURE_CODES = frozenset(
    {
        "result_apply_receipt_identity_changed",
        "result_apply_receipt_invalid",
        "result_apply_receipt_not_successful",
        "result_apply_receipt_corrupt",
        "result_apply_receipt_inspection_failed",
        "result_apply_ref_write_failed",
        "result_apply_repository_unavailable",
    }
)


class ResultApplyCrash(RuntimeError):
    """Test-only crash boundary used to verify durable recovery."""

    def __init__(self, point: str) -> None:
        if not isinstance(point, str) or not point.strip():
            raise ValueError("result apply crash point is required")
        self.point = point.strip()
        super().__init__(self.point)


class ResultApplyState(StrEnum):
    COMPLETED = "completed"
    PENDING = "pending"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ResultApplyOutcome:
    state: str
    operation_id: UUID
    failure_code: str | None = None
    receipt_ref: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {item.value for item in ResultApplyState}:
            raise ValueError(f"unsupported result apply state: {self.state}")
        if not isinstance(self.operation_id, UUID):
            raise TypeError("result apply operation_id must be a UUID")
        if self.failure_code is not None and not self.failure_code.strip():
            raise ValueError("result apply failure_code must not be empty")
        if self.receipt_ref is not None and not self.receipt_ref.strip():
            raise ValueError("result apply receipt_ref must not be empty")


@dataclass(frozen=True, slots=True)
class _PreparedApply:
    operation: ResultApplyOperationV1


class ResultApplyService:
    """Reconcile a durable result-application intent across DB and Git."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        code_store: CodeResultApplyPort,
        *,
        finalizer: ResultFinalizationService | None = None,
        clock: Callable[[], datetime] | None = None,
        owner_id: str = "result-applier",
        lease_ttl: timedelta = _RESULT_APPLY_LEASE_TTL,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("result apply owner_id is required")
        if lease_ttl <= timedelta(0):
            raise ValueError("result apply lease_ttl must be positive")
        self._uow_factory = uow_factory
        self._code_store = code_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner_id = owner_id.strip()
        self._lease_ttl = lease_ttl
        self._fault_injector = fault_injector
        self._finalizer = finalizer or ResultFinalizationService(
            uow_factory,
            clock=self._clock,
            owner_id=f"{self._owner_id}:finalizer",
        )

    def reconcile(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        operation_id: UUID | None = None,
    ) -> ResultApplyOutcome:
        _require_uuid(workspace_id, "workspace_id")
        _require_uuid(root_task_id, "root_task_id")
        if operation_id is not None:
            _require_uuid(operation_id, "operation_id")

        prepared = self._prepare(
            workspace_id=workspace_id,
            root_task_id=root_task_id,
            operation_id=operation_id,
        )
        if isinstance(prepared, ResultApplyOutcome):
            return prepared
        operation = prepared.operation
        if operation.status is ResultOperationStatus.SUCCEEDED:
            return self._finish_succeeded(operation)

        try:
            receipt = self._code_store.inspect_apply(operation)
        except ResultError as exc:
            return self._mark_external_failure(operation, exc)

        if receipt is None and operation.status is ResultOperationStatus.UNKNOWN:
            return self._block_unknown(operation)
        if receipt is None:
            self._fault("before_git_apply")
            try:
                receipt = self._code_store.apply(operation)
            except ResultError as exc:
                return self._mark_external_failure(operation, exc)
            self._fault("after_git_apply_before_db_commit")
        try:
            return self._record_receipt(operation, receipt)
        except ResultError as exc:
            return self._mark_external_failure(operation, exc)

    def _prepare(
        self,
        *,
        workspace_id: UUID,
        root_task_id: UUID,
        operation_id: UUID | None,
    ) -> _PreparedApply | ResultApplyOutcome:
        with self._uow_factory() as uow:
            require_result_gate(
                uow,
                workspace_id,
                root_task_id,
                ResultGateAction.RECORD_RECEIPT,
            )
            task = uow.tasks.get(root_task_id, workspace_id, for_update=True)
            if task is None:
                return _blocked(None, "root_task_missing")
            head = uow.results.get_head(workspace_id, root_task_id, for_update=True)
            if head is None:
                return _blocked(None, "result_head_missing")
            selected_id = operation_id or head.active_operation_id
            if selected_id is None:
                return _blocked(None, "result_apply_not_requested")
            if head.active_operation_id != selected_id:
                return _blocked(selected_id, "active_result_operation_mismatch")
            operation = uow.results.get_operation(
                selected_id, workspace_id, for_update=True
            )
            if not isinstance(operation, ResultApplyOperationV1):
                return _blocked(selected_id, "result_apply_operation_missing")
            if operation.root_task_id != root_task_id:
                return _blocked(operation.id, "result_apply_scope_mismatch")
            if head.current_acceptance_id != operation.acceptance_id:
                return _blocked(operation.id, "result_apply_acceptance_mismatch")
            if head.current_candidate_id != operation.candidate_id:
                return _blocked(operation.id, "result_apply_candidate_mismatch")
            acceptance = uow.results.get_record(
                "acceptance", operation.acceptance_id, workspace_id
            )
            candidate = uow.results.get_record(
                "candidate", operation.candidate_id, workspace_id
            )
            if not isinstance(acceptance, TaskAcceptanceDecisionV1):
                return _blocked(operation.id, "result_apply_acceptance_missing")
            if not isinstance(candidate, IntegrationCandidateV1):
                return _blocked(operation.id, "result_apply_candidate_missing")
            if (
                acceptance.root_task_id != root_task_id
                or acceptance.candidate_id != candidate.id
                or candidate.root_task_id != root_task_id
            ):
                return _blocked(operation.id, "result_apply_scope_mismatch")
            if operation.status in {
                ResultOperationStatus.BLOCKED,
                ResultOperationStatus.FAILED,
            }:
                return _blocked(
                    operation.id,
                    operation.failure_code or "result_apply_operation_failed",
                )
            if operation.status is ResultOperationStatus.SUCCEEDED:
                return _PreparedApply(operation)
            if operation.status is ResultOperationStatus.UNKNOWN:
                return _PreparedApply(operation)

            now = _aware_now(self._clock())
            if (
                _lease_is_active(operation, now)
                and operation.lease_owner != self._owner_id
            ):
                return _pending(operation.id, "result_operation_in_progress")
            if _lease_is_active(operation, now):
                return _PreparedApply(operation)

            claimed = (
                replace(operation, version=operation.version + 1)
                if operation.status is ResultOperationStatus.RUNNING
                else transition_operation(operation, ResultOperationStatus.RUNNING)
            )
            claimed = replace(
                claimed,
                lease_owner=self._owner_id,
                lease_token=str(uuid4()),
                lease_expiry=now + self._lease_ttl,
            )
            stored = uow.results.save_operation(
                claimed, expected_version=operation.version
            )
            if not isinstance(stored, ResultApplyOperationV1):
                return _blocked(operation.id, "result_apply_operation_conflict")
            return _PreparedApply(stored)

    def _record_receipt(
        self,
        operation: ResultApplyOperationV1,
        receipt: ApplyReceipt,
    ) -> ResultApplyOutcome:
        _validate_receipt(operation, receipt)
        with self._uow_factory() as uow:
            task = uow.tasks.get(
                operation.root_task_id, operation.workspace_id, for_update=True
            )
            if task is None:
                return _blocked(operation.id, "root_task_missing")
            head = uow.results.get_head(
                operation.workspace_id, operation.root_task_id, for_update=True
            )
            if head is None:
                return _blocked(operation.id, "result_head_missing")
            acceptance = uow.results.get_record(
                "acceptance", operation.acceptance_id, operation.workspace_id
            )
            if not isinstance(acceptance, TaskAcceptanceDecisionV1):
                return _blocked(operation.id, "result_apply_acceptance_missing")
            current = uow.results.get_operation(
                operation.id, operation.workspace_id, for_update=True
            )
            if not isinstance(current, ResultApplyOperationV1):
                return _blocked(operation.id, "result_apply_operation_missing")
            if current.status is ResultOperationStatus.SUCCEEDED:
                if current.receipt_ref != receipt.receipt_ref:
                    return _blocked(operation.id, "result_apply_receipt_conflict")
                return self._finish_succeeded_in_uow(uow, task, head, current)
            if not _same_claim(current, operation):
                return _pending(operation.id, "result_operation_lease_lost")
            if head.active_operation_id != current.id:
                return _blocked(current.id, "active_result_operation_mismatch")

            succeeded = transition_operation(current, ResultOperationStatus.SUCCEEDED)
            succeeded = replace(
                succeeded,
                lease_owner=None,
                lease_token=None,
                lease_expiry=None,
                receipt_ref=receipt.receipt_ref,
                failure_code=None,
            )
            stored = uow.results.save_operation(
                succeeded, expected_version=current.version
            )
            if not isinstance(stored, ResultApplyOperationV1):
                return _blocked(current.id, "result_apply_operation_conflict")
            uow.events.append_with_outbox(
                _operation_event(
                    stored,
                    event_type="ResultApplyReceiptRecorded",
                    payload={
                        "operation_id": str(stored.id),
                        "root_task_id": str(stored.root_task_id),
                        "acceptance_id": str(stored.acceptance_id),
                        "candidate_id": str(stored.candidate_id),
                        "repository_ref": stored.repository_ref,
                        "target_ref": stored.target_ref,
                        "expected_old_commit": stored.expected_old_commit,
                        "accepted_commit": stored.accepted_commit,
                        "receipt_ref": receipt.receipt_ref,
                        "status": stored.status.value,
                    },
                    causation_id=acceptance.command_id,
                )
            )
            return self._finish_succeeded_in_uow(uow, task, head, stored)

    def _finish_succeeded(
        self, operation: ResultApplyOperationV1
    ) -> ResultApplyOutcome:
        with self._uow_factory() as uow:
            task = uow.tasks.get(
                operation.root_task_id, operation.workspace_id, for_update=True
            )
            if task is None:
                return _blocked(operation.id, "root_task_missing")
            head = uow.results.get_head(
                operation.workspace_id, operation.root_task_id, for_update=True
            )
            if head is None:
                return _blocked(operation.id, "result_head_missing")
            acceptance = uow.results.get_record(
                "acceptance", operation.acceptance_id, operation.workspace_id
            )
            if not isinstance(acceptance, TaskAcceptanceDecisionV1):
                return _blocked(operation.id, "result_apply_acceptance_missing")
            current = uow.results.get_operation(
                operation.id, operation.workspace_id, for_update=True
            )
            if not isinstance(current, ResultApplyOperationV1):
                return _blocked(operation.id, "result_apply_operation_missing")
            if current.status is not ResultOperationStatus.SUCCEEDED:
                return _blocked(operation.id, "result_apply_receipt_missing")
            return self._finish_succeeded_in_uow(uow, task, head, current)

    def _finish_succeeded_in_uow(
        self,
        uow: ControlPlaneUnitOfWork,
        task: object,
        head: TaskResultHead,
        operation: ResultApplyOperationV1,
    ) -> ResultApplyOutcome:
        if head.active_operation_id == operation.id:
            next_head = replace(
                head,
                active_operation_id=None,
                result_version=head.result_version + 1,
            )
            uow.results.save_head(next_head, expected_version=head.result_version)
            head = next_head
        elif head.active_operation_id is not None:
            return _blocked(operation.id, "active_result_operation_mismatch")
        finalization = self._finalizer.reconcile_in_uow(
            uow,
            operation.workspace_id,
            operation.root_task_id,
        )
        return _from_finalization(operation, finalization)

    def _mark_external_failure(
        self, operation: ResultApplyOperationV1, error: ResultError
    ) -> ResultApplyOutcome:
        target = (
            ResultOperationStatus.UNKNOWN
            if error.code in _UNCERTAIN_FAILURE_CODES
            else ResultOperationStatus.BLOCKED
        )
        return self._record_failure(operation, target, error.code)

    def _block_unknown(self, operation: ResultApplyOperationV1) -> ResultApplyOutcome:
        return self._record_failure(
            operation,
            ResultOperationStatus.UNKNOWN,
            operation.failure_code or "result_operation_inspect_required",
        )

    def _record_failure(
        self,
        operation: ResultApplyOperationV1,
        status: ResultOperationStatus,
        failure_code: str,
    ) -> ResultApplyOutcome:
        with self._uow_factory() as uow:
            task = uow.tasks.get(
                operation.root_task_id, operation.workspace_id, for_update=True
            )
            if task is None:
                return _blocked(operation.id, "root_task_missing")
            head = uow.results.get_head(
                operation.workspace_id, operation.root_task_id, for_update=True
            )
            if head is None:
                return _blocked(operation.id, "result_head_missing")
            acceptance = uow.results.get_record(
                "acceptance", operation.acceptance_id, operation.workspace_id
            )
            if not isinstance(acceptance, TaskAcceptanceDecisionV1):
                return _blocked(operation.id, "result_apply_acceptance_missing")
            current = uow.results.get_operation(
                operation.id, operation.workspace_id, for_update=True
            )
            if not isinstance(current, ResultApplyOperationV1):
                return _blocked(operation.id, "result_apply_operation_missing")
            if (
                current.status is ResultOperationStatus.UNKNOWN
                and status is ResultOperationStatus.UNKNOWN
            ):
                return _blocked(current.id, failure_code)
            if not _same_claim(current, operation):
                return _pending(operation.id, "result_operation_lease_lost")
            updated = transition_operation(current, status)
            updated = replace(
                updated,
                lease_owner=None,
                lease_token=None,
                lease_expiry=None,
                failure_code=failure_code,
            )
            stored = uow.results.save_operation(
                updated, expected_version=current.version
            )
            if not isinstance(stored, ResultApplyOperationV1):
                return _blocked(current.id, "result_apply_operation_conflict")
            uow.events.append_with_outbox(
                _operation_event(
                    stored,
                    event_type=(
                        "ResultApplyUnknown"
                        if status is ResultOperationStatus.UNKNOWN
                        else "ResultApplyBlocked"
                    ),
                    payload={
                        "operation_id": str(stored.id),
                        "root_task_id": str(stored.root_task_id),
                        "acceptance_id": str(stored.acceptance_id),
                        "candidate_id": str(stored.candidate_id),
                        "status": stored.status.value,
                        "failure_code": failure_code,
                    },
                    causation_id=acceptance.command_id,
                )
            )
            return _blocked(stored.id, failure_code)

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)


def _validate_receipt(operation: ResultApplyOperationV1, receipt: ApplyReceipt) -> None:
    if not isinstance(receipt, ApplyReceipt):
        raise ResultError("result_apply_receipt_invalid", {})
    checks = (
        (receipt.operation_id, operation.id, "operation_id"),
        (receipt.workspace_id, operation.workspace_id, "workspace_id"),
        (receipt.root_task_id, operation.root_task_id, "root_task_id"),
        (receipt.acceptance_id, operation.acceptance_id, "acceptance_id"),
        (receipt.candidate_id, operation.candidate_id, "candidate_id"),
        (receipt.repository_ref, operation.repository_ref, "repository_ref"),
        (receipt.target_ref, operation.target_ref, "target_ref"),
        (receipt.old_commit, operation.expected_old_commit, "old_commit"),
        (receipt.new_commit, operation.accepted_commit, "new_commit"),
    )
    if any(actual != expected for actual, expected, _label in checks):
        raise ResultError(
            "result_apply_receipt_identity_changed",
            {"operation_id": str(operation.id)},
        )
    if receipt.outcome is not ApplyOutcome.SUCCEEDED:
        raise ResultError("result_apply_receipt_not_successful", {})


def _same_claim(
    current: ResultApplyOperationV1, claimed: ResultApplyOperationV1
) -> bool:
    if current.version != claimed.version or current.status is not claimed.status:
        return False
    if current.status is ResultOperationStatus.UNKNOWN:
        return current.lease_owner is None and current.lease_token is None
    return (
        current.lease_owner == claimed.lease_owner
        and current.lease_token == claimed.lease_token
        and current.lease_expiry == claimed.lease_expiry
    )


def _lease_is_active(operation: ResultApplyOperationV1, now: datetime) -> bool:
    return (
        operation.lease_owner is not None
        and operation.lease_token is not None
        and operation.lease_expiry is not None
        and operation.lease_expiry > now
    )


def _from_finalization(
    operation: ResultApplyOperationV1, outcome: FinalizationOutcome
) -> ResultApplyOutcome:
    return ResultApplyOutcome(
        state=outcome.state,
        operation_id=operation.id,
        failure_code=outcome.failure_code,
        receipt_ref=operation.receipt_ref,
    )


def _operation_event(
    operation: ResultApplyOperationV1,
    *,
    event_type: str,
    payload: dict[str, object],
    causation_id: UUID,
) -> EventData:
    return EventData(
        id=uuid5(
            _EVENT_NAMESPACE,
            f"{event_type}:{operation.id}:v{operation.version}",
        ),
        workspace_id=operation.workspace_id,
        aggregate_type="result_apply_operation",
        aggregate_id=operation.id,
        aggregate_version=operation.version,
        event_type=event_type,
        payload=payload,
        actor_type="system",
        actor_id="result-applier",
        causation_id=causation_id,
        correlation_id=operation.root_task_id,
    )


def _blocked(operation_id: UUID | None, failure_code: str) -> ResultApplyOutcome:
    if operation_id is None:
        operation_id = UUID("00000000-0000-0000-0000-000000000000")
    return ResultApplyOutcome(
        state=ResultApplyState.BLOCKED.value,
        operation_id=operation_id,
        failure_code=failure_code,
    )


def _pending(operation_id: UUID, failure_code: str) -> ResultApplyOutcome:
    return ResultApplyOutcome(
        state=ResultApplyState.PENDING.value,
        operation_id=operation_id,
        failure_code=failure_code,
    )


def _require_uuid(value: object, label: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{label} must be a UUID")
    return value


def _aware_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("result apply clock must return an aware datetime")
    return value.astimezone(UTC)


__all__ = [
    "ResultApplyCrash",
    "ResultApplyOutcome",
    "ResultApplyService",
    "ResultApplyState",
]
