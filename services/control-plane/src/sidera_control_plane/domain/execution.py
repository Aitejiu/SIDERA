from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sidera_control_plane.domain.errors import (
    InvalidTransition,
    StaleFencingToken,
    ValidationError,
)
from sidera_control_plane.domain.modules import WorkerAdapterCapabilityProfile


class ExecutionStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    LOST = "lost"


TERMINAL_EXECUTION_STATUSES = {
    ExecutionStatus.SUCCEEDED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELED,
    ExecutionStatus.LOST,
}


class ExecutionSessionStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    LOST = "lost"


@dataclass(slots=True)
class Execution:
    id: UUID
    workspace_id: UUID
    task_id: UUID
    worker_item_id: UUID | None
    lineage_id: UUID
    attempt: int
    max_attempts: int
    purpose: str
    status: ExecutionStatus
    capability_requirements: frozenset[str]
    pinned_worker_id: UUID | None
    assigned_worker_id: UUID | None
    active_lease_id: UUID | None
    last_fencing_token: int
    not_before: datetime
    failure_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    version: int
    stop_requested: bool = False
    source_execution_id: UUID | None = None
    source_session_id: UUID | None = None
    handoff_transition_id: UUID | None = None
    adapter_preferences: tuple[str, ...] = ()
    adapter_forbidden: frozenset[str] = frozenset()
    created_at: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        execution_id: UUID,
        workspace_id: UUID,
        task_id: UUID,
        worker_item_id: UUID | None = None,
        lineage_id: UUID,
        attempt: int,
        max_attempts: int,
        purpose: str,
        capability_requirements: frozenset[str],
        pinned_worker_id: UUID | None,
        not_before: datetime,
        source_execution_id: UUID | None = None,
        source_session_id: UUID | None = None,
        handoff_transition_id: UUID | None = None,
        adapter_preferences: Iterable[str] = (),
        adapter_forbidden: Iterable[str] = (),
        initial_fencing_token: int = 0,
    ) -> "Execution":
        clean_purpose = purpose.strip()
        clean_requirements = frozenset(
            item.strip() for item in capability_requirements if item.strip()
        )
        if attempt < 1 or max_attempts < attempt:
            raise ValidationError(
                "attempt must be positive and cannot exceed max_attempts"
            )
        if not clean_purpose or not clean_requirements:
            raise ValidationError("purpose and capability_requirements are required")
        if (
            not isinstance(initial_fencing_token, int)
            or isinstance(initial_fencing_token, bool)
            or initial_fencing_token < 0
        ):
            raise ValidationError("initial_fencing_token must be non-negative")
        if not_before.tzinfo is None:
            raise ValidationError("not_before must be timezone-aware")
        for name, value in (
            ("source_execution_id", source_execution_id),
            ("source_session_id", source_session_id),
            ("handoff_transition_id", handoff_transition_id),
        ):
            if value is not None and not isinstance(value, UUID):
                raise ValidationError(f"{name} must be a UUID")
        if source_session_id is not None and source_execution_id is None:
            raise ValidationError("source_session_id requires source_execution_id")
        if handoff_transition_id is not None and (
            source_execution_id is None or source_session_id is None
        ):
            raise ValidationError(
                "handoff target requires source execution and session lineage"
            )
        preferences = _clean_policy_values(adapter_preferences, "adapter_preferences")
        forbidden = frozenset(
            _clean_policy_values(adapter_forbidden, "adapter_forbidden")
        )
        if set(preferences).intersection(forbidden):
            raise ValidationError(
                "adapter preferences cannot contain forbidden adapter types"
            )
        return cls(
            id=execution_id,
            workspace_id=workspace_id,
            task_id=task_id,
            worker_item_id=worker_item_id,
            lineage_id=lineage_id,
            attempt=attempt,
            max_attempts=max_attempts,
            purpose=clean_purpose,
            status=ExecutionStatus.QUEUED,
            capability_requirements=clean_requirements,
            pinned_worker_id=pinned_worker_id,
            assigned_worker_id=None,
            active_lease_id=None,
            last_fencing_token=initial_fencing_token,
            not_before=not_before,
            failure_code=None,
            started_at=None,
            finished_at=None,
            version=1,
            stop_requested=False,
            source_execution_id=source_execution_id,
            source_session_id=source_session_id,
            handoff_transition_id=handoff_transition_id,
            adapter_preferences=preferences,
            adapter_forbidden=forbidden,
        )

    def pin_worker(self, worker_id: UUID) -> None:
        self._require_queued_routing_mutation()
        if self.pinned_worker_id == worker_id:
            raise ValidationError("worker is already pinned")
        self.pinned_worker_id = worker_id
        self.version += 1

    def clear_worker_pin(self) -> None:
        self._require_queued_routing_mutation()
        if self.pinned_worker_id is None:
            raise ValidationError("execution has no worker pin")
        self.pinned_worker_id = None
        self.version += 1

    def _require_queued_routing_mutation(self) -> None:
        if self.status is not ExecutionStatus.QUEUED:
            raise InvalidTransition(
                "worker routing can only change for a queued execution"
            )

    def _assert_mutable(self) -> None:
        if self.status in TERMINAL_EXECUTION_STATUSES:
            raise InvalidTransition(f"execution is terminal: {self.status}")

    def assign(self, *, lease_id: UUID, worker_id: UUID, fencing_token: int) -> None:
        self._assert_mutable()
        if self.status is not ExecutionStatus.QUEUED:
            raise InvalidTransition("only a queued execution can be assigned")
        if fencing_token <= self.last_fencing_token:
            raise StaleFencingToken("fencing token must increase")
        self.status = ExecutionStatus.LEASED
        self.assigned_worker_id = worker_id
        self.active_lease_id = lease_id
        self.last_fencing_token = fencing_token
        self.version += 1

    def start(self, *, lease_id: UUID, fencing_token: int, now: datetime) -> None:
        self._assert_mutable()
        if self.status is not ExecutionStatus.LEASED:
            raise InvalidTransition("only a leased execution can start")
        self.assert_fence(lease_id=lease_id, fencing_token=fencing_token)
        self.status = ExecutionStatus.RUNNING
        self.started_at = now
        self.version += 1

    def succeed(self, *, lease_id: UUID, fencing_token: int, now: datetime) -> None:
        self._finish(
            status=ExecutionStatus.SUCCEEDED,
            lease_id=lease_id,
            fencing_token=fencing_token,
            failure_code=None,
            now=now,
        )

    def fail(
        self,
        *,
        lease_id: UUID,
        fencing_token: int,
        failure_code: str,
        now: datetime,
    ) -> None:
        clean_code = failure_code.strip()
        if not clean_code:
            raise ValidationError("failure_code is required")
        self._finish(
            status=ExecutionStatus.FAILED,
            lease_id=lease_id,
            fencing_token=fencing_token,
            failure_code=clean_code,
            now=now,
        )

    def fail_before_start(
        self,
        *,
        lease_id: UUID,
        fencing_token: int,
        failure_code: str,
        now: datetime,
    ) -> None:
        clean_code = failure_code.strip()
        if not clean_code:
            raise ValidationError("failure_code is required")
        self._assert_mutable()
        if self.status is not ExecutionStatus.LEASED or self.started_at is not None:
            raise InvalidTransition("only a leased execution can fail before start")
        self.assert_fence(lease_id=lease_id, fencing_token=fencing_token)
        self.status = ExecutionStatus.FAILED
        self.failure_code = clean_code
        self.finished_at = now
        self.active_lease_id = None
        self.version += 1

    def _finish(
        self,
        *,
        status: ExecutionStatus,
        lease_id: UUID,
        fencing_token: int,
        failure_code: str | None,
        now: datetime,
    ) -> None:
        self._assert_mutable()
        if self.status is not ExecutionStatus.RUNNING:
            raise InvalidTransition("only a running execution can finish")
        self.assert_fence(lease_id=lease_id, fencing_token=fencing_token)
        self.status = status
        self.failure_code = failure_code
        self.finished_at = now
        self.active_lease_id = None
        self.version += 1

    def cancel(self, *, now: datetime) -> None:
        self._assert_mutable()
        if self.status not in {
            ExecutionStatus.QUEUED,
            ExecutionStatus.LEASED,
            ExecutionStatus.RUNNING,
        }:
            raise InvalidTransition(f"execution cannot be canceled from {self.status}")
        self.status = ExecutionStatus.CANCELED
        self.finished_at = now
        self.active_lease_id = None
        self.version += 1

    def request_stop(self) -> None:
        self._assert_mutable()
        if self.status not in {ExecutionStatus.LEASED, ExecutionStatus.RUNNING}:
            raise InvalidTransition("only a leased or running execution can be stopped")
        if self.active_lease_id is None:
            raise InvalidTransition("stopping an execution requires an active lease")
        if self.stop_requested:
            raise InvalidTransition("execution stop has already been requested")
        self.stop_requested = True
        self.version += 1

    def mark_lost(
        self,
        *,
        lease_id: UUID,
        fencing_token: int,
        failure_code: str,
        now: datetime,
    ) -> None:
        self._assert_mutable()
        if self.status not in {ExecutionStatus.LEASED, ExecutionStatus.RUNNING}:
            raise InvalidTransition(f"execution cannot be lost from {self.status}")
        self.assert_fence(lease_id=lease_id, fencing_token=fencing_token)
        clean_code = failure_code.strip()
        if not clean_code:
            raise ValidationError("failure_code is required")
        self.status = ExecutionStatus.LOST
        self.failure_code = clean_code
        self.finished_at = now
        self.active_lease_id = None
        self.version += 1

    def assert_fence(self, *, lease_id: UUID, fencing_token: int) -> None:
        if self.active_lease_id != lease_id or self.last_fencing_token != fencing_token:
            raise StaleFencingToken("execution is owned by a different lease or token")


def _clean_policy_values(values: Iterable[str], label: str) -> tuple[str, ...]:
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValidationError(f"{label} must contain strings")
        item = value.strip()
        if item and item not in cleaned:
            cleaned.append(item)
    return tuple(cleaned)


@dataclass(slots=True)
class ExecutionSession:
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    worker_id: UUID
    adapter_session_id: str
    status: ExecutionSessionStatus
    last_sequence: int
    last_activity_at: datetime
    input_mode: str
    version: int
    adapter_module_id: UUID | None = None
    adapter_config_version: int | None = None
    workspace_module_id: UUID | None = None
    workspace_config_version: int | None = None
    capability_snapshot: WorkerAdapterCapabilityProfile | None = None
    process_ref: str | None = None
    workspace_ref: str | None = None

    @classmethod
    def start(
        cls,
        *,
        session_id: UUID,
        workspace_id: UUID,
        execution_id: UUID,
        worker_id: UUID,
        adapter_session_id: str,
        now: datetime,
        adapter_module_id: UUID | None = None,
        adapter_config_version: int | None = None,
        workspace_module_id: UUID | None = None,
        workspace_config_version: int | None = None,
        capability_snapshot: WorkerAdapterCapabilityProfile | None = None,
        process_ref: str | None = None,
        workspace_ref: str | None = None,
    ) -> "ExecutionSession":
        clean_adapter_session_id = adapter_session_id.strip()
        if not clean_adapter_session_id:
            raise ValidationError("adapter_session_id is required")
        return cls(
            id=session_id,
            workspace_id=workspace_id,
            execution_id=execution_id,
            worker_id=worker_id,
            adapter_session_id=clean_adapter_session_id,
            status=ExecutionSessionStatus.ACTIVE,
            last_sequence=0,
            last_activity_at=now,
            input_mode="unsupported",
            version=1,
            adapter_module_id=adapter_module_id,
            adapter_config_version=adapter_config_version,
            workspace_module_id=workspace_module_id,
            workspace_config_version=workspace_config_version,
            capability_snapshot=capability_snapshot,
            process_ref=process_ref,
            workspace_ref=workspace_ref,
        )

    def bind_runtime(
        self,
        *,
        adapter_module_id: UUID,
        adapter_config_version: int,
        workspace_module_id: UUID,
        workspace_config_version: int,
        capability_snapshot: WorkerAdapterCapabilityProfile,
        process_ref: str,
        workspace_ref: str,
    ) -> None:
        if self.status is not ExecutionSessionStatus.ACTIVE:
            raise InvalidTransition("only an active session can bind runtime facts")
        if adapter_config_version < 1 or workspace_config_version < 1:
            raise ValidationError("runtime config versions must be positive")
        if not process_ref.strip() or not workspace_ref.strip():
            raise ValidationError(
                "runtime process and workspace references are required"
            )
        self.adapter_module_id = adapter_module_id
        self.adapter_config_version = adapter_config_version
        self.workspace_module_id = workspace_module_id
        self.workspace_config_version = workspace_config_version
        self.capability_snapshot = capability_snapshot
        self.process_ref = process_ref.strip()
        self.workspace_ref = workspace_ref.strip()
        self.version += 1

    def record_activity(self, *, sequence: int, now: datetime) -> None:
        if self.status is not ExecutionSessionStatus.ACTIVE:
            raise InvalidTransition("session is not active")
        if sequence != self.last_sequence + 1:
            raise ValidationError("session sequence must increase by exactly one")
        self.last_sequence = sequence
        self.last_activity_at = now
        self.version += 1

    def close(self, *, lost: bool, now: datetime) -> None:
        if self.status is not ExecutionSessionStatus.ACTIVE:
            raise InvalidTransition("session is already terminal")
        self.status = (
            ExecutionSessionStatus.LOST if lost else ExecutionSessionStatus.CLOSED
        )
        self.last_activity_at = now
        self.version += 1
