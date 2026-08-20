from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sidera_control_plane.application.reconciler import Reconciler
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.lease import AssignmentLease

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class RecordingLeaseStore:
    def __init__(self, lease: AssignmentLease, calls: list[str]) -> None:
        self._lease = lease
        self._calls = calls

    def get(self, _lease_id, _workspace_id, *, for_update=False):
        self._calls.append("lease_lock" if for_update else "lease_read")
        return self._lease


class RecordingExecutionRepository:
    def __init__(self, execution: Execution, calls: list[str]) -> None:
        self._execution = execution
        self._calls = calls

    def get(self, _execution_id, _workspace_id, *, for_update=False):
        assert for_update is True
        self._calls.append("execution_lock")
        return self._execution


class RecordingUnitOfWork:
    def __init__(self, lease: AssignmentLease, execution: Execution) -> None:
        calls: list[str] = []
        self.calls = calls
        self.leases = RecordingLeaseStore(lease, calls)
        self.executions = RecordingExecutionRepository(execution, calls)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_expired_lease_revalidation_locks_execution_before_lease() -> None:
    workspace_id = uuid4()
    execution_id = uuid4()
    lease_id = uuid4()
    other_lease_id = uuid4()
    task_id = uuid4()
    worker_id = uuid4()
    lease = AssignmentLease.issue(
        lease_id=lease_id,
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW - timedelta(seconds=1),
        fencing_token=1,
        assignment_reason={"policy": "test"},
    )
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=task_id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=1,
        purpose="lock ordering",
        capability_requirements=frozenset({"code"}),
        pinned_worker_id=worker_id,
        not_before=NOW,
    )
    execution.assign(
        lease_id=other_lease_id,
        worker_id=worker_id,
        fencing_token=1,
    )
    uow = RecordingUnitOfWork(lease, execution)
    reconciler = Reconciler(
        uow_factory=cast(Any, lambda: uow),
        clock=lambda: NOW,
    )

    result = reconciler._reconcile_expired_lease(
        lease_id=lease_id,
        workspace_id=workspace_id,
        now=NOW,
    )

    assert result.expired is False
    assert uow.calls == ["lease_read", "execution_lock", "lease_lock"]
