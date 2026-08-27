from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError
from sidera_control_plane.domain.modules import (
    ModuleLifecycle,
    WorkerAdapterCapabilityProfile,
)


class GatewayOperationType(StrEnum):
    PREPARE_WORKSPACE = "prepare_workspace"
    START_SESSION = "start_session"
    SEND_INPUT = "send_input"
    APPLY_CONTEXT_PATCH = "apply_context_patch"
    STOP_SESSION = "stop_session"
    COLLECT_ARTIFACTS = "collect_artifacts"
    CLEANUP_WORKSPACE = "cleanup_workspace"


class GatewayOperationStatus(StrEnum):
    REQUESTED = "requested"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DeliveryOutcome(StrEnum):
    DELIVERED = "delivered"
    BLOCKED = "blocked"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RuntimeModuleOperation(StrEnum):
    START = "start"
    RESUME = "resume"
    INPUT = "input"
    PATCH = "patch"
    OBSERVE = "observe"
    STOP = "stop"
    CLEANUP = "cleanup"


def runtime_module_operation_allowed(
    lifecycle: ModuleLifecycle | str,
    operation: RuntimeModuleOperation | str,
) -> bool:
    current = (
        lifecycle
        if isinstance(lifecycle, ModuleLifecycle)
        else ModuleLifecycle(lifecycle)
    )
    requested = (
        operation
        if isinstance(operation, RuntimeModuleOperation)
        else RuntimeModuleOperation(operation)
    )
    if current is not ModuleLifecycle.DISABLED:
        return True
    return requested in {
        RuntimeModuleOperation.OBSERVE,
        RuntimeModuleOperation.STOP,
        RuntimeModuleOperation.CLEANUP,
    }


@dataclass(slots=True)
class GatewayOperation:
    id: UUID
    operation_id: UUID
    workspace_id: UUID
    task_id: UUID
    execution_id: UUID
    session_id: UUID | None
    operation_type: GatewayOperationType
    module_instance_id: UUID | None
    config_version: int | None
    lease_id: UUID
    fencing_token: int
    status: GatewayOperationStatus
    attempt_count: int
    next_attempt_at: datetime | None
    result_ref: str | None
    failure_code: str | None
    version: int

    @classmethod
    def create(
        cls,
        *,
        id: UUID,
        operation_id: UUID,
        workspace_id: UUID,
        task_id: UUID,
        execution_id: UUID,
        session_id: UUID | None,
        operation_type: GatewayOperationType,
        module_instance_id: UUID | None,
        config_version: int | None,
        lease_id: UUID,
        fencing_token: int,
        now: datetime,
    ) -> "GatewayOperation":
        if fencing_token < 1:
            raise ValidationError("fencing_token must be positive")
        if config_version is not None and config_version < 1:
            raise ValidationError("config_version must be positive")
        if now.tzinfo is None:
            raise ValidationError("operation timestamp must be timezone-aware")
        return cls(
            id=id,
            operation_id=operation_id,
            workspace_id=workspace_id,
            task_id=task_id,
            execution_id=execution_id,
            session_id=session_id,
            operation_type=_operation_type(operation_type),
            module_instance_id=module_instance_id,
            config_version=config_version,
            lease_id=lease_id,
            fencing_token=fencing_token,
            status=GatewayOperationStatus.REQUESTED,
            attempt_count=0,
            next_attempt_at=now,
            result_ref=None,
            failure_code=None,
            version=1,
        )

    @property
    def idempotency_key(self) -> tuple[UUID, GatewayOperationType]:
        return self.operation_id, self.operation_type

    def start(self, *, now: datetime) -> None:
        if self.status is not GatewayOperationStatus.REQUESTED:
            raise InvalidTransition("only a requested operation can start")
        self.status = GatewayOperationStatus.RUNNING
        if self.attempt_count == 0:
            self.attempt_count = 1
        self.next_attempt_at = None
        self.version += 1

    def succeed(self, *, result_ref: str, now: datetime) -> None:
        self._finish(GatewayOperationStatus.SUCCEEDED, result_ref, None)

    def fail(self, *, failure_code: str, now: datetime) -> None:
        clean_code = failure_code.strip()
        if not clean_code:
            raise ValidationError("failure_code is required")
        self._finish(GatewayOperationStatus.FAILED, None, clean_code)

    def mark_unknown(self, *, failure_code: str, now: datetime) -> None:
        clean_code = failure_code.strip()
        if not clean_code:
            raise ValidationError("failure_code is required")
        self._finish(GatewayOperationStatus.UNKNOWN, None, clean_code)

    def retry(self, *, now: datetime) -> None:
        if self.status not in {
            GatewayOperationStatus.FAILED,
            GatewayOperationStatus.UNKNOWN,
        }:
            raise InvalidTransition("only failed or unknown operations can retry")
        self.status = GatewayOperationStatus.REQUESTED
        self.attempt_count += 1
        self.next_attempt_at = now
        self.result_ref = None
        self.failure_code = None
        self.version += 1

    def _finish(
        self,
        status: GatewayOperationStatus,
        result_ref: str | None,
        failure_code: str | None,
    ) -> None:
        if self.status is not GatewayOperationStatus.RUNNING:
            raise InvalidTransition("only a running operation can finish")
        if status is GatewayOperationStatus.SUCCEEDED and not result_ref:
            raise ValidationError("successful operation requires result_ref")
        self.status = status
        self.result_ref = result_ref
        self.failure_code = failure_code
        self.version += 1


@dataclass(frozen=True, slots=True)
class AdapterSessionHandle:
    execution_session_id: UUID
    adapter_session_id: str
    process_ref: str
    capability_snapshot: WorkerAdapterCapabilityProfile
    last_accepted_sequence: int
    started_at: datetime

    def __post_init__(self) -> None:
        if not self.adapter_session_id.strip() or not self.process_ref.strip():
            raise ValidationError("adapter and process references are required")
        if self.last_accepted_sequence < 0:
            raise ValidationError("last_accepted_sequence cannot be negative")
        if self.started_at.tzinfo is None:
            raise ValidationError("session start time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AdapterEvent:
    event_type: str
    sequence: int
    payload: Mapping[str, Any]
    occurred_at: datetime
    raw_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.event_type.strip() or self.sequence < 1:
            raise ValidationError("adapter event type and sequence are required")
        if self.occurred_at.tzinfo is None:
            raise ValidationError("adapter event time must be timezone-aware")
        if not isinstance(self.payload, Mapping):
            raise ValidationError("adapter event payload must be a mapping")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    session_id: UUID
    sequence: int
    event_type: str
    payload: Mapping[str, Any]
    occurred_at: datetime
    raw_ref: str | None

    def __post_init__(self) -> None:
        if self.sequence < 1 or not self.event_type.strip():
            raise ValidationError("execution event sequence and type are required")
        if self.occurred_at.tzinfo is None:
            raise ValidationError("execution event time must be timezone-aware")
        if not isinstance(self.payload, Mapping):
            raise ValidationError("execution event payload must be a mapping")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    operation_id: UUID
    outcome: DeliveryOutcome
    accepted_sequence: int | None
    failure_code: str | None

    def __post_init__(self) -> None:
        try:
            outcome = (
                self.outcome
                if isinstance(self.outcome, DeliveryOutcome)
                else DeliveryOutcome(self.outcome)
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"unsupported delivery outcome: {self.outcome!r}") from exc
        object.__setattr__(self, "outcome", outcome)
        if self.accepted_sequence is not None and self.accepted_sequence < 0:
            raise ValidationError("accepted_sequence cannot be negative")
        if outcome is DeliveryOutcome.DELIVERED and self.accepted_sequence is None:
            raise ValidationError("delivered input requires accepted_sequence")


def _operation_type(value: GatewayOperationType | str) -> GatewayOperationType:
    try:
        return value if isinstance(value, GatewayOperationType) else GatewayOperationType(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported gateway operation type: {value!r}") from exc


def gateway_operation_id(
    execution_id: UUID, operation_type: GatewayOperationType
) -> UUID:
    return uuid5(NAMESPACE_URL, f"sidera.gateway:{operation_type.value}:{execution_id}")
