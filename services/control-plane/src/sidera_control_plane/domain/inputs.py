from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError


class ExecutionInputStatus(StrEnum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    DELIVERED = "delivered"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


EDITABLE_INPUT_STATUSES = {
    ExecutionInputStatus.ACCEPTED,
    ExecutionInputStatus.QUEUED,
}


@dataclass(slots=True)
class ExecutionInput:
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    session_id: UUID
    client_idempotency_key: str
    text: str
    attachment_refs: tuple[str, ...]
    status: ExecutionInputStatus
    operation_id: UUID | None
    failure_code: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    source_command_id: UUID | None = None
    superseded_reason: str | None = None

    @classmethod
    def accept(
        cls,
        *,
        id: UUID,
        workspace_id: UUID,
        execution_id: UUID,
        session_id: UUID,
        client_idempotency_key: str,
        text: str,
        attachment_refs: tuple[str, ...],
        now: datetime,
        source_command_id: UUID | None = None,
    ) -> "ExecutionInput":
        if not text:
            raise ValidationError("input text cannot be empty")
        if not client_idempotency_key.strip():
            raise ValidationError("client_idempotency_key is required")
        if any(not ref.strip() for ref in attachment_refs):
            raise ValidationError("attachment_refs cannot contain empty values")
        if now.tzinfo is None:
            raise ValidationError("input timestamp must be timezone-aware")
        return cls(
            id=id,
            workspace_id=workspace_id,
            execution_id=execution_id,
            session_id=session_id,
            client_idempotency_key=client_idempotency_key.strip(),
            text=text,
            attachment_refs=tuple(attachment_refs),
            status=ExecutionInputStatus.ACCEPTED,
            operation_id=None,
            failure_code=None,
            version=1,
            created_at=now,
            updated_at=now,
            source_command_id=source_command_id,
            superseded_reason=None,
        )

    def update_text(self, text: str) -> None:
        if self.status not in EDITABLE_INPUT_STATUSES:
            raise InvalidTransition("only accepted or queued input can be edited")
        if not text:
            raise ValidationError("input text cannot be empty")
        self.text = text
        self.version += 1

    def queue(self) -> None:
        self._transition(ExecutionInputStatus.ACCEPTED, ExecutionInputStatus.QUEUED)

    def begin_dispatch(self, *, operation_id: UUID) -> None:
        if self.status is not ExecutionInputStatus.QUEUED:
            raise InvalidTransition("only queued input can begin dispatch")
        self.status = ExecutionInputStatus.DISPATCHING
        self.operation_id = operation_id
        self.failure_code = None
        self.version += 1

    def defer(self) -> None:
        if self.status is not ExecutionInputStatus.DISPATCHING:
            raise InvalidTransition("only dispatching input can be deferred")
        self.status = ExecutionInputStatus.QUEUED
        self.operation_id = None
        self.failure_code = None
        self.version += 1

    def deliver(self) -> None:
        self._transition(ExecutionInputStatus.DISPATCHING, ExecutionInputStatus.DELIVERED)
        self.operation_id = None

    def block_unknown(self, *, failure_code: str) -> None:
        self._finish_dispatch(ExecutionInputStatus.BLOCKED, failure_code)

    def fail(self, *, failure_code: str) -> None:
        self._finish_dispatch(ExecutionInputStatus.FAILED, failure_code)

    def retry_blocked(self) -> None:
        if self.status is not ExecutionInputStatus.BLOCKED:
            raise InvalidTransition("only blocked input can be retried")
        self.status = ExecutionInputStatus.QUEUED
        self.operation_id = None
        self.failure_code = None
        self.version += 1

    def cancel(self, *, now: datetime) -> None:
        if self.status not in EDITABLE_INPUT_STATUSES:
            raise InvalidTransition("only accepted or queued input can be cancelled")
        self.status = ExecutionInputStatus.CANCELLED
        self.updated_at = now
        self.version += 1

    def supersede(self, *, now: datetime, reason: str) -> None:
        if self.status not in EDITABLE_INPUT_STATUSES:
            raise InvalidTransition("only accepted or queued input can be superseded")
        if now.tzinfo is None:
            raise ValidationError("input timestamp must be timezone-aware")
        clean_reason = reason.strip()
        if not clean_reason:
            raise ValidationError("superseded reason is required")
        self.status = ExecutionInputStatus.SUPERSEDED
        self.superseded_reason = clean_reason
        self.operation_id = None
        self.failure_code = None
        self.updated_at = now
        self.version += 1

    def _finish_dispatch(
        self, status: ExecutionInputStatus, failure_code: str
    ) -> None:
        if self.status is not ExecutionInputStatus.DISPATCHING:
            raise InvalidTransition("only dispatching input can finish dispatch")
        clean_code = failure_code.strip()
        if not clean_code:
            raise ValidationError("failure_code is required")
        self.status = status
        self.failure_code = clean_code
        self.version += 1

    def _transition(
        self, expected: ExecutionInputStatus, target: ExecutionInputStatus
    ) -> None:
        if self.status is not expected:
            raise InvalidTransition(f"input cannot move from {self.status} to {target}")
        self.status = target
        self.version += 1
