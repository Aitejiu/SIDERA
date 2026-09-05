from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from sidera_control_plane.domain.collaboration import CollaborationGateMode
from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.execution_requests import (
    DependencyEnforcement,
    ExecutionRequestStatus,
)
from sidera_control_plane.persistence.execution_request_repository import (
    ExecutionRequestRepository,
)
from sidera_control_plane.persistence.models import ExecutionRequestDependencyModel
from tests.integration.test_r5_1_execution_request_schema import (
    NOW,
    WORKSPACE_A,
    request_row,
    seed_execution_request_facts,
)

pytestmark = pytest.mark.integration


def test_execution_request_round_trip_save_and_dependency_queries(
    database, workspace_id: UUID
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    with database.session() as session:
        upstream = request_row(ids, worker_key="upstream")
        downstream = request_row(ids, worker_key="downstream")
        downstream.adapter_forbidden = ["unsafe"]
        downstream.collaboration_gate_mode = (
            CollaborationGateMode.RELEASE_REQUIRED.value
        )
        downstream.not_before = NOW + timedelta(seconds=5)
        session.add_all((upstream, downstream))
        session.flush()
        session.add(
            ExecutionRequestDependencyModel(
                workspace_id=WORKSPACE_A,
                downstream_request_id=downstream.id,
                upstream_request_id=upstream.id,
                enforcement=DependencyEnforcement.HARD.value,
                required_artifact_refs=["artifact_type:diff"],
                required_evidence_refs=["acceptance:test"],
            )
        )
        session.commit()

    with database.session() as session:
        repository = ExecutionRequestRepository(session)
        stored = repository.get(downstream.id, workspace_id, for_update=True)
        assert stored is not None
        assert stored.expected_outputs == ("diff",)
        assert stored.status is ExecutionRequestStatus.BLOCKED
        assert stored.adapter_forbidden == frozenset({"unsafe"})
        assert stored.collaboration_gate_mode is CollaborationGateMode.RELEASE_REQUIRED
        dependencies = repository.list_dependencies(downstream.id, workspace_id)
        assert len(dependencies) == 1
        assert dependencies[0].upstream_request_id == upstream.id
        assert dependencies[0].is_blocking
        assert tuple(
            item.id
            for item in repository.list_downstream_for_upstream(
                upstream.id, workspace_id
            )
        ) == (downstream.id,)

        previous_version = stored.version
        stored.collaboration_gate_mode = CollaborationGateMode.UNMANAGED
        stored.mark_ready(now=NOW)
        repository.save(stored, previous_version)
        session.commit()

    with database.session() as session:
        persisted = ExecutionRequestRepository(session).get(downstream.id, workspace_id)
        assert persisted is not None
        assert (
            persisted.collaboration_gate_mode is CollaborationGateMode.RELEASE_REQUIRED
        )

    with database.session() as session:
        repository = ExecutionRequestRepository(session)
        stored = repository.get(downstream.id, workspace_id, for_update=True)
        assert stored is not None
        previous_version = stored.version - 1
        with pytest.raises(VersionConflict):
            repository.save(stored, previous_version)


def test_ready_claim_uses_skip_locked_and_deterministic_not_before_order(
    database, workspace_id: UUID
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    with database.session() as session:
        first = request_row(ids, worker_key="first")
        second = request_row(ids, worker_key="second")
        first.status = "ready"
        second.status = "ready"
        first.not_before = NOW
        second.not_before = NOW + timedelta(seconds=1)
        session.add_all((first, second))
        session.commit()

    with database.session() as session_one:
        repository_one = ExecutionRequestRepository(session_one)
        claimed = repository_one.claim_ready(now=NOW, limit=1)
        assert tuple(item.worker_item_key for item in claimed) == ("first",)
        with Session(database.engine) as session_two:
            assert (
                ExecutionRequestRepository(session_two).claim_ready(now=NOW, limit=1)
                == ()
            )
        session_one.rollback()

    with database.session() as session:
        claimed = ExecutionRequestRepository(session).claim_ready(
            now=NOW + timedelta(seconds=2), limit=10
        )
        assert tuple(item.worker_item_key for item in claimed) == ("first", "second")


def test_ready_claim_excludes_future_not_before(database, workspace_id: UUID) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    future = request_row(ids, worker_key="future")
    future.status = "ready"
    future.not_before = NOW + timedelta(minutes=1)
    with database.session() as session:
        session.add(future)
        session.commit()

    with database.session() as session:
        assert ExecutionRequestRepository(session).claim_ready(now=NOW, limit=10) == ()


def test_get_many_returns_requests_in_stable_identity_order(
    database, workspace_id: UUID
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    request_ids = (
        UUID("ffffffff-ffff-ffff-ffff-fffffffffff1"),
        UUID("00000000-0000-0000-0000-000000000010"),
    )
    with database.session() as session:
        session.add_all(
            (
                request_row(ids, request_id=request_ids[0], worker_key="later"),
                request_row(ids, request_id=request_ids[1], worker_key="earlier"),
            )
        )
        session.commit()

    with database.session() as session:
        stored = ExecutionRequestRepository(session).get_many(
            tuple(reversed(request_ids)), workspace_id, for_update=True
        )
        assert tuple(item.id for item in stored) == tuple(sorted(request_ids, key=str))
