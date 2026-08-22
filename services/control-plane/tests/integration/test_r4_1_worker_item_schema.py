from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    PlanSnapshotModel,
    TaskModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
    WorkspaceModel,
)

pytestmark = pytest.mark.integration


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _task(task_id, workspace_id, title: str) -> TaskModel:
    return TaskModel(
        id=task_id,
        workspace_id=workspace_id,
        title=title,
        description=title,
        acceptance_criteria=["constraints hold"],
        status="pending",
        priority=50,
        requested_by="schema-test",
        version=1,
    )


def _worker_item(item_id, workspace_id, root_task_id, stable_key: str) -> WorkerItemModel:
    return WorkerItemModel(
        id=item_id,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        stable_key=stable_key,
        title="Callback implementer",
        responsibility="Keep callback behavior correct.",
        status="active",
        context_version=1,
        acknowledged_context_version=1,
        derived_from_worker_item_ids=[],
        version=1,
    )


def _placement(snapshot_id, workspace_id, root_task_id, worker_item_id, key: str):
    return WorkerItemPlacementModel(
        plan_snapshot_id=snapshot_id,
        plan_generation=1,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        worker_item_id=worker_item_id,
        worker_item_key=key,
        subtask_key="callback-idempotency",
        subtask_task_id=None,
        role="implementer",
        capability_requirements=["python"],
        worker_policy_ref=None,
        configuration_ref=None,
    )


def _execution(execution_id, workspace_id, task_id, worker_item_id):
    return ExecutionModel(
        id=execution_id,
        workspace_id=workspace_id,
        task_id=task_id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=1,
        purpose="Schema execution",
        status="queued",
        capability_requirements=["python"],
        pinned_worker_id=None,
        assigned_worker_id=None,
        active_lease_id=None,
        worker_item_id=worker_item_id,
        last_fencing_token=0,
        not_before=NOW,
        failure_code=None,
        started_at=None,
        finished_at=None,
        version=1,
        stop_requested=False,
        source_execution_id=None,
        source_session_id=None,
    )


def test_r4_1_schema_creates_worker_item_tables_and_nullable_execution_link(
    database: Database,
) -> None:
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        assert {
            "worker_items",
            "worker_item_placements",
        }.issubset(inspector.get_table_names())
        execution_worker_item = next(
            column
            for column in inspector.get_columns("executions")
            if column["name"] == "worker_item_id"
        )
        assert execution_worker_item["nullable"] is True
        assert set(
            inspector.get_pk_constraint("worker_item_placements")["constrained_columns"]
        ) == {"plan_snapshot_id", "worker_item_id"}


def test_r4_1_schema_rejects_duplicate_identity_invalid_context_and_duplicate_placement(
    database: Database,
    workspace_id,
) -> None:
    root_task_id = uuid4()
    snapshot_id = uuid4()
    first_item_id = uuid4()
    second_item_id = uuid4()

    with database.session() as session:
        session.add(_task(root_task_id, workspace_id, "WorkerItem schema task"))
        session.flush()
        session.add(
            PlanSnapshotModel(
                id=snapshot_id,
                schema_version=2,
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                generation=1,
                parent_snapshot_id=None,
                source_type="test",
                source_ref="schema-test",
                title="WorkerItem schema plan",
                goal_summary="Verify WorkerItem persistence constraints.",
                payload={"schema_version": 2},
                published_by="schema-test",
                published_at=NOW,
            )
        )
        session.add(
            _worker_item(first_item_id, workspace_id, root_task_id, "stable-a")
        )
        session.add(
            _worker_item(second_item_id, workspace_id, root_task_id, "stable-b")
        )
        session.commit()

    with database.session() as session:
        session.add(_worker_item(uuid4(), workspace_id, root_task_id, "stable-a"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with database.session() as session:
        invalid = _worker_item(uuid4(), workspace_id, root_task_id, "invalid-context")
        invalid.acknowledged_context_version = 2
        session.add(invalid)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with database.session() as session:
        session.add(
            _placement(snapshot_id, workspace_id, root_task_id, first_item_id, "same-key")
        )
        session.commit()

    with database.session() as session:
        session.add(
            _placement(snapshot_id, workspace_id, root_task_id, second_item_id, "same-key")
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_r4_1_schema_isolates_cross_workspace_execution_and_keeps_independent_execution_valid(
    database: Database,
    workspace_id,
) -> None:
    other_workspace_id = uuid4()
    root_task_id = uuid4()
    other_root_task_id = uuid4()
    other_item_id = uuid4()

    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=other_workspace_id,
                name="Other schema workspace",
                status="active",
                version=1,
            )
        )
        session.flush()
        session.add(_task(root_task_id, workspace_id, "Primary task"))
        session.add(_task(other_root_task_id, other_workspace_id, "Other task"))
        session.flush()
        session.add(
            _worker_item(
                other_item_id,
                other_workspace_id,
                other_root_task_id,
                "other-worker",
            )
        )
        session.commit()

    with database.session() as session:
        session.add(_execution(uuid4(), workspace_id, root_task_id, other_item_id))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with database.session() as session:
        session.add(_execution(uuid4(), workspace_id, root_task_id, None))
        session.commit()
