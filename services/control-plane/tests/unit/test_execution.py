from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sidera_control_plane.domain.errors import (
    InvalidTransition,
    StaleFencingToken,
    ValidationError,
)
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSession,
    ExecutionSessionStatus,
    ExecutionStatus,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def make_execution(*, attempt: int = 1, max_attempts: int = 3) -> Execution:
    return Execution.create(
        execution_id=uuid4(),
        workspace_id=uuid4(),
        task_id=uuid4(),
        lineage_id=uuid4(),
        attempt=attempt,
        max_attempts=max_attempts,
        purpose="Implement worker API",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=None,
        not_before=NOW,
    )


def test_execution_rejects_invalid_attempts_requirements_and_time() -> None:
    with pytest.raises(ValidationError):
        make_execution(attempt=0)
    with pytest.raises(ValidationError):
        make_execution(attempt=2, max_attempts=1)
    with pytest.raises(ValidationError):
        Execution.create(
            execution_id=uuid4(),
            workspace_id=uuid4(),
            task_id=uuid4(),
            lineage_id=uuid4(),
            attempt=1,
            max_attempts=1,
            purpose="purpose",
            capability_requirements=frozenset(),
            pinned_worker_id=None,
            not_before=NOW,
        )
    with pytest.raises(ValidationError):
        Execution.create(
            execution_id=uuid4(),
            workspace_id=uuid4(),
            task_id=uuid4(),
            lineage_id=uuid4(),
            attempt=1,
            max_attempts=1,
            purpose="purpose",
            capability_requirements=frozenset({"python"}),
            pinned_worker_id=None,
            not_before=datetime.fromisoformat("2026-08-20T12:00:00"),
        )


def test_execution_lifecycle_requires_order_and_exact_fence() -> None:
    execution = make_execution()
    lease_id = uuid4()
    worker_id = uuid4()

    execution.assign(lease_id=lease_id, worker_id=worker_id, fencing_token=1)
    assert execution.status is ExecutionStatus.LEASED
    assert execution.version == 2

    with pytest.raises(StaleFencingToken):
        execution.start(lease_id=lease_id, fencing_token=2, now=NOW)

    execution.start(lease_id=lease_id, fencing_token=1, now=NOW)
    execution.succeed(lease_id=lease_id, fencing_token=1, now=NOW)
    assert execution.status is ExecutionStatus.SUCCEEDED
    assert execution.active_lease_id is None
    assert execution.finished_at == NOW

    with pytest.raises(InvalidTransition):
        execution.assign(lease_id=uuid4(), worker_id=worker_id, fencing_token=2)


def test_execution_never_reopens_after_lost() -> None:
    execution = make_execution()
    lease_id = uuid4()
    worker_id = uuid4()
    execution.assign(lease_id=lease_id, worker_id=worker_id, fencing_token=1)
    execution.mark_lost(
        lease_id=lease_id,
        fencing_token=1,
        failure_code="lease_expired",
        now=NOW,
    )

    with pytest.raises(InvalidTransition):
        execution.assign(lease_id=uuid4(), worker_id=worker_id, fencing_token=2)


def test_execution_cancel_accepts_non_terminal_states_only() -> None:
    queued = make_execution()
    queued.cancel(now=NOW)
    assert queued.status is ExecutionStatus.CANCELED

    leased = make_execution()
    leased.assign(lease_id=uuid4(), worker_id=uuid4(), fencing_token=1)
    leased.cancel(now=NOW)
    assert leased.status is ExecutionStatus.CANCELED

    with pytest.raises(InvalidTransition):
        queued.cancel(now=NOW)


def test_execution_fail_requires_a_code_and_running_state() -> None:
    execution = make_execution()
    with pytest.raises(InvalidTransition):
        execution.fail(
            lease_id=uuid4(),
            fencing_token=1,
            failure_code="failure",
            now=NOW,
        )

    lease_id = uuid4()
    execution.assign(lease_id=lease_id, worker_id=uuid4(), fencing_token=1)
    execution.start(lease_id=lease_id, fencing_token=1, now=NOW)
    with pytest.raises(ValidationError):
        execution.fail(
            lease_id=lease_id,
            fencing_token=1,
            failure_code=" ",
            now=NOW,
        )


def test_session_activity_is_strictly_ordered_and_closes_once() -> None:
    session = ExecutionSession.start(
        session_id=uuid4(),
        workspace_id=uuid4(),
        execution_id=uuid4(),
        worker_id=uuid4(),
        adapter_session_id="adapter-session-1",
        now=NOW,
    )
    assert session.status is ExecutionSessionStatus.ACTIVE
    session.record_activity(sequence=1, now=NOW)
    assert session.last_sequence == 1

    with pytest.raises(ValidationError):
        session.record_activity(sequence=3, now=NOW)

    session.close(lost=False, now=NOW)
    assert session.status is ExecutionSessionStatus.CLOSED
    with pytest.raises(InvalidTransition):
        session.close(lost=True, now=NOW)


def test_session_requires_adapter_session_id() -> None:
    with pytest.raises(ValidationError):
        ExecutionSession.start(
            session_id=uuid4(),
            workspace_id=uuid4(),
            execution_id=uuid4(),
            worker_id=uuid4(),
            adapter_session_id=" ",
            now=NOW,
        )
