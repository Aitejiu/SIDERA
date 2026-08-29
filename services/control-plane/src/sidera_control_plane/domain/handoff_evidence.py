from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.gateway import GatewayOperationType

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
_MAX_TEXT_LENGTH = 16_384
_MAX_MAP_ENTRIES = 128
_MAX_MAP_DEPTH = 8
_MAX_MAP_BYTES = 128 * 1024
_FORBIDDEN_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "credential",
    "authorization",
    "api_key",
    "private_trace",
    "trace",
    "environment",
    "env",
    "path",
    "body",
    "content",
    "markdown",
)


class HandoffInspectionOutcome(StrEnum):
    ABSENT = "absent"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class HandoffWorkspaceTransferStatus(StrEnum):
    REQUESTED = "requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class HandoffInputAction(StrEnum):
    PRESERVE_DELIVERED = "preserve_delivered"
    TRANSFER_QUEUED = "transfer_queued"
    EXCLUDE = "exclude"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class HandoffCheckpointEvidence:
    operation_id: UUID
    transition_id: UUID
    source_session_id: UUID
    accepted_sequence: int
    workspace_digest: str
    context_package_id: UUID
    context_package_hash: str
    context_version: int
    progress_summary_ref: str
    artifact_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    quiescent: bool
    produced_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in (
            "operation_id",
            "transition_id",
            "source_session_id",
            "context_package_id",
        ):
            _uuid(getattr(self, name), f"checkpoint {name}")
        _schema(self.schema_version, "checkpoint")
        _non_negative(self.accepted_sequence, "checkpoint accepted_sequence")
        _hash(self.workspace_digest, "checkpoint workspace_digest")
        _hash(self.context_package_hash, "checkpoint context_package_hash")
        _positive(self.context_version, "checkpoint context_version")
        object.__setattr__(
            self,
            "progress_summary_ref",
            _opaque_ref(self.progress_summary_ref, "progress_summary_ref"),
        )
        object.__setattr__(
            self,
            "artifact_refs",
            _opaque_refs(self.artifact_refs, "checkpoint artifact_refs"),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _opaque_refs(self.evidence_refs, "checkpoint evidence_refs"),
        )
        if self.quiescent is not True:
            raise ValidationError("checkpoint evidence must be quiescent")
        _aware(self.produced_at, "checkpoint produced_at")

    def to_mapping(self) -> dict[str, object]:
        return {
            "operation_id": str(self.operation_id),
            "transition_id": str(self.transition_id),
            "source_session_id": str(self.source_session_id),
            "accepted_sequence": self.accepted_sequence,
            "workspace_digest": self.workspace_digest,
            "context_package_id": str(self.context_package_id),
            "context_package_hash": self.context_package_hash,
            "context_version": self.context_version,
            "progress_summary_ref": self.progress_summary_ref,
            "artifact_refs": list(self.artifact_refs),
            "evidence_refs": list(self.evidence_refs),
            "quiescent": self.quiescent,
            "produced_at": self.produced_at.isoformat(),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class HandoffStopEvidence:
    operation_id: UUID
    source_session_id: UUID
    accepted_sequence: int
    stopped: bool
    produced_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.operation_id, "stop operation_id")
        _uuid(self.source_session_id, "stop source_session_id")
        _non_negative(self.accepted_sequence, "stop accepted_sequence")
        if self.stopped is not True:
            raise ValidationError("stop evidence must prove stopped=true")
        _aware(self.produced_at, "stop produced_at")

    def assert_matches(self, checkpoint: HandoffCheckpointEvidence) -> None:
        if not isinstance(checkpoint, HandoffCheckpointEvidence):
            raise ValidationError("stop evidence requires checkpoint evidence")
        if self.source_session_id != checkpoint.source_session_id:
            raise ValidationError("stop session does not match checkpoint session")
        if self.accepted_sequence != checkpoint.accepted_sequence:
            raise ValidationError("stop sequence does not match checkpoint sequence")

    def validate_against(self, checkpoint: HandoffCheckpointEvidence) -> None:
        self.assert_matches(checkpoint)

    def to_mapping(self) -> dict[str, object]:
        return {
            "operation_id": str(self.operation_id),
            "source_session_id": str(self.source_session_id),
            "accepted_sequence": self.accepted_sequence,
            "stopped": self.stopped,
            "produced_at": self.produced_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class HandoffOperationInspection:
    operation_id: UUID
    operation_type: GatewayOperationType
    outcome: HandoffInspectionOutcome
    execution_id: UUID | None
    session_id: UUID | None
    accepted_sequence: int | None
    context_binding: SessionContextBinding | None
    observed_at: datetime
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _uuid(self.operation_id, "operation inspection operation_id")
        object.__setattr__(
            self,
            "operation_type",
            _enum(self.operation_type, GatewayOperationType, "operation_type"),
        )
        object.__setattr__(
            self,
            "outcome",
            _enum(self.outcome, HandoffInspectionOutcome, "inspection outcome"),
        )
        _optional_uuid(self.execution_id, "operation inspection execution_id")
        _optional_uuid(self.session_id, "operation inspection session_id")
        _optional_non_negative(
            self.accepted_sequence, "operation inspection accepted_sequence"
        )
        if self.context_binding is not None and not isinstance(
            self.context_binding, SessionContextBinding
        ):
            raise ValidationError("operation context_binding must be session-bound")
        _aware(self.observed_at, "operation inspection observed_at")
        object.__setattr__(
            self,
            "evidence_refs",
            _opaque_refs(self.evidence_refs, "operation inspection evidence_refs"),
        )


@dataclass(frozen=True, slots=True)
class HandoffWorkspaceTargetSpec:
    workspace_id: UUID
    task_id: UUID
    target_execution_id: UUID
    target_worker_id: UUID
    adapter_module_ref: UUID
    adapter_config_version: int
    workspace_module_ref: UUID
    workspace_config_version: int
    repositories: tuple[Mapping[str, object], ...]
    anticipated_fencing_token: int

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "task_id",
            "target_execution_id",
            "target_worker_id",
            "adapter_module_ref",
            "workspace_module_ref",
        ):
            _uuid(getattr(self, name), f"workspace target {name}")
        _positive(self.adapter_config_version, "adapter_config_version")
        _positive(self.workspace_config_version, "workspace_config_version")
        _positive(self.anticipated_fencing_token, "anticipated_fencing_token")
        repositories = tuple(
            _mapping(item, "workspace target repository") for item in self.repositories
        )
        if not repositories:
            raise ValidationError("workspace target repositories cannot be empty")
        object.__setattr__(self, "repositories", repositories)


@dataclass(frozen=True, slots=True)
class HandoffWorkspaceTransfer:
    transition_id: UUID
    logical_attempt: int
    operation_id: UUID
    source_manifest_id: UUID
    target_manifest_id: UUID | None
    source_execution_id: UUID
    target_execution_id: UUID
    source_fencing_token: int
    target_fencing_token: int
    location_ref_hash: str
    repository_state_digest: str
    status: HandoffWorkspaceTransferStatus
    failure_code: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "transition_id",
            "operation_id",
            "source_manifest_id",
            "source_execution_id",
            "target_execution_id",
        ):
            _uuid(getattr(self, name), f"workspace transfer {name}")
        _optional_uuid(self.target_manifest_id, "workspace transfer target_manifest_id")
        _positive(self.logical_attempt, "workspace transfer logical_attempt")
        _positive(self.source_fencing_token, "source_fencing_token")
        _positive(self.target_fencing_token, "target_fencing_token")
        if self.target_fencing_token <= self.source_fencing_token:
            raise ValidationError("target fencing token must be higher than source")
        _hash(self.location_ref_hash, "location_ref_hash")
        _hash(self.repository_state_digest, "repository_state_digest")
        object.__setattr__(
            self,
            "status",
            _enum(
                self.status, HandoffWorkspaceTransferStatus, "workspace transfer status"
            ),
        )
        object.__setattr__(
            self,
            "failure_code",
            _optional_text(self.failure_code, "workspace transfer failure_code"),
        )
        _aware(self.created_at, "workspace transfer created_at")
        _aware(self.updated_at, "workspace transfer updated_at")
        if self.updated_at < self.created_at:
            raise ValidationError(
                "workspace transfer updated_at cannot precede created_at"
            )
        if self.status is HandoffWorkspaceTransferStatus.SUCCEEDED:
            if self.target_manifest_id is None or self.failure_code is not None:
                raise ValidationError(
                    "successful workspace transfer requires target manifest"
                )
        elif self.status is not HandoffWorkspaceTransferStatus.REQUESTED and (
            self.failure_code is None
        ):
            raise ValidationError(
                "unsuccessful workspace transfer requires failure_code"
            )


@dataclass(frozen=True, slots=True)
class HandoffWorkspaceInspection:
    transition_id: UUID
    logical_attempt: int
    operation_id: UUID
    outcome: HandoffInspectionOutcome
    source_manifest_id: UUID | None
    target_manifest_id: UUID | None
    source_lifecycle: str | None
    target_lifecycle: str | None
    observed_at: datetime
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _uuid(self.transition_id, "workspace inspection transition_id")
        _positive(self.logical_attempt, "workspace inspection logical_attempt")
        _uuid(self.operation_id, "workspace inspection operation_id")
        object.__setattr__(
            self,
            "outcome",
            _enum(
                self.outcome, HandoffInspectionOutcome, "workspace inspection outcome"
            ),
        )
        _optional_uuid(
            self.source_manifest_id, "workspace inspection source_manifest_id"
        )
        _optional_uuid(
            self.target_manifest_id, "workspace inspection target_manifest_id"
        )
        object.__setattr__(
            self,
            "source_lifecycle",
            _optional_text(self.source_lifecycle, "source_lifecycle"),
        )
        object.__setattr__(
            self,
            "target_lifecycle",
            _optional_text(self.target_lifecycle, "target_lifecycle"),
        )
        _aware(self.observed_at, "workspace inspection observed_at")
        object.__setattr__(
            self,
            "evidence_refs",
            _opaque_refs(self.evidence_refs, "workspace inspection evidence_refs"),
        )


@dataclass(frozen=True, slots=True)
class SessionContextBinding:
    operation_id: UUID
    execution_session_id: UUID
    consumer_execution_id: UUID
    package_id: UUID
    package_hash: str
    context_version: int
    acknowledged_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in (
            "operation_id",
            "execution_session_id",
            "consumer_execution_id",
            "package_id",
        ):
            _uuid(getattr(self, name), f"context binding {name}")
        _schema(self.schema_version, "context binding")
        _hash(self.package_hash, "context binding package_hash")
        _positive(self.context_version, "context binding context_version")
        _aware(self.acknowledged_at, "context binding acknowledged_at")

    def assert_matches(
        self,
        *,
        execution_session_id: UUID,
        consumer_execution_id: UUID,
        package_id: UUID,
        package_hash: str,
        context_version: int,
        operation_id: UUID,
    ) -> None:
        expected = (
            execution_session_id,
            consumer_execution_id,
            package_id,
            package_hash,
            context_version,
            operation_id,
        )
        actual = (
            self.execution_session_id,
            self.consumer_execution_id,
            self.package_id,
            self.package_hash,
            self.context_version,
            self.operation_id,
        )
        if actual != expected:
            raise ValidationError("session context binding does not match target start")

    def to_mapping(self) -> dict[str, object]:
        return {
            "operation_id": str(self.operation_id),
            "execution_session_id": str(self.execution_session_id),
            "consumer_execution_id": str(self.consumer_execution_id),
            "package_id": str(self.package_id),
            "package_hash": self.package_hash,
            "context_version": self.context_version,
            "acknowledged_at": self.acknowledged_at.isoformat(),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class HandoffTargetStartResult:
    execution_id: UUID
    session_id: UUID
    adapter_session_id: str
    context_binding: SessionContextBinding

    def __post_init__(self) -> None:
        _uuid(self.execution_id, "target start execution_id")
        _uuid(self.session_id, "target start session_id")
        object.__setattr__(
            self,
            "adapter_session_id",
            _text(self.adapter_session_id, "target adapter_session_id"),
        )
        if not isinstance(self.context_binding, SessionContextBinding):
            raise ValidationError("target start requires a SessionContextBinding")
        if (
            self.context_binding.execution_session_id != self.session_id
            or self.context_binding.consumer_execution_id != self.execution_id
        ):
            raise ValidationError("target start binding does not match target identity")


@dataclass(frozen=True, slots=True)
class HandoffInputDisposition:
    source_input_id: UUID
    source_status: str
    action: HandoffInputAction
    reason_code: str
    accepted_sequence: int | None
    operation_id: UUID | None

    def __post_init__(self) -> None:
        _uuid(self.source_input_id, "input disposition source_input_id")
        object.__setattr__(
            self, "source_status", _text(self.source_status, "input source_status")
        )
        object.__setattr__(
            self, "action", _enum(self.action, HandoffInputAction, "input action")
        )
        object.__setattr__(
            self, "reason_code", _text(self.reason_code, "input reason_code")
        )
        _optional_non_negative(
            self.accepted_sequence, "input disposition accepted_sequence"
        )
        _optional_uuid(self.operation_id, "input disposition operation_id")


@dataclass(frozen=True, slots=True)
class HandoffInputClassification:
    source_session_id: UUID
    cutoff_sequence: int
    dispositions: tuple[HandoffInputDisposition, ...]
    transferable_input_ids: tuple[UUID, ...]
    excluded_input_ids: tuple[UUID, ...]
    unknown_input_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        _uuid(self.source_session_id, "input classification source_session_id")
        _non_negative(self.cutoff_sequence, "input classification cutoff_sequence")
        dispositions = tuple(self.dispositions)
        if any(not isinstance(item, HandoffInputDisposition) for item in dispositions):
            raise ValidationError("input classification dispositions must be typed")
        disposition_ids = tuple(item.source_input_id for item in dispositions)
        if len(set(disposition_ids)) != len(disposition_ids):
            raise ValidationError("input disposition ids must be unique")
        transferable = _uuid_tuple(
            self.transferable_input_ids, "transferable_input_ids"
        )
        excluded = _uuid_tuple(self.excluded_input_ids, "excluded_input_ids")
        unknown = _uuid_tuple(self.unknown_input_ids, "unknown_input_ids")
        _assert_disjoint((transferable, excluded, unknown), "input classification")
        by_id = {item.source_input_id: item.action for item in dispositions}
        if set(transferable) != {
            input_id
            for input_id, action in by_id.items()
            if action is HandoffInputAction.TRANSFER_QUEUED
        }:
            raise ValidationError("transferable ids do not match dispositions")
        if set(excluded) != {
            input_id
            for input_id, action in by_id.items()
            if action is HandoffInputAction.EXCLUDE
        }:
            raise ValidationError("excluded ids do not match dispositions")
        if set(unknown) != {
            input_id
            for input_id, action in by_id.items()
            if action is HandoffInputAction.BLOCK
        }:
            raise ValidationError("unknown ids do not match dispositions")
        object.__setattr__(self, "dispositions", dispositions)
        object.__setattr__(self, "transferable_input_ids", transferable)
        object.__setattr__(self, "excluded_input_ids", excluded)
        object.__setattr__(self, "unknown_input_ids", unknown)


@dataclass(frozen=True, slots=True)
class HandoffSourceFacts:
    checkpoint: HandoffCheckpointEvidence
    stop: HandoffStopEvidence
    source_context_package_ref: str
    source_worker_context_version: int
    workspace_state: Mapping[str, object]
    artifact_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    input_classification: HandoffInputClassification | None

    def __post_init__(self) -> None:
        if not isinstance(self.checkpoint, HandoffCheckpointEvidence):
            raise ValidationError("source facts require checkpoint evidence")
        if not isinstance(self.stop, HandoffStopEvidence):
            raise ValidationError("source facts require stop evidence")
        self.stop.assert_matches(self.checkpoint)
        object.__setattr__(
            self,
            "source_context_package_ref",
            _opaque_ref(self.source_context_package_ref, "source_context_package_ref"),
        )
        _positive(self.source_worker_context_version, "source_worker_context_version")
        object.__setattr__(
            self,
            "workspace_state",
            _mapping(self.workspace_state, "source workspace_state"),
        )
        object.__setattr__(
            self,
            "artifact_refs",
            _opaque_refs(self.artifact_refs, "source artifact_refs"),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _opaque_refs(self.evidence_refs, "source evidence_refs"),
        )
        if self.input_classification is not None and not isinstance(
            self.input_classification, HandoffInputClassification
        ):
            raise ValidationError("source input classification must be typed")


@dataclass(frozen=True, slots=True)
class HandoffContextBundle:
    id: UUID
    schema_version: int
    workspace_id: UUID
    worker_item_id: UUID
    handoff_transition_id: UUID
    logical_attempt: int
    recovery_decision_id: UUID | None
    source_execution_id: UUID
    source_session_id: UUID
    source_checkpoint_sequence: int
    source_last_accepted_sequence: int
    source_context_package_id: UUID
    source_context_package_hash: str
    source_worker_context_version: int
    target_execution_id: UUID
    target_worker_id: UUID
    target_runtime_snapshot_hash: str
    target_context_version: int
    source_workspace_manifest_id: UUID
    source_workspace_version: int
    workspace_location_ref_hash: str
    repository_state: tuple[Mapping[str, object], ...]
    artifact_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    handoff_reason: str
    handoff_instruction: str
    delivered_input_refs: tuple[UUID, ...]
    transferable_input_refs: tuple[UUID, ...]
    excluded_input_refs: tuple[UUID, ...]
    bundle_hash: str
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        id: UUID,
        schema_version: int,
        workspace_id: UUID,
        worker_item_id: UUID,
        handoff_transition_id: UUID,
        logical_attempt: int,
        recovery_decision_id: UUID | None,
        source_execution_id: UUID,
        source_session_id: UUID,
        source_checkpoint_sequence: int,
        source_last_accepted_sequence: int,
        source_context_package_id: UUID,
        source_context_package_hash: str,
        source_worker_context_version: int,
        target_execution_id: UUID,
        target_worker_id: UUID,
        target_runtime_snapshot_hash: str,
        target_context_version: int,
        source_workspace_manifest_id: UUID,
        source_workspace_version: int,
        workspace_location_ref_hash: str,
        repository_state: tuple[Mapping[str, object], ...],
        artifact_refs: tuple[str, ...],
        evidence_refs: tuple[str, ...],
        handoff_reason: str,
        handoff_instruction: str,
        delivered_input_refs: tuple[UUID, ...],
        transferable_input_refs: tuple[UUID, ...],
        excluded_input_refs: tuple[UUID, ...],
        created_at: datetime,
    ) -> HandoffContextBundle:
        values = {
            "id": id,
            "schema_version": schema_version,
            "workspace_id": workspace_id,
            "worker_item_id": worker_item_id,
            "handoff_transition_id": handoff_transition_id,
            "logical_attempt": logical_attempt,
            "recovery_decision_id": recovery_decision_id,
            "source_execution_id": source_execution_id,
            "source_session_id": source_session_id,
            "source_checkpoint_sequence": source_checkpoint_sequence,
            "source_last_accepted_sequence": source_last_accepted_sequence,
            "source_context_package_id": source_context_package_id,
            "source_context_package_hash": source_context_package_hash,
            "source_worker_context_version": source_worker_context_version,
            "target_execution_id": target_execution_id,
            "target_worker_id": target_worker_id,
            "target_runtime_snapshot_hash": target_runtime_snapshot_hash,
            "target_context_version": target_context_version,
            "source_workspace_manifest_id": source_workspace_manifest_id,
            "source_workspace_version": source_workspace_version,
            "workspace_location_ref_hash": workspace_location_ref_hash,
            "repository_state": repository_state,
            "artifact_refs": artifact_refs,
            "evidence_refs": evidence_refs,
            "handoff_reason": handoff_reason,
            "handoff_instruction": handoff_instruction,
            "delivered_input_refs": delivered_input_refs,
            "transferable_input_refs": transferable_input_refs,
            "excluded_input_refs": excluded_input_refs,
            "created_at": created_at,
        }
        bundle_hash = hashlib.sha256(_canonical(_bundle_payload(values))).hexdigest()
        return cls(**values, bundle_hash=bundle_hash)

    def __post_init__(self) -> None:
        _uuid(self.id, "bundle id")
        _schema(self.schema_version, "bundle")
        for name in (
            "workspace_id",
            "worker_item_id",
            "handoff_transition_id",
            "source_execution_id",
            "source_session_id",
            "source_context_package_id",
            "target_execution_id",
            "target_worker_id",
            "source_workspace_manifest_id",
        ):
            _uuid(getattr(self, name), f"bundle {name}")
        _optional_uuid(self.recovery_decision_id, "bundle recovery_decision_id")
        _positive(self.logical_attempt, "bundle logical_attempt")
        _non_negative(
            self.source_checkpoint_sequence, "bundle source_checkpoint_sequence"
        )
        _non_negative(
            self.source_last_accepted_sequence, "bundle source_last_accepted_sequence"
        )
        if self.source_last_accepted_sequence < self.source_checkpoint_sequence:
            raise ValidationError("bundle accepted sequence cannot precede checkpoint")
        for name in (
            "source_worker_context_version",
            "target_context_version",
            "source_workspace_version",
        ):
            _positive(getattr(self, name), f"bundle {name}")
        for name in (
            "source_context_package_hash",
            "target_runtime_snapshot_hash",
            "workspace_location_ref_hash",
        ):
            _hash(getattr(self, name), f"bundle {name}")
        object.__setattr__(
            self,
            "repository_state",
            tuple(
                _mapping(item, "bundle repository_state")
                for item in self.repository_state
            ),
        )
        object.__setattr__(
            self,
            "artifact_refs",
            _opaque_refs(self.artifact_refs, "bundle artifact_refs"),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _opaque_refs(self.evidence_refs, "bundle evidence_refs"),
        )
        object.__setattr__(
            self, "handoff_reason", _text(self.handoff_reason, "bundle handoff_reason")
        )
        object.__setattr__(
            self,
            "handoff_instruction",
            _text(self.handoff_instruction, "bundle handoff_instruction"),
        )
        delivered = _uuid_tuple(
            self.delivered_input_refs, "bundle delivered_input_refs"
        )
        transferable = _uuid_tuple(
            self.transferable_input_refs, "bundle transferable_input_refs"
        )
        excluded = _uuid_tuple(self.excluded_input_refs, "bundle excluded_input_refs")
        _assert_disjoint((delivered, transferable, excluded), "bundle input refs")
        object.__setattr__(self, "delivered_input_refs", delivered)
        object.__setattr__(self, "transferable_input_refs", transferable)
        object.__setattr__(self, "excluded_input_refs", excluded)
        _hash(self.bundle_hash, "bundle_hash")
        _aware(self.created_at, "bundle created_at")
        if self.bundle_hash != self.content_hash:
            raise ValidationError("bundle hash does not match content")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(_canonical(self.canonical_payload())).hexdigest()

    def canonical_payload(self) -> dict[str, object]:
        return _bundle_payload(self)

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            **self.canonical_payload(),
            "bundle_hash": self.bundle_hash,
            "created_at": self.created_at.isoformat(),
        }


def _schema(value: object, label: str) -> None:
    if value != 1:
        raise ValidationError(f"{label} schema_version must be 1")


def _enum[EnumValue: StrEnum](
    value: object, enum_type: type[EnumValue], label: str
) -> EnumValue:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported {label}: {value!r}") from exc


def _uuid(value: object, label: str) -> UUID:
    if not isinstance(value, UUID):
        raise ValidationError(f"{label} must be a UUID")
    return value


def _optional_uuid(value: object, label: str) -> UUID | None:
    if value is None:
        return None
    return _uuid(value, label)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} is required")
    clean = value.strip()
    if len(clean) > _MAX_TEXT_LENGTH:
        raise ValidationError(f"{label} is too long")
    return clean


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _positive(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{label} must be positive")
    return value


def _non_negative(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{label} must be non-negative")
    return value


def _optional_non_negative(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _non_negative(value, label)


def _aware(value: object, label: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValidationError(f"{label} must be timezone-aware")
    return value


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{label} must be a lowercase SHA-256 hash")
    return value


def _opaque_ref(value: object, label: str) -> str:
    clean = _text(value, label)
    if (
        clean.startswith(("/", "~/", "\\", "file://"))
        or _WINDOWS_PATH_PATTERN.match(clean) is not None
    ):
        raise ValidationError(f"{label} must be an opaque non-absolute reference")
    return clean


def _opaque_refs(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValidationError(f"{label} must be a sequence")
    clean: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _opaque_ref(value, label)
        if item not in seen:
            seen.add(item)
            clean.append(item)
    return tuple(clean)


def _uuid_tuple(values: Sequence[UUID], label: str) -> tuple[UUID, ...]:
    if isinstance(values, (str, bytes)):
        raise ValidationError(f"{label} must be a sequence of UUIDs")
    result: list[UUID] = []
    seen: set[UUID] = set()
    for value in values:
        item = _uuid(value, label)
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _assert_disjoint(groups: Sequence[Sequence[UUID]], label: str) -> None:
    seen: set[UUID] = set()
    for group in groups:
        overlap = seen.intersection(group)
        if overlap:
            raise ValidationError(f"{label} contains overlapping ids")
        seen.update(group)


def _mapping(value: object, label: str, *, depth: int = 0) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be a mapping")
    if depth > _MAX_MAP_DEPTH:
        raise ValidationError(f"{label} is too deeply nested")
    if len(value) > _MAX_MAP_ENTRIES:
        raise ValidationError(f"{label} has too many entries")
    result: dict[str, object] = {}
    for key, item in value.items():
        clean_key = _mapping_key(key, label)
        result[clean_key] = _freeze(item, f"{label}.{clean_key}", depth=depth + 1)
    frozen = MappingProxyType(result)
    if len(_canonical(frozen)) > _MAX_MAP_BYTES:
        raise ValidationError(f"{label} is too large")
    return frozen


def _mapping_key(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} keys must be non-empty strings")
    clean = value.strip()
    lowered = clean.lower()
    if any(part in lowered for part in _FORBIDDEN_KEY_PARTS):
        raise ValidationError(f"{label} contains forbidden field {clean}")
    return clean


def _freeze(value: object, label: str, *, depth: int) -> object:
    if isinstance(value, Mapping):
        return _mapping(value, label, depth=depth)
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_MAP_ENTRIES:
            raise ValidationError(f"{label} has too many items")
        return tuple(_freeze(item, label, depth=depth) for item in value)
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            if len(value) > _MAX_TEXT_LENGTH:
                raise ValidationError(f"{label} contains text that is too long")
            if (
                value.startswith(("/", "~/", "\\", "file://"))
                or _WINDOWS_PATH_PATTERN.match(value) is not None
            ):
                raise ValidationError(f"{label} contains an absolute path")
        return value
    raise ValidationError(f"{label} contains unsupported or inline content")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    return value


def _bundle_payload(source: object) -> dict[str, object]:
    def value(name: str) -> object:
        if isinstance(source, Mapping):
            return source[name]
        return getattr(source, name)

    recovery_decision_id = value("recovery_decision_id")
    return {
        "schema_version": value("schema_version"),
        "workspace_id": str(value("workspace_id")),
        "worker_item_id": str(value("worker_item_id")),
        "handoff_transition_id": str(value("handoff_transition_id")),
        "logical_attempt": value("logical_attempt"),
        "recovery_decision_id": (
            str(recovery_decision_id) if recovery_decision_id is not None else None
        ),
        "source_execution_id": str(value("source_execution_id")),
        "source_session_id": str(value("source_session_id")),
        "source_checkpoint_sequence": value("source_checkpoint_sequence"),
        "source_last_accepted_sequence": value("source_last_accepted_sequence"),
        "source_context_package_id": str(value("source_context_package_id")),
        "source_context_package_hash": value("source_context_package_hash"),
        "source_worker_context_version": value("source_worker_context_version"),
        "target_execution_id": str(value("target_execution_id")),
        "target_worker_id": str(value("target_worker_id")),
        "target_runtime_snapshot_hash": value("target_runtime_snapshot_hash"),
        "target_context_version": value("target_context_version"),
        "source_workspace_manifest_id": str(value("source_workspace_manifest_id")),
        "source_workspace_version": value("source_workspace_version"),
        "workspace_location_ref_hash": value("workspace_location_ref_hash"),
        "repository_state": _plain(value("repository_state")),
        "artifact_refs": list(_sequence(value("artifact_refs"), "artifact_refs")),
        "evidence_refs": list(_sequence(value("evidence_refs"), "evidence_refs")),
        "handoff_reason": value("handoff_reason"),
        "handoff_instruction": value("handoff_instruction"),
        "delivered_input_refs": [
            str(item)
            for item in _sequence(value("delivered_input_refs"), "delivered_input_refs")
        ],
        "transferable_input_refs": [
            str(item)
            for item in _sequence(
                value("transferable_input_refs"), "transferable_input_refs"
            )
        ],
        "excluded_input_refs": [
            str(item)
            for item in _sequence(value("excluded_input_refs"), "excluded_input_refs")
        ],
    }


def _sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValidationError(f"{label} must be a sequence")
    return value


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            _plain(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("handoff evidence cannot be canonicalized") from exc
