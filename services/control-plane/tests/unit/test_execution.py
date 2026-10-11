from datetime import UTC, datetime
from uuid import UUID, uuid4

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
WORKER_ITEM_ID = UUID("73000000-0000-0000-0000-000000000001")


def make_execution(
    *,
    attempt: int = 1,
    max_attempts: int = 3,
    worker_item_id: UUID | None = None,
) -> Execution:
    return Execution.create(
        execution_id=uuid4(),
        workspace_id=uuid4(),
        task_id=uuid4(),
        worker_item_id=worker_item_id,
        lineage_id=uuid4(),
        attempt=attempt,
        max_attempts=max_attempts,
        purpose="Implement worker API",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=None,
        not_before=NOW,
    )


def test_execution_can_be_standalone_or_bound_to_a_worker_item() -> None:
    standalone = make_execution()
    bound = make_execution(worker_item_id=WORKER_ITEM_ID)

    assert standalone.worker_item_id is None
    assert bound.worker_item_id == WORKER_ITEM_ID


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


def test_execution_canonicalizes_adapter_preference_and_forbidden_policy() -> None:
    execution = Execution.create(
        execution_id=uuid4(),
        workspace_id=uuid4(),
        task_id=uuid4(),
        lineage_id=uuid4(),
        attempt=1,
        max_attempts=1,
        purpose="route execution",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=None,
        not_before=NOW,
        adapter_preferences=(" opencode ", "opencode", "codex"),
        adapter_forbidden=(" unsafe ", "unsafe"),
    )

    assert execution.adapter_preferences == ("opencode", "codex")
    assert execution.adapter_forbidden == frozenset({"unsafe"})

    with pytest.raises(ValidationError, match="forbidden"):
        Execution.create(
            execution_id=uuid4(),
            workspace_id=uuid4(),
            task_id=uuid4(),
            lineage_id=uuid4(),
            attempt=1,
            max_attempts=1,
            purpose="route execution",
            capability_requirements=frozenset({"python"}),
            pinned_worker_id=None,
            not_before=NOW,
            adapter_preferences=("opencode",),
            adapter_forbidden=("opencode",),
        )


def test_execution_pin_and_clear_are_queued_only_versioned_mutations() -> None:
    execution = make_execution()
    worker_id = uuid4()

    execution.pin_worker(worker_id)
    assert execution.pinned_worker_id == worker_id
    assert execution.version == 2

    execution.clear_worker_pin()
    assert execution.pinned_worker_id is None
    assert execution.version == 3

    execution.assign(lease_id=uuid4(), worker_id=worker_id, fencing_token=1)
    with pytest.raises(InvalidTransition, match="queued"):
        execution.pin_worker(uuid4())


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


def test_prestart_failure_requires_the_exact_leased_fence() -> None:
    execution = make_execution()
    lease_id = uuid4()
    worker_id = uuid4()
    execution.assign(lease_id=lease_id, worker_id=worker_id, fencing_token=2)

    with pytest.raises(StaleFencingToken):
        execution.fail_before_start(
            lease_id=lease_id,
            fencing_token=1,
            failure_code="acceptance_budget_exhausted",
            now=NOW,
        )

    execution.fail_before_start(
        lease_id=lease_id,
        fencing_token=2,
        failure_code="acceptance_budget_exhausted",
        now=NOW,
    )
    assert execution.status is ExecutionStatus.FAILED
    assert execution.failure_code == "acceptance_budget_exhausted"
    assert execution.started_at is None
    assert execution.active_lease_id is None
    assert execution.finished_at == NOW

    with pytest.raises(InvalidTransition):
        execution.fail_before_start(
            lease_id=lease_id,
            fencing_token=2,
            failure_code="another_failure",
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


def test_historical_observation_keeps_closed_session_closed_and_exact_sequence():
    session = ExecutionSession.start(
        session_id=uuid4(),
        workspace_id=uuid4(),
        execution_id=uuid4(),
        worker_id=uuid4(),
        adapter_session_id="agent-run:original",
        now=NOW,
    )
    session.record_activity(sequence=1, now=NOW)
    session.close(lost=False, now=NOW)
    with pytest.raises(InvalidTransition):
        session.record_activity(sequence=2, now=NOW)
    session.record_historical_observation(sequence=2, now=NOW)
    assert session.status is ExecutionSessionStatus.CLOSED
    assert session.last_sequence == 2
    version = session.version
    for sequence in (2, 4, True):
        with pytest.raises(ValidationError):
            session.record_historical_observation(sequence=sequence, now=NOW)
        assert session.last_sequence == 2
        assert session.version == version
        assert session.status is ExecutionSessionStatus.CLOSED
