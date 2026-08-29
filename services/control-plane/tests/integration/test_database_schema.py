from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import inspect, update
from sqlalchemy.exc import IntegrityError

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    ExecutionModel,
    ExecutionSessionModel,
    ExecutionWorkspaceTransferModel,
    HandoffContextBundleModel,
    HandoffProposalModel,
    HandoffRecoveryDecisionModel,
    HandoffTransitionModel,
    TaskModel,
    WorkerModel,
    WorkspaceModel,
)

pytestmark = pytest.mark.integration


def test_foundation_migration_creates_authoritative_tables(database: Database) -> None:
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())

    assert {
        "workspaces",
        "tasks",
        "commands",
        "events",
        "outbox_messages",
        "workers",
        "executions",
        "execution_sessions",
        "assignment_leases",
        "worker_items",
        "worker_item_placements",
    } <= tables

    with database.engine.connect() as connection:
        inspector = inspect(connection)
        worker_columns = {item["name"] for item in inspector.get_columns("workers")}
        worker_constraints = {
            item["name"] for item in inspector.get_check_constraints("workers")
        }
        lease_indexes = {
            item["name"] for item in inspector.get_indexes("assignment_leases")
        }
        session_indexes = {
            item["name"] for item in inspector.get_indexes("execution_sessions")
        }

    assert "desired_state" in worker_columns
    assert "ck_workers_desired_state" in worker_constraints
    assert "uq_assignment_leases_active_execution" in lease_indexes
    assert "uq_execution_sessions_active_execution" in session_indexes


def test_r6_2_migration_adds_handoff_tables_and_lineage_columns(
    database: Database,
) -> None:
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        assert {
            HandoffProposalModel.__tablename__,
            HandoffTransitionModel.__tablename__,
            HandoffContextBundleModel.__tablename__,
            HandoffRecoveryDecisionModel.__tablename__,
            ExecutionWorkspaceTransferModel.__tablename__,
        } <= tables
        assert {"handoff_transition_id"} <= {
            item["name"] for item in inspector.get_columns("executions")
        }
        assert {"handoff_transition_id"} <= {
            item["name"] for item in inspector.get_columns("execution_requests")
        }
        assert {
            "source_input_id",
            "handoff_transition_id",
        } <= {item["name"] for item in inspector.get_columns("execution_inputs")}


def test_r2_schema_enforces_capacity_fencing_and_workspace_ownership(
    database: Database,
    workspace_id,
) -> None:
    workspace_b_id = uuid4()
    task_id = uuid4()
    worker_a_id = uuid4()
    worker_b_id = uuid4()
    execution_id = uuid4()
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=workspace_b_id,
                name="Workspace B",
                status="active",
                version=1,
            )
        )
        session.commit()
        session.add(
            TaskModel(
                id=task_id,
                workspace_id=workspace_id,
                title="Schema task",
                description="Schema constraints",
                acceptance_criteria=["constraints hold"],
                status="pending",
                priority=50,
                requested_by="test",
                version=1,
            )
        )
        session.add_all(
            [
                WorkerModel(
                    id=worker_a_id,
                    workspace_id=workspace_id,
                    adapter_type="opencode",
                    display_name="Worker A",
                    status="idle",
                    capabilities=["python"],
                    capacity=1,
                    current_load=0,
                    cost_per_hour_microunits=1,
                    startup_latency_ms=1,
                    last_heartbeat_at=now,
                    version=1,
                ),
                WorkerModel(
                    id=worker_b_id,
                    workspace_id=workspace_b_id,
                    adapter_type="opencode",
                    display_name="Worker B",
                    status="idle",
                    capabilities=["python"],
                    capacity=1,
                    current_load=0,
                    cost_per_hour_microunits=1,
                    startup_latency_ms=1,
                    last_heartbeat_at=now,
                    version=1,
                ),
            ]
        )
        session.commit()

    with database.session() as session:
        session.add(
            ExecutionModel(
                id=execution_id,
                workspace_id=workspace_id,
                task_id=task_id,
                lineage_id=execution_id,
                attempt=1,
                max_attempts=2,
                purpose="Schema execution",
                status="queued",
                capability_requirements=["python"],
                pinned_worker_id=None,
                assigned_worker_id=worker_a_id,
                active_lease_id=None,
                last_fencing_token=0,
                not_before=now,
                failure_code=None,
                started_at=None,
                finished_at=None,
                version=1,
            )
        )
        session.commit()

    with database.session() as session:
        session.add(
            AssignmentLeaseModel(
                id=uuid4(),
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_a_id,
                status="active",
                issued_at=now,
                expires_at=now + timedelta(minutes=1),
                renewed_at=None,
                ended_at=None,
                fencing_token=1,
                assignment_reason={"test": True},
                version=1,
            )
        )
        session.commit()

    with database.session() as session:
        session.add(
            AssignmentLeaseModel(
                id=uuid4(),
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_a_id,
                status="active",
                issued_at=now,
                expires_at=now + timedelta(minutes=1),
                renewed_at=None,
                ended_at=None,
                fencing_token=2,
                assignment_reason={"test": True},
                version=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with database.session() as session:
        session.add(
            ExecutionSessionModel(
                id=uuid4(),
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_a_id,
                adapter_session_id="session-1",
                status="active",
                last_sequence=0,
                last_activity_at=now,
                input_mode="unsupported",
                version=1,
            )
        )
        session.commit()

    with database.session() as session:
        session.add(
            ExecutionSessionModel(
                id=uuid4(),
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_a_id,
                adapter_session_id="session-2",
                status="active",
                last_sequence=0,
                last_activity_at=now,
                input_mode="unsupported",
                version=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with database.session() as session, pytest.raises(IntegrityError):
        session.execute(
            update(WorkerModel)
            .where(WorkerModel.id == worker_a_id)
            .values(current_load=2)
        )

    with database.session() as session:
        session.add(
            AssignmentLeaseModel(
                id=uuid4(),
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_a_id,
                status="released",
                issued_at=now,
                expires_at=now + timedelta(minutes=1),
                renewed_at=None,
                ended_at=now,
                fencing_token=0,
                assignment_reason={"test": True},
                version=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with database.session() as session:
        session.add(
            ExecutionModel(
                id=uuid4(),
                workspace_id=workspace_id,
                task_id=task_id,
                lineage_id=uuid4(),
                attempt=1,
                max_attempts=1,
                purpose="Cross workspace execution",
                status="queued",
                capability_requirements=["python"],
                pinned_worker_id=None,
                assigned_worker_id=worker_b_id,
                active_lease_id=None,
                last_fencing_token=0,
                not_before=now,
                failure_code=None,
                started_at=None,
                finished_at=None,
                version=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
