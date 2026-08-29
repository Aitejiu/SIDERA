from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.handoff_evidence import (
    HandoffOperationInspection,
    HandoffWorkspaceInspection,
    _aware,
    _enum,
    _mapping,
    _opaque_refs,
    _optional_uuid,
    _positive,
    _text,
    _uuid,
    _uuid_tuple,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffSourceChannel,
    HandoffTransitionStatus,
    HandoffWriterState,
)


class HandoffRecoveryStrategy(StrEnum):
    RETRY_CURRENT_PHASE = "retry_current_phase"
    RETRY_SAME_TARGET = "retry_same_target"
    SELECT_NEW_TARGET = "select_new_target"
    CONTINUE_WITH_SOURCE_AGENT = "continue_with_source_agent"
    CONFIRM_SOURCE_STOPPED = "confirm_source_stopped"
    CONFIRM_TARGET_ABSENT = "confirm_target_absent"
    CONFIRM_TARGET_ACTIVE = "confirm_target_active"


class HandoffSafeConclusion(StrEnum):
    OPERATION_ABSENT = "operation_absent"
    OPERATION_SUCCEEDED = "operation_succeeded"
    OPERATION_FAILED = "operation_failed"
    SOURCE_STOPPED = "source_stopped"
    TARGET_ABSENT = "target_absent"
    TARGET_ACTIVE = "target_active"


@dataclass(frozen=True, slots=True)
class HandoffRecoveryInspection:
    transition_id: UUID
    transition_version: int
    logical_attempt: int
    phase: HandoffPhase
    writer_state: HandoffWriterState
    blocked_operation_id: UUID | None
    operation_inspections: tuple[HandoffOperationInspection, ...]
    workspace_inspection: HandoffWorkspaceInspection | None
    source_session_state: str
    target_session_state: str | None
    target_manifest_state: str | None
    safe_conclusions: tuple[HandoffSafeConclusion, ...]
    evidence_refs: tuple[str, ...]
    inspected_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.transition_id, "recovery inspection transition_id")
        _positive(self.transition_version, "recovery inspection transition_version")
        _positive(self.logical_attempt, "recovery inspection logical_attempt")
        object.__setattr__(
            self,
            "phase",
            _enum(self.phase, HandoffPhase, "recovery inspection phase"),
        )
        object.__setattr__(
            self,
            "writer_state",
            _enum(
                self.writer_state,
                HandoffWriterState,
                "recovery inspection writer_state",
            ),
        )
        _optional_uuid(
            self.blocked_operation_id, "recovery inspection blocked_operation_id"
        )
        operations = tuple(self.operation_inspections)
        if any(not isinstance(item, HandoffOperationInspection) for item in operations):
            raise ValidationError("recovery operation inspections must be typed")
        operation_ids = tuple(item.operation_id for item in operations)
        if len(set(operation_ids)) != len(operation_ids):
            raise ValidationError("recovery operation inspection ids must be unique")
        object.__setattr__(self, "operation_inspections", operations)
        if self.workspace_inspection is not None and not isinstance(
            self.workspace_inspection, HandoffWorkspaceInspection
        ):
            raise ValidationError("recovery workspace inspection must be typed")
        object.__setattr__(
            self,
            "source_session_state",
            _text(self.source_session_state, "source_session_state"),
        )
        object.__setattr__(
            self,
            "target_session_state",
            _optional_text(self.target_session_state, "target_session_state"),
        )
        object.__setattr__(
            self,
            "target_manifest_state",
            _optional_text(self.target_manifest_state, "target_manifest_state"),
        )
        conclusions = tuple(
            _enum(item, HandoffSafeConclusion, "safe conclusion")
            for item in self.safe_conclusions
        )
        object.__setattr__(self, "safe_conclusions", _unique(conclusions))
        object.__setattr__(
            self,
            "evidence_refs",
            _opaque_refs(self.evidence_refs, "recovery inspection evidence_refs"),
        )
        _aware(self.inspected_at, "recovery inspection inspected_at")


@dataclass(frozen=True, slots=True)
class HandoffRecoveryDecision:
    id: UUID
    handoff_transition_id: UUID
    expected_transition_version: int
    strategy: HandoffRecoveryStrategy
    actor_type: str
    actor_id: str
    source_channel: HandoffSourceChannel
    reason: str
    evidence_refs: tuple[str, ...]
    replacement_target_snapshot: Mapping[str, object] | None
    resulting_operation_refs: tuple[UUID, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.id, "recovery decision id")
        _uuid(self.handoff_transition_id, "recovery decision transition_id")
        _positive(
            self.expected_transition_version,
            "recovery decision expected_transition_version",
        )
        object.__setattr__(
            self,
            "strategy",
            _enum(self.strategy, HandoffRecoveryStrategy, "recovery strategy"),
        )
        object.__setattr__(
            self, "actor_type", _text(self.actor_type, "recovery actor_type")
        )
        object.__setattr__(self, "actor_id", _text(self.actor_id, "recovery actor_id"))
        object.__setattr__(
            self,
            "source_channel",
            _enum(self.source_channel, HandoffSourceChannel, "recovery source_channel"),
        )
        object.__setattr__(self, "reason", _text(self.reason, "recovery reason"))
        evidence_refs = _opaque_refs(self.evidence_refs, "recovery evidence_refs")
        if not evidence_refs:
            raise ValidationError("recovery evidence_refs cannot be empty")
        object.__setattr__(self, "evidence_refs", evidence_refs)
        if self.replacement_target_snapshot is not None:
            object.__setattr__(
                self,
                "replacement_target_snapshot",
                _mapping(
                    self.replacement_target_snapshot,
                    "replacement_target_snapshot",
                ),
            )
        if (
            self.strategy is HandoffRecoveryStrategy.SELECT_NEW_TARGET
            and self.replacement_target_snapshot is None
        ):
            raise ValidationError(
                "select_new_target requires a replacement target snapshot"
            )
        if (
            self.strategy is not HandoffRecoveryStrategy.SELECT_NEW_TARGET
            and self.replacement_target_snapshot is not None
        ):
            raise ValidationError(
                "replacement target snapshot requires select_new_target"
            )
        object.__setattr__(
            self,
            "resulting_operation_refs",
            _uuid_tuple(self.resulting_operation_refs, "resulting_operation_refs"),
        )
        _aware(self.created_at, "recovery decision created_at")


@dataclass(frozen=True, slots=True)
class HandoffRecoveryResult:
    transition_id: UUID
    transition_version: int
    status: HandoffTransitionStatus
    phase: HandoffPhase
    writer_state: HandoffWriterState
    logical_attempt: int
    resulting_operation_refs: tuple[UUID, ...]

    def __post_init__(self) -> None:
        _uuid(self.transition_id, "recovery result transition_id")
        _positive(self.transition_version, "recovery result transition_version")
        object.__setattr__(
            self,
            "status",
            _enum(self.status, HandoffTransitionStatus, "recovery result status"),
        )
        object.__setattr__(
            self,
            "phase",
            _enum(self.phase, HandoffPhase, "recovery result phase"),
        )
        object.__setattr__(
            self,
            "writer_state",
            _enum(
                self.writer_state, HandoffWriterState, "recovery result writer_state"
            ),
        )
        _positive(self.logical_attempt, "recovery result logical_attempt")
        object.__setattr__(
            self,
            "resulting_operation_refs",
            _uuid_tuple(self.resulting_operation_refs, "resulting_operation_refs"),
        )


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _unique[T](values: tuple[T, ...]) -> tuple[T, ...]:
    result: list[T] = []
    seen: set[T] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)
