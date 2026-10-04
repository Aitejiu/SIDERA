from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.domain.context_packages import ContextAcknowledgement
from sidera_control_plane.domain.errors import InvalidTransition, ValidationError
from sidera_control_plane.domain.handoffs import HandoffPhase
from sidera_control_plane.domain.modules import (
    ModuleLifecycle,
    WorkerAdapterCapabilityProfile,
)

if TYPE_CHECKING:
    from sidera_control_plane.domain.handoff_evidence import SessionContextBinding


class GatewayOperationType(StrEnum):
    PREPARE_WORKSPACE = "prepare_workspace"
    START_SESSION = "start_session"
    SEND_INPUT = "send_input"
    APPLY_CONTEXT_PATCH = "apply_context_patch"
    STOP_SESSION = "stop_session"
    COLLECT_ARTIFACTS = "collect_artifacts"
    CLEANUP_WORKSPACE = "cleanup_workspace"
    HANDOFF_CHECKPOINT = "handoff_checkpoint"
    HANDOFF_ENABLE_WRITER = "handoff_enable_writer"


class GatewayOperationStatus(StrEnum):
    REQUESTED = "requested"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


_HANDOFF_INSPECTION_RESULT_PREFIX = "handoff-inspection:v1:"
_HANDOFF_INSPECTION_OUTCOMES = frozenset(
    {"absent", "in_progress", "succeeded", "failed", "unknown"}
)


class DeliveryOutcome(StrEnum):
    ACCEPTED = "accepted"
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
    ) -> GatewayOperation:
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

    def resolve_stop(
        self,
        *,
        now: datetime,
        result_ref: str | None = None,
        failure_code: str | None = None,
    ) -> None:
        if (
            self.operation_type is not GatewayOperationType.STOP_SESSION
            or self.status is not GatewayOperationStatus.UNKNOWN
        ):
            raise InvalidTransition(
                "only an unknown Stop can resolve its original outcome"
            )
        if (
            (result_ref is None) == (failure_code is None)
            or (
                result_ref is not None
                and (not isinstance(result_ref, str) or not result_ref.strip())
            )
            or (
                failure_code is not None
                and (not isinstance(failure_code, str) or not failure_code.strip())
            )
        ):
            raise ValidationError(
                "Stop resolution requires exactly one non-empty terminal outcome"
            )
        self.status = (
            GatewayOperationStatus.SUCCEEDED
            if result_ref is not None
            else GatewayOperationStatus.FAILED
        )
        self.result_ref = None if result_ref is None else result_ref.strip()
        self.failure_code = None if failure_code is None else failure_code.strip()
        self.version += 1

    def record_handoff_inspection(self, *, outcome: str) -> None:
        """Persist bounded provider inspection without resolving the operation."""

        if self.status is not GatewayOperationStatus.UNKNOWN:
            raise InvalidTransition(
                "handoff inspection can only be recorded for an unknown operation"
            )
        clean_outcome = outcome.strip()
        if clean_outcome not in _HANDOFF_INSPECTION_OUTCOMES:
            raise ValidationError("handoff inspection outcome is invalid")
        marker = f"{_HANDOFF_INSPECTION_RESULT_PREFIX}{clean_outcome}"
        if self.result_ref == marker:
            return
        if self.result_ref is not None:
            raise InvalidTransition("handoff inspection result is already recorded")
        self.result_ref = marker
        self.version += 1

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


def handoff_inspection_outcome_from_result_ref(result_ref: str | None) -> str | None:
    if not isinstance(result_ref, str) or not result_ref.startswith(
        _HANDOFF_INSPECTION_RESULT_PREFIX
    ):
        return None
    outcome = result_ref.removeprefix(_HANDOFF_INSPECTION_RESULT_PREFIX)
    return outcome if outcome in _HANDOFF_INSPECTION_OUTCOMES else None


@dataclass(frozen=True, slots=True)
class AdapterSessionHandle:
    execution_session_id: UUID
    adapter_session_id: str
    process_ref: str
    capability_snapshot: WorkerAdapterCapabilityProfile
    last_accepted_sequence: int
    started_at: datetime
    context_binding: SessionContextBinding | None = None

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
class NativeInputIdentity:
    workspace_id: UUID
    input_id: UUID
    execution_id: UUID
    session_id: UUID
    run_id: UUID
    operation_id: UUID
    authority_id: UUID
    binding_version: int

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "input_id",
            "execution_id",
            "session_id",
            "run_id",
            "operation_id",
            "authority_id",
        ):
            if not isinstance(getattr(self, name), UUID):
                raise ValidationError("native input identity must contain UUIDs")
        if type(self.binding_version) is not int or self.binding_version < 1:
            raise ValidationError("native input binding version must be positive")

    def result_ref(self, *, evidence_sequence: int | None = None) -> str:
        if evidence_sequence is not None and (
            type(evidence_sequence) is not int or evidence_sequence < 1
        ):
            raise ValidationError("native input delivery proof must be positive")
        names = (
            "workspace_id",
            "input_id",
            "execution_id",
            "session_id",
            "run_id",
            "operation_id",
            "authority_id",
            "binding_version",
        )
        payload = {
            name: value if name == "binding_version" else str(value)
            for name in names
            for value in (getattr(self, name),)
        }
        if evidence_sequence is None:
            return "native-input:v1:" + json.dumps(payload, sort_keys=True)
        return "native-input-delivery:v1:" + json.dumps(
            {"identity": payload, "evidence_sequence": evidence_sequence},
            sort_keys=True,
        )

    @classmethod
    def from_result_ref(cls, value: str | None) -> NativeInputIdentity:
        return cls._decode_result_ref(value)[0]

    @classmethod
    def delivery_sequence_from_result_ref(cls, value: str | None) -> int | None:
        return cls._decode_result_ref(value)[1]

    @classmethod
    def _decode_result_ref(cls, value):
        prefixes = ("native-input:v1:", "native-input-delivery:v1:")
        if not isinstance(value, str) or not value.startswith(prefixes):
            raise ValidationError("original native input identity is missing")
        try:
            delivered = value.startswith(prefixes[1])
            payload = json.loads(
                value.removeprefix(prefixes[int(delivered)]),
                object_pairs_hook=_native_input_fields,
            )
            sequence = None
            if delivered:
                if set(payload) != {"identity", "evidence_sequence"}:
                    raise ValueError("invalid native delivery shape")
                sequence = payload["evidence_sequence"]
                if type(sequence) is not int or sequence < 1:
                    raise ValueError("invalid native delivery proof")
                payload = payload["identity"]
            if set(payload) != {
                "workspace_id",
                "input_id",
                "execution_id",
                "session_id",
                "run_id",
                "operation_id",
                "authority_id",
                "binding_version",
            }:
                raise ValueError("invalid native input identity shape")
            identity = cls(
                workspace_id=UUID(payload["workspace_id"]),
                input_id=UUID(payload["input_id"]),
                execution_id=UUID(payload["execution_id"]),
                session_id=UUID(payload["session_id"]),
                run_id=UUID(payload["run_id"]),
                operation_id=UUID(payload["operation_id"]),
                authority_id=UUID(payload["authority_id"]),
                binding_version=payload["binding_version"],
            )
            return identity, sequence
        except (TypeError, ValueError, AttributeError):
            raise ValidationError(
                "original native input identity is malformed"
            ) from None


def _native_input_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("duplicate native input field")
    return fields


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    operation_id: UUID
    outcome: DeliveryOutcome
    accepted_sequence: int | None
    failure_code: str | None
    execution_session_id: UUID | None = None
    native_identity: NativeInputIdentity | None = None

    def __post_init__(self) -> None:
        try:
            outcome = (
                self.outcome
                if isinstance(self.outcome, DeliveryOutcome)
                else DeliveryOutcome(self.outcome)
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"unsupported delivery outcome: {self.outcome!r}"
            ) from exc
        object.__setattr__(self, "outcome", outcome)
        if self.execution_session_id is not None and not isinstance(
            self.execution_session_id, UUID
        ):
            raise ValidationError("execution_session_id must be a UUID")
        if self.accepted_sequence is not None and self.accepted_sequence < 0:
            raise ValidationError("accepted_sequence cannot be negative")
        if outcome is DeliveryOutcome.DELIVERED and self.accepted_sequence is None:
            raise ValidationError("delivered input requires accepted_sequence")


def bind_context_acknowledgement(
    acknowledgement: ContextAcknowledgement,
    *,
    adapter_module_id: UUID,
    adapter_config_version: int,
) -> ContextAcknowledgement:
    """Bind acknowledgement ownership to the exact runtime snapshot."""

    if not isinstance(adapter_module_id, UUID):
        raise ValidationError("adapter_module_id must be a UUID")
    if (
        not isinstance(adapter_config_version, int)
        or isinstance(adapter_config_version, bool)
        or adapter_config_version < 1
    ):
        raise ValidationError("adapter_config_version must be positive")
    if acknowledgement.adapter_config_version != adapter_config_version:
        raise ValidationError("context acknowledgement adapter config is stale")
    return replace(
        acknowledgement,
        adapter_module_id=adapter_module_id,
        adapter_config_version=adapter_config_version,
    )


def _operation_type(value: GatewayOperationType | str) -> GatewayOperationType:
    try:
        return (
            value
            if isinstance(value, GatewayOperationType)
            else GatewayOperationType(value)
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported gateway operation type: {value!r}") from exc


def gateway_operation_id(
    execution_id: UUID, operation_type: GatewayOperationType
) -> UUID:
    return uuid5(NAMESPACE_URL, f"sidera.gateway:{operation_type.value}:{execution_id}")


HANDOFF_OPERATION_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "urn:sidera:handoff-operation:v1",
)
HANDOFF_STEP_ACTIONS = frozenset(
    {
        "checkpoint",
        "freeze_workspace",
        "transfer_workspace",
        "enable_writer",
        "stop_source",
    }
)


def handoff_step_operation_id(
    transition_id: UUID,
    phase: HandoffPhase,
    *,
    logical_attempt: int,
    action: str,
) -> UUID:
    if not isinstance(transition_id, UUID):
        raise ValidationError("transition_id must be a UUID")
    if not isinstance(phase, HandoffPhase):
        try:
            phase = HandoffPhase(phase)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"unsupported handoff phase: {phase!r}") from exc
    if (
        not isinstance(logical_attempt, int)
        or isinstance(logical_attempt, bool)
        or logical_attempt < 1
    ):
        raise ValidationError("logical_attempt must be positive")
    if action not in HANDOFF_STEP_ACTIONS:
        raise ValidationError("unsupported handoff step action")
    identity = f"handoff:v1:{transition_id}:{logical_attempt}:{phase.value}:{action}"
    return uuid5(HANDOFF_OPERATION_NAMESPACE, identity)


def context_patch_operation_id(
    worker_transition_id: UUID,
    action_step: str,
    *,
    logical_attempt: int,
) -> UUID:
    """Return the stable idempotency key for one transition patch step."""

    if not isinstance(worker_transition_id, UUID):
        raise ValidationError("worker_transition_id must be a UUID")
    step = action_step.strip()
    if not step:
        raise ValidationError("context patch action_step is required")
    if (
        not isinstance(logical_attempt, int)
        or isinstance(logical_attempt, bool)
        or logical_attempt < 1
    ):
        raise ValidationError("context patch logical_attempt must be positive")
    return uuid5(
        NAMESPACE_URL,
        f"sidera.gateway:context-patch:{worker_transition_id}:{step}:{logical_attempt}",
    )
