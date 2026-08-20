from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sidera_control_plane.domain.errors import (
    InvalidTransition,
    LeaseExpired,
    StaleFencingToken,
    ValidationError,
)
from sidera_control_plane.domain.lease import AssignmentLease, LeaseStatus

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def make_lease(*, expires_at: datetime | None = None) -> AssignmentLease:
    return AssignmentLease.issue(
        lease_id=uuid4(),
        workspace_id=uuid4(),
        execution_id=uuid4(),
        worker_id=uuid4(),
        issued_at=NOW,
        expires_at=expires_at or NOW + timedelta(seconds=30),
        fencing_token=1,
        assignment_reason={"capability_match": True},
    )


def test_lease_rejects_invalid_expiry_token_and_reason() -> None:
    with pytest.raises(ValidationError):
        make_lease(expires_at=NOW)
    with pytest.raises(ValidationError):
        AssignmentLease.issue(
            lease_id=uuid4(),
            workspace_id=uuid4(),
            execution_id=uuid4(),
            worker_id=uuid4(),
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=1),
            fencing_token=0,
            assignment_reason={"reason": "test"},
        )
    with pytest.raises(ValidationError):
        AssignmentLease.issue(
            lease_id=uuid4(),
            workspace_id=uuid4(),
            execution_id=uuid4(),
            worker_id=uuid4(),
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=1),
            fencing_token=1,
            assignment_reason={},
        )


def test_lease_rejects_wrong_or_expired_fence() -> None:
    lease = make_lease(expires_at=NOW + timedelta(seconds=30))

    with pytest.raises(StaleFencingToken):
        lease.assert_owner(lease.worker_id, lease.fencing_token - 1, NOW)
    with pytest.raises(LeaseExpired):
        lease.assert_owner(lease.worker_id, lease.fencing_token, lease.expires_at)


def test_lease_renew_and_release_are_fenced_and_terminal() -> None:
    lease = make_lease()
    renewed_at = NOW + timedelta(seconds=5)
    lease.renew(
        worker_id=lease.worker_id,
        fencing_token=lease.fencing_token,
        now=renewed_at,
        ttl=timedelta(seconds=30),
    )
    assert lease.renewed_at == renewed_at
    assert lease.expires_at == renewed_at + timedelta(seconds=30)

    lease.release(
        worker_id=lease.worker_id,
        fencing_token=lease.fencing_token,
        now=renewed_at,
    )
    assert lease.status is LeaseStatus.RELEASED
    assert lease.ended_at == renewed_at
    with pytest.raises(StaleFencingToken):
        lease.assert_owner(lease.worker_id, lease.fencing_token, renewed_at)


def test_lease_renew_requires_positive_ttl() -> None:
    lease = make_lease()
    with pytest.raises(ValidationError):
        lease.renew(
            worker_id=lease.worker_id,
            fencing_token=lease.fencing_token,
            now=NOW,
            ttl=timedelta(0),
        )


def test_lease_expire_is_allowed_only_at_or_after_deadline() -> None:
    lease = make_lease()
    with pytest.raises(InvalidTransition):
        lease.expire(now=NOW + timedelta(seconds=29))

    deadline = lease.expires_at
    lease.expire(now=deadline)
    assert lease.status is LeaseStatus.EXPIRED
    assert lease.ended_at == deadline
    with pytest.raises(InvalidTransition):
        lease.expire(now=deadline)


def test_lease_revoke_is_one_way() -> None:
    lease = make_lease()
    lease.revoke(now=NOW + timedelta(seconds=1))
    assert lease.status is LeaseStatus.RELEASED
    with pytest.raises(InvalidTransition):
        lease.revoke(now=NOW + timedelta(seconds=2))
