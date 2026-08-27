from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

from sidera_control_plane.domain.errors import (
    InvalidTransition,
    StaleFencingToken,
    ValidationError,
)
from sidera_control_plane.domain.plan_revisions import WorkerTransitionAction


class PlanTransitionStatus(StrEnum):
    CONVERGING = "converging"
    CONVERGED = "converged"
    DEGRADED = "degraded"


class WorkerTransitionStatus(StrEnum):
    PENDING = "pending"
    WAITING_SAFE_BOUNDARY = "waiting_safe_boundary"
    APPLYING = "applying"
    WAITING_ACTIVATION = "waiting_activation"
    CONVERGED = "converged"
    FAILED = "failed"
    BLOCKED_UNKNOWN = "blocked_unknown"


class WriterState(StrEnum):
    ACTIVE = "active"
    FENCED = "fenced"
    CLOSED = "closed"
    UNKNOWN = "unknown"


WORKER_TRANSITION_IN_FLIGHT = frozenset(
    {
        WorkerTransitionStatus.PENDING,
        WorkerTransitionStatus.WAITING_SAFE_BOUNDARY,
        WorkerTransitionStatus.APPLYING,
        WorkerTransitionStatus.WAITING_ACTIVATION,
    }
)
WORKER_TRANSITION_TERMINAL = frozenset(
    {
        WorkerTransitionStatus.CONVERGED,
        WorkerTransitionStatus.FAILED,
        WorkerTransitionStatus.BLOCKED_UNKNOWN,
    }
)


@dataclass(frozen=True, slots=True)
class PlanEntityLineage:
    id: UUID
    plan_transition_id: UUID
    workspace_id: UUID
    target_type: str
    target_id: UUID
    source_type: str
    source_id: UUID
    reason: str

    def __post_init__(self) -> None:
        for name in (
            "id",
            "plan_transition_id",
            "workspace_id",
            "target_id",
            "source_id",
        ):
            if not isinstance(getattr(self, name), UUID):
                raise ValidationError(f"plan entity lineage {name} must be a UUID")
        for name in ("target_type", "source_type", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"plan entity lineage {name} is required")
            object.__setattr__(self, name, value.strip())


@dataclass(slots=True)
class PlanTransition:
    id: UUID
    workspace_id: UUID
    root_task_id: UUID
    planning_request_id: UUID
    proposal_revision_id: UUID
    from_snapshot_id: UUID
    from_generation: int
    to_snapshot_id: UUID
    to_generation: int
    status: PlanTransitionStatus
    started_at: datetime
    completed_at: datetime | None
    version: int

    def __post_init__(self) -> None:
        for name in (
            "id",
            "workspace_id",
            "root_task_id",
            "planning_request_id",
            "proposal_revision_id",
            "from_snapshot_id",
            "to_snapshot_id",
        ):
            if not isinstance(getattr(self, name), UUID):
                raise ValidationError(f"plan transition {name} must be a UUID")
        _positive(self.from_generation, "from_generation")
        _positive(self.to_generation, "to_generation")
        if self.to_generation != self.from_generation + 1:
            raise ValidationError("plan transition generations must be consecutive")
        if self.from_snapshot_id == self.to_snapshot_id:
            raise ValidationError("plan transition snapshots must be different")
        self.status = _enum(self.status, PlanTransitionStatus, "plan transition status")
        _aware(self.started_at, "plan transition started_at")
        if self.completed_at is not None:
            _aware(self.completed_at, "plan transition completed_at")
        _positive(self.version, "plan transition version")

    def reconcile(
        self, workers: Iterable[WorkerTransition], *, now: datetime
    ) -> None:
        items = tuple(workers)
        _aware(now, "plan transition reconcile time")
        if any(
            item.status is WorkerTransitionStatus.BLOCKED_UNKNOWN
            or item.status is WorkerTransitionStatus.FAILED
            for item in items
        ):
            self.status = PlanTransitionStatus.DEGRADED
        elif all(item.status is WorkerTransitionStatus.CONVERGED for item in items):
            self.status = PlanTransitionStatus.CONVERGED
        else:
            self.status = PlanTransitionStatus.CONVERGING

        has_unknown = any(
            item.status is WorkerTransitionStatus.BLOCKED_UNKNOWN
            or item.writer_state is WriterState.UNKNOWN
            for item in items
        )
        all_terminal = all(item.status in WORKER_TRANSITION_TERMINAL for item in items)
        if items and all_terminal and not has_unknown and all(
            item.can_release_fence for item in items
        ):
            self.completed_at = now
        else:
            self.completed_at = None
        self.version += 1

    def is_confirmation_blocked(self, workers: Iterable[WorkerTransition]) -> bool:
        items = tuple(workers)
        return self.confirmation_blocked(
            has_in_flight=any(item.status in WORKER_TRANSITION_IN_FLIGHT for item in items),
            has_unknown=any(
                item.status is WorkerTransitionStatus.BLOCKED_UNKNOWN
                or item.writer_state is WriterState.UNKNOWN
                for item in items
            ),
            has_active_fence=any(
                item.writer_state in {WriterState.ACTIVE, WriterState.FENCED}
                for item in items
                if item.status not in {
                    WorkerTransitionStatus.CONVERGED,
                    WorkerTransitionStatus.FAILED,
                }
            ),
        )

    @staticmethod
    def confirmation_blocked(
        *, has_in_flight: bool, has_unknown: bool, has_active_fence: bool
    ) -> bool:
        return has_in_flight or has_unknown or has_active_fence


@dataclass(slots=True)
class WorkerTransition:
    id: UUID
    plan_transition_id: UUID
    workspace_id: UUID
    root_task_id: UUID
    worker_item_id: UUID
    from_subtask_id: UUID | None
    to_subtask_id: UUID | None
    action: WorkerTransitionAction
    fallback_action: WorkerTransitionAction | None
    target_context_version: int
    source_execution_id: UUID | None
    source_session_id: UUID | None
    target_execution_id: UUID | None
    target_session_id: UUID | None
    writer_state: WriterState
    status: WorkerTransitionStatus
    failure_code: str | None
    lease_id: UUID | None
    fencing_token: int
    attempt_count: int
    next_attempt_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    version: int

    def __post_init__(self) -> None:
        for name in (
            "id",
            "plan_transition_id",
            "workspace_id",
            "root_task_id",
            "worker_item_id",
        ):
            if not isinstance(getattr(self, name), UUID):
                raise ValidationError(f"worker transition {name} must be a UUID")
        for name in (
            "from_subtask_id",
            "to_subtask_id",
            "source_execution_id",
            "source_session_id",
            "target_execution_id",
            "target_session_id",
            "lease_id",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, UUID):
                raise ValidationError(f"worker transition {name} must be a UUID")
        self.action = _enum(self.action, WorkerTransitionAction, "worker transition action")
        if self.fallback_action is not None:
            self.fallback_action = _enum(
                self.fallback_action,
                WorkerTransitionAction,
                "worker transition fallback action",
            )
        _positive(self.target_context_version, "target_context_version")
        self.writer_state = _enum(self.writer_state, WriterState, "writer state")
        self.status = _enum(self.status, WorkerTransitionStatus, "worker transition status")
        if self.failure_code is not None:
            self.failure_code = _required(self.failure_code, "failure_code")
        if self.fencing_token < 0:
            raise ValidationError("fencing_token cannot be negative")
        if self.attempt_count < 0:
            raise ValidationError("attempt_count cannot be negative")
        if self.next_attempt_at is not None:
            _aware(self.next_attempt_at, "next_attempt_at")
        if self.started_at is not None:
            _aware(self.started_at, "started_at")
        if self.completed_at is not None:
            _aware(self.completed_at, "completed_at")
        _positive(self.version, "worker transition version")

    @property
    def can_auto_continue(self) -> bool:
        return self.status not in {
            WorkerTransitionStatus.FAILED,
            WorkerTransitionStatus.BLOCKED_UNKNOWN,
        }

    @property
    def can_release_fence(self) -> bool:
        return self.writer_state is WriterState.CLOSED and self.status in {
            WorkerTransitionStatus.CONVERGED,
            WorkerTransitionStatus.FAILED,
        }

    def claim(self, *, lease_id: UUID, fencing_token: int, now: datetime) -> None:
        if not isinstance(lease_id, UUID) or fencing_token < 1:
            raise StaleFencingToken("worker transition lease and fencing token are invalid")
        _aware(now, "worker transition claim time")
        if self.lease_id is not None:
            if lease_id != self.lease_id or fencing_token < self.fencing_token:
                raise StaleFencingToken("worker transition is owned by another fence")
            if fencing_token == self.fencing_token:
                return
        self.lease_id = lease_id
        self.fencing_token = fencing_token
        self.version += 1

    def wait_for_safe_boundary(self, *, now: datetime) -> None:
        self._move(WorkerTransitionStatus.WAITING_SAFE_BOUNDARY, now=now)

    def begin_apply(self, *, now: datetime) -> None:
        self._move(WorkerTransitionStatus.APPLYING, now=now)
        self.attempt_count += 1
        self.next_attempt_at = None
        self.writer_state = WriterState.ACTIVE

    def wait_for_activation(self, *, now: datetime) -> None:
        self._move(WorkerTransitionStatus.WAITING_ACTIVATION, now=now)

    def converge(self, *, now: datetime) -> None:
        if self.status not in {
            WorkerTransitionStatus.PENDING,
            WorkerTransitionStatus.WAITING_SAFE_BOUNDARY,
            WorkerTransitionStatus.APPLYING,
            WorkerTransitionStatus.WAITING_ACTIVATION,
        }:
            raise InvalidTransition("worker transition cannot converge from its current state")
        _aware(now, "worker transition completed_at")
        self.status = WorkerTransitionStatus.CONVERGED
        self.writer_state = WriterState.CLOSED
        self.completed_at = now
        self.version += 1

    def mark_unknown(self, *, failure_code: str, now: datetime) -> None:
        if self.status not in {
            WorkerTransitionStatus.APPLYING,
            WorkerTransitionStatus.WAITING_ACTIVATION,
        }:
            raise InvalidTransition("only an applying transition can become unknown")
        self.failure_code = _required(failure_code, "failure_code")
        _aware(now, "worker transition completed_at")
        self.status = WorkerTransitionStatus.BLOCKED_UNKNOWN
        self.writer_state = WriterState.UNKNOWN
        self.completed_at = None
        self.version += 1

    def fail(self, *, failure_code: str, writer_closed: bool, now: datetime) -> None:
        if self.status not in {
            WorkerTransitionStatus.APPLYING,
            WorkerTransitionStatus.WAITING_ACTIVATION,
        }:
            raise InvalidTransition("only an applying transition can fail")
        self.failure_code = _required(failure_code, "failure_code")
        _aware(now, "worker transition completed_at")
        if writer_closed:
            self.status = WorkerTransitionStatus.FAILED
            self.writer_state = WriterState.CLOSED
            self.completed_at = now
        else:
            self.status = WorkerTransitionStatus.BLOCKED_UNKNOWN
            self.writer_state = WriterState.UNKNOWN
            self.completed_at = None
        self.version += 1

    def retry(self, *, now: datetime) -> None:
        if self.status is WorkerTransitionStatus.BLOCKED_UNKNOWN:
            raise InvalidTransition("unknown worker transition requires reconciliation")
        if self.status is not WorkerTransitionStatus.FAILED:
            raise InvalidTransition("only a known failed transition can retry")
        if not self.can_release_fence:
            raise InvalidTransition("failed worker transition writer is not closed")
        _aware(now, "worker transition retry time")
        self.status = WorkerTransitionStatus.PENDING
        self.writer_state = WriterState.FENCED
        self.failure_code = None
        self.completed_at = None
        self.next_attempt_at = now
        self.version += 1

    def bind_target(
        self, *, execution_id: UUID, session_id: UUID | None, now: datetime
    ) -> None:
        if self.status not in {
            WorkerTransitionStatus.APPLYING,
            WorkerTransitionStatus.WAITING_ACTIVATION,
        }:
            raise InvalidTransition("target execution can only bind while applying")
        if not isinstance(execution_id, UUID):
            raise ValidationError("target execution_id must be a UUID")
        if session_id is not None and not isinstance(session_id, UUID):
            raise ValidationError("target session_id must be a UUID")
        _aware(now, "worker transition target bind time")
        self.target_execution_id = execution_id
        self.target_session_id = session_id
        self.version += 1

    def _move(self, target: WorkerTransitionStatus, *, now: datetime) -> None:
        allowed = {
            WorkerTransitionStatus.PENDING: {
                WorkerTransitionStatus.WAITING_SAFE_BOUNDARY,
            },
            WorkerTransitionStatus.WAITING_SAFE_BOUNDARY: {
                WorkerTransitionStatus.APPLYING,
            },
            WorkerTransitionStatus.APPLYING: {
                WorkerTransitionStatus.WAITING_ACTIVATION,
            },
        }
        if target not in allowed.get(self.status, set()):
            raise InvalidTransition(
                f"worker transition cannot move from {self.status} to {target}"
            )
        _aware(now, "worker transition timestamp")
        if self.started_at is None:
            self.started_at = now
        self.status = target
        self.version += 1


def _enum[EnumValue: StrEnum](
    value: object, enum_type: type[EnumValue], label: str
) -> EnumValue:
    try:
        return (
            value
            if isinstance(value, enum_type)
            else cast(EnumValue, enum_type(value))
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"unsupported {label}: {value!r}") from exc


def _required(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} is required")
    return value.strip()


def _positive(value: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{label} must be positive")


def _aware(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationError(f"{label} must be timezone-aware")
