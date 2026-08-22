from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sidera_control_plane.domain.errors import (
    InvalidTransition,
    LeaseExpired,
    StaleFencingToken,
    ValidationError,
)


class LeaseStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


@dataclass(slots=True)
class AssignmentLease:
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    worker_id: UUID
    status: LeaseStatus
    issued_at: datetime
    expires_at: datetime
    renewed_at: datetime | None
    ended_at: datetime | None
    fencing_token: int
    assignment_reason: dict[str, object]
    version: int

    @classmethod
    def issue(
        cls,
        *,
        lease_id: UUID,
        workspace_id: UUID,
        execution_id: UUID,
        worker_id: UUID,
        issued_at: datetime,
        expires_at: datetime,
        fencing_token: int,
        assignment_reason: dict[str, object],
    ) -> "AssignmentLease":
        if expires_at <= issued_at:
            raise ValidationError("lease expiry must be after issue time")
        if fencing_token < 1 or not assignment_reason:
            raise ValidationError("lease requires a positive token and assignment reason")
        return cls(
            id=lease_id,
            workspace_id=workspace_id,
            execution_id=execution_id,
            worker_id=worker_id,
            status=LeaseStatus.ACTIVE,
            issued_at=issued_at,
            expires_at=expires_at,
            renewed_at=None,
            ended_at=None,
            fencing_token=fencing_token,
            assignment_reason=dict(assignment_reason),
            version=1,
        )

    def assert_owner(self, worker_id: UUID, fencing_token: int, now: datetime) -> None:
        if self.status is not LeaseStatus.ACTIVE:
            raise StaleFencingToken("lease is not active")
        if self.worker_id != worker_id or self.fencing_token != fencing_token:
            raise StaleFencingToken("worker or fencing token does not own the lease")
        if now >= self.expires_at:
            raise LeaseExpired("lease has expired")

    def renew(
        self,
        *,
        worker_id: UUID,
        fencing_token: int,
        now: datetime,
        ttl: timedelta,
    ) -> None:
        self.assert_owner(worker_id, fencing_token, now)
        if ttl <= timedelta(0):
            raise ValidationError("lease ttl must be positive")
        self.renewed_at = now
        self.expires_at = now + ttl
        self.version += 1

    def release(self, *, worker_id: UUID, fencing_token: int, now: datetime) -> None:
        self.assert_owner(worker_id, fencing_token, now)
        self.status = LeaseStatus.RELEASED
        self.ended_at = now
        self.version += 1

    def revoke(self, *, now: datetime) -> None:
        if self.status is not LeaseStatus.ACTIVE:
            raise InvalidTransition("only an active lease can be revoked")
        self.status = LeaseStatus.RELEASED
        self.ended_at = now
        self.version += 1

    def expire(self, *, now: datetime) -> None:
        if self.status is not LeaseStatus.ACTIVE:
            raise InvalidTransition("only an active lease can expire")
        if now < self.expires_at:
            raise InvalidTransition("lease has not expired")
        self.status = LeaseStatus.EXPIRED
        self.ended_at = now
        self.version += 1
