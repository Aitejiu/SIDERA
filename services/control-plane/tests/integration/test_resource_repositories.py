import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def seed_resources(
    database: Database,
    workspace_id: UUID,
) -> tuple[Task, Worker, Execution, AssignmentLease, ExecutionSession]:
    task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="Repository task",
        description="Round-trip resource persistence",
        acceptance_criteria=("all resources round-trip",),
        priority=80,
        requested_by="user:test",
    )
    task.mark_ready()
    worker = Worker.register(
        worker_id=uuid4(),
        workspace_id=workspace_id,
        adapter_type="opencode",
        display_name="Repository worker",
        capabilities=frozenset({"python", "postgres"}),
        capacity=2,
        cost_per_hour_microunits=100,
        startup_latency_ms=250,
        now=NOW,
    )
    execution_id = uuid4()
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=task.id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=3,
        purpose="Implement repositories",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=worker.id,
        not_before=NOW,
    )

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.tasks.add(task)
        uow.workers.add(worker)
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.executions.add(execution)

    lease = AssignmentLease.issue(
        lease_id=uuid4(),
        workspace_id=workspace_id,
        execution_id=execution.id,
        worker_id=worker.id,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        fencing_token=1,
        assignment_reason={"capability_match": True},
    )
    execution_session = ExecutionSession.start(
        session_id=uuid4(),
        workspace_id=workspace_id,
        execution_id=execution.id,
        worker_id=worker.id,
        adapter_session_id="adapter-session-1",
        now=NOW,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.leases.add(lease)
        uow.sessions.add(execution_session)
    return task, worker, execution, lease, execution_session


def test_resource_repositories_round_trip_and_save(
    database: Database,
    workspace_id: UUID,
) -> None:
    task, worker, execution, lease, _execution_session = seed_resources(
        database, workspace_id
    )

    with SqlAlchemyUnitOfWork(database) as uow:
        stored_worker = uow.workers.get(worker.id, workspace_id)
        stored_execution = uow.executions.get(execution.id, workspace_id)
        stored_lease = uow.leases.get(lease.id, workspace_id)
        stored_session = uow.sessions.get_active_for_execution(execution.id)

    assert stored_worker is not None
    assert stored_worker.capabilities == frozenset({"python", "postgres"})
    assert stored_worker.desired_state.value == "active"
    assert stored_worker.last_heartbeat_at == NOW
    assert stored_execution is not None
    assert stored_execution.capability_requirements == frozenset({"python"})
    assert stored_execution.pinned_worker_id == worker.id
    assert stored_lease is not None
    assert stored_lease.assignment_reason == {"capability_match": True}
    assert stored_lease.expires_at == NOW + timedelta(minutes=5)
    assert stored_session is not None
    assert stored_session.adapter_session_id == "adapter-session-1"
    assert stored_session.input_mode == "unsupported"
    assert task.status.value == "ready"

    with SqlAlchemyUnitOfWork(database) as uow:
        stored_worker = uow.workers.get(worker.id, workspace_id)
        assert stored_worker is not None
        previous_version = stored_worker.version
        stored_worker.heartbeat(healthy=True, now=NOW + timedelta(seconds=1))
        uow.workers.save(stored_worker, previous_version)

        stored_execution = uow.executions.get(execution.id, workspace_id)
        assert stored_execution is not None
        previous_version = stored_execution.version
        stored_execution.cancel(now=NOW + timedelta(seconds=1))
        uow.executions.save(stored_execution, previous_version)

        stored_lease = uow.leases.get(lease.id, workspace_id)
        assert stored_lease is not None
        previous_version = stored_lease.version
        stored_lease.revoke(now=NOW + timedelta(seconds=1))
        uow.leases.save(stored_lease, previous_version)

        stored_session = uow.sessions.get_active_for_execution(execution.id)
        assert stored_session is not None
        previous_version = stored_session.version
        stored_session.close(lost=False, now=NOW + timedelta(seconds=1))
        uow.sessions.save(stored_session, previous_version)

    with SqlAlchemyUnitOfWork(database) as uow:
        assert uow.executions.attempt_exists(execution.lineage_id, execution.attempt)
        assert not uow.executions.has_live_for_task(task.id)
        assert uow.workers.lock_candidates(
            workspace_id=workspace_id,
            required_capabilities=frozenset({"python"}),
            pinned_worker_id=worker.id,
            heartbeat_cutoff=NOW - timedelta(minutes=1),
        )
        assert not uow.workers.lock_candidates(
            workspace_id=workspace_id,
            required_capabilities=frozenset({"rust"}),
            pinned_worker_id=None,
            heartbeat_cutoff=NOW - timedelta(minutes=1),
        )


def test_repository_save_rejects_stale_worker_version(
    database: Database,
    workspace_id: UUID,
) -> None:
    _, worker, _, _, _ = seed_resources(database, workspace_id)

    with SqlAlchemyUnitOfWork(database) as uow:
        stale_worker = uow.workers.get(worker.id, workspace_id)
    assert stale_worker is not None

    with SqlAlchemyUnitOfWork(database) as uow:
        current_worker = uow.workers.get(worker.id, workspace_id)
        assert current_worker is not None
        previous_version = current_worker.version
        current_worker.heartbeat(healthy=True, now=NOW + timedelta(seconds=1))
        uow.workers.save(current_worker, previous_version)

    stale_worker.heartbeat(healthy=True, now=NOW + timedelta(seconds=2))
    with pytest.raises(VersionConflict, match="expected version"), SqlAlchemyUnitOfWork(
        database
    ) as uow:
        uow.workers.save(stale_worker, 1)


def test_schedulable_claim_uses_skip_locked(
    database: Database,
    workspace_id: UUID,
) -> None:
    _, _, execution, _, _ = seed_resources(database, workspace_id)
    barrier = threading.Barrier(2)
    after_claim = threading.Barrier(2)

    def claim() -> tuple[UUID, ...]:
        with SqlAlchemyUnitOfWork(database) as uow:
            barrier.wait(timeout=5)
            claimed = tuple(
                item.id for item in uow.executions.claim_schedulable(now=NOW, limit=10)
            )
            after_claim.wait(timeout=5)
            return claimed

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = tuple(pool.map(lambda _index: claim(), range(2)))

    assert sorted(len(items) for items in claims) == [0, 1]
    assert execution.id in {item for claim_ids in claims for item in claim_ids}


def test_expired_claim_uses_skip_locked(
    database: Database,
    workspace_id: UUID,
) -> None:
    _, _, execution, lease, _ = seed_resources(database, workspace_id)
    with SqlAlchemyUnitOfWork(database) as uow:
        stored_lease = uow.leases.get(lease.id, workspace_id)
        assert stored_lease is not None
        previous_version = stored_lease.version
        stored_lease.revoke(now=NOW - timedelta(minutes=2))
        uow.leases.save(stored_lease, previous_version)
        expired = AssignmentLease.issue(
            lease_id=uuid4(),
            workspace_id=workspace_id,
            execution_id=execution.id,
            worker_id=lease.worker_id,
            issued_at=NOW - timedelta(minutes=3),
            expires_at=NOW - timedelta(minutes=2),
            fencing_token=2,
            assignment_reason={"expired": True},
        )
        uow.leases.add(expired)

    barrier = threading.Barrier(2)
    after_claim = threading.Barrier(2)

    def claim() -> tuple[UUID, ...]:
        with SqlAlchemyUnitOfWork(database) as uow:
            barrier.wait(timeout=5)
            claimed = tuple(
                item.id for item in uow.leases.claim_expired(now=NOW, limit=10)
            )
            after_claim.wait(timeout=5)
            return claimed

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = tuple(pool.map(lambda _index: claim(), range(2)))

    assert sorted(len(items) for items in claims) == [0, 1]
