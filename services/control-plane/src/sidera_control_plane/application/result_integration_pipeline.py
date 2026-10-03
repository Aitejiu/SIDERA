from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.code_result_capture import (
    capture_operation_for_execution,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.result_integration import (
    ResultIntegrationService,
)
from sidera_control_plane.application.result_ports import ResultOperationAttemptV1
from sidera_control_plane.application.result_verification import (
    ResultVerificationService,
)
from sidera_control_plane.domain.code_results import CodeResultSnapshotV1
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.domain.result_integration import IntegrationSpecV1
from sidera_control_plane.domain.result_operations import (
    ResultError,
    ResultOperationStatus,
)

INTEGRATION_CANDIDATE_PUBLICATION_KIND = "integration_candidate_publication"


def integration_candidate_publication_operation_id(execution_id: UUID) -> UUID:
    if not isinstance(execution_id, UUID):
        raise TypeError("execution_id must be a UUID")
    return uuid5(
        NAMESPACE_URL,
        f"sidera.integration-candidate-publication:{execution_id}",
    )


def verification_schedule_command_id(candidate_id: UUID) -> UUID:
    if not isinstance(candidate_id, UUID):
        raise TypeError("candidate_id must be a UUID")
    return uuid5(
        NAMESPACE_URL,
        f"sidera.integration-verification-schedule:{candidate_id}",
    )


def enqueue_integration_candidate_publication(
    uow: ControlPlaneUnitOfWork,
    execution: Execution,
    *,
    now: datetime,
) -> UUID | None:
    """Durably queue only the active plan's declared integration WorkerItem."""
    if execution.status is not ExecutionStatus.SUCCEEDED:
        return None
    if execution.worker_item_id is None:
        return None
    request = uow.execution_requests.get_by_execution_lineage(
        execution.lineage_id, execution.workspace_id, for_update=True
    )
    if request is None:
        return None
    # A direct rework execution produces a replacement source result. It must
    # wait for an explicit ReintegrateTaskResult command before becoming a new
    # integration candidate. Verification executions are also result inputs,
    # not candidate publishers.
    if request.result_derivation_kind is not None and request.purpose != "reintegrate":
        return None
    plan_head = uow.plans.get_head(request.root_task_id, execution.workspace_id)
    if plan_head is None or plan_head.active_snapshot_id != request.plan_snapshot_id:
        return None
    plan = uow.plans.get(plan_head.active_snapshot_id, execution.workspace_id)
    if plan is None or not isinstance(plan.integration_spec, IntegrationSpecV1):
        return None
    if request.worker_item_key != plan.integration_spec.integration_worker_item_key:
        return None

    snapshot = uow.results.get_code_result_by_capture_operation(
        capture_operation_for_execution(
            uow, workspace_id=execution.workspace_id, execution_id=execution.id
        ),
        execution.workspace_id,
    )
    if (
        not isinstance(snapshot, CodeResultSnapshotV1)
        or snapshot.execution_id != execution.id
        or snapshot.root_task_id != request.root_task_id
    ):
        raise ResultError(
            "integration_code_result_required",
            {"execution_id": str(execution.id)},
        )

    operation_id = integration_candidate_publication_operation_id(execution.id)
    payload = {
        "execution_id": str(execution.id),
        "plan_snapshot_id": str(plan.id),
        "worker_item_key": request.worker_item_key,
        "code_result_id": str(snapshot.id),
    }
    existing = uow.results.get_operation(
        operation_id, execution.workspace_id, for_update=True
    )
    if existing is not None:
        if (
            not isinstance(existing, ResultOperationAttemptV1)
            or existing.operation_kind != INTEGRATION_CANDIDATE_PUBLICATION_KIND
            or existing.root_task_id != request.root_task_id
            or dict(existing.payload) != payload
        ):
            raise ResultError(
                "integration_candidate_publication_conflict",
                {"operation_id": str(operation_id)},
            )
        return operation_id

    timestamp = _aware_now(now)
    operation = ResultOperationAttemptV1(
        operation_id=operation_id,
        workspace_id=execution.workspace_id,
        root_task_id=request.root_task_id,
        operation_kind=INTEGRATION_CANDIDATE_PUBLICATION_KIND,
        attempt=1,
        status=ResultOperationStatus.PENDING,
        payload=payload,
        lease_owner=None,
        lease_token=None,
        lease_expiry=None,
        receipt_ref=None,
        failure_code=None,
        version=1,
        created_at=timestamp,
        updated_at=timestamp,
    )
    uow.results.save_operation(operation, expected_version=0)
    return operation_id


class IntegrationCandidatePublicationHandler:
    """Publish a completed integration result and schedule its required checks."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._integration = ResultIntegrationService(uow_factory, clock=clock)
        self._verification = ResultVerificationService(uow_factory, clock=clock)

    def __call__(self, claimed: ResultOperationAttemptV1) -> None:
        if claimed.operation_kind != INTEGRATION_CANDIDATE_PUBLICATION_KIND:
            raise ResultError("integration_publication_operation_kind_invalid", {})
        if claimed.status is ResultOperationStatus.SUCCEEDED:
            return
        if claimed.status is not ResultOperationStatus.RUNNING:
            raise ResultError("integration_publication_operation_not_claimed", {})
        if not claimed.lease_owner or not claimed.lease_token:
            raise ResultError("integration_publication_lease_missing", {})
        execution_id = _payload_uuid(claimed, "execution_id")

        candidate = self._integration.publish_candidate(
            workspace_id=claimed.workspace_id,
            execution_id=execution_id,
            operation_id=claimed.operation_id,
        )
        request_id = self._verification.schedule(
            workspace_id=claimed.workspace_id,
            candidate_id=candidate.id,
            command_id=verification_schedule_command_id(candidate.id),
        )

        now = _aware_now(self._clock())
        with self._uow_factory() as uow:
            operation = uow.results.get_operation(
                claimed.operation_id, claimed.workspace_id, for_update=True
            )
            if not isinstance(operation, ResultOperationAttemptV1):
                raise ResultError("integration_publication_operation_missing", {})
            if operation.status is ResultOperationStatus.SUCCEEDED:
                if (
                    operation.payload.get("candidate_id") != str(candidate.id)
                    or operation.payload.get("verification_request_id")
                    != str(request_id)
                ):
                    raise ResultError("integration_publication_receipt_conflict", {})
                return
            if (
                operation.status is not ResultOperationStatus.RUNNING
                or operation.lease_owner != claimed.lease_owner
                or operation.lease_token != claimed.lease_token
            ):
                raise ResultError("integration_publication_lease_lost", {})
            succeeded = replace(
                operation,
                status=ResultOperationStatus.SUCCEEDED,
                payload={
                    **dict(operation.payload),
                    "candidate_id": str(candidate.id),
                    "candidate_hash": candidate.content_hash,
                    "verification_request_id": str(request_id),
                },
                lease_owner=None,
                lease_token=None,
                lease_expiry=None,
                receipt_ref=f"candidate://{candidate.id}",
                failure_code=None,
                updated_at=now,
                version=operation.version + 1,
            )
            uow.results.save_operation(succeeded, expected_version=operation.version)


def _payload_uuid(operation: ResultOperationAttemptV1, key: str) -> UUID:
    value = operation.payload.get(key)
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ResultError("integration_publication_payload_invalid", {"field": key}) from exc


def _aware_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ResultError("integration_publication_clock_invalid", {})
    return value.astimezone(UTC)


__all__ = [
    "INTEGRATION_CANDIDATE_PUBLICATION_KIND",
    "IntegrationCandidatePublicationHandler",
    "enqueue_integration_candidate_publication",
    "integration_candidate_publication_operation_id",
    "verification_schedule_command_id",
]
