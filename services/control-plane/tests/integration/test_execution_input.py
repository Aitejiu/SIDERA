from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.execution_commands import (
    ExecutionInputCommandHandler,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import InvalidTransition
from sidera_control_plane.domain.plans import (
    TaskPlanSnapshot,
    WorkerSlot,
    WorkItem,
    WorkItemKind,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    CommandModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionSessionModel,
    OutboxMessageModel,
    PlanSnapshotModel,
    TaskModel,
    WorkerModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _seed_session(database: Database, workspace_id: UUID) -> dict[str, UUID]:
    task_id = uuid4()
    plan_snapshot_id = uuid4()
    worker_id = uuid4()
    execution_id = uuid4()
    lease_id = uuid4()
    session_id = uuid4()
    with database.session() as db_session:
        snapshot = TaskPlanSnapshot.create(
            snapshot_id=plan_snapshot_id,
            schema_version=1,
            workspace_id=workspace_id,
            root_task_id=task_id,
            generation=1,
            parent_snapshot_id=None,
            source_type="test",
            source_ref="integration:input",
            title="Input task",
            goal_summary="Accept direct user input",
            acceptance_refs=("input is delivered",),
            work_items=(
                WorkItem(
                    key="input",
                    task_id=task_id,
                    kind=WorkItemKind.CHILD,
                    title="Input task",
                    goal_summary="Accept direct user input",
                    acceptance_refs=("input is delivered",),
                    milestone_key=None,
                    worker_slot_key="input",
                    expected_outputs=(),
                    source_refs=(),
                ),
            ),
            worker_slots=(
                WorkerSlot(
                    key="input",
                    role="implementation",
                    capability_requirements=("python",),
                    worker_policy_ref=None,
                    configuration_ref=None,
                    concurrency_limit=1,
                ),
            ),
            milestones=(),
            dependencies=(),
            published_by="integration",
            published_at=NOW,
        )
        db_session.add(
            TaskModel(
                id=task_id,
                workspace_id=workspace_id,
                title="Input task",
                description="Accept direct user input",
                acceptance_criteria=["input is delivered"],
                status="running",
                priority=50,
                requested_by="integration",
                version=1,
            )
        )
        db_session.flush()
        db_session.add(
            PlanSnapshotModel(
                id=snapshot.id,
                schema_version=snapshot.schema_version,
                workspace_id=snapshot.workspace_id,
                root_task_id=snapshot.root_task_id,
                generation=snapshot.generation,
                parent_snapshot_id=None,
                source_type=snapshot.source_type,
                source_ref=snapshot.source_ref,
                title=snapshot.title,
                goal_summary=snapshot.goal_summary,
                payload=snapshot.to_payload(),
                published_by=snapshot.published_by,
                published_at=snapshot.published_at,
            )
        )
        db_session.add(
            WorkerModel(
                id=worker_id,
                workspace_id=workspace_id,
                adapter_type="fake",
                display_name="Input Worker",
                status="busy",
                desired_state="active",
                capabilities=["python"],
                capacity=1,
                current_load=1,
                cost_per_hour_microunits=1,
                startup_latency_ms=1,
                last_heartbeat_at=NOW,
                version=1,
            )
        )
        db_session.commit()
    with database.session() as db_session:
        execution = ExecutionModel(
            id=execution_id,
            workspace_id=workspace_id,
            task_id=task_id,
            lineage_id=execution_id,
            attempt=1,
            max_attempts=2,
            purpose="Receive input",
            status="running",
            capability_requirements=["python"],
            pinned_worker_id=None,
            assigned_worker_id=worker_id,
            active_lease_id=None,
            last_fencing_token=1,
            not_before=NOW,
            failure_code=None,
            started_at=NOW,
            finished_at=None,
            version=1,
        )
        db_session.add(execution)
        db_session.flush([execution])
        db_session.add(
            AssignmentLeaseModel(
                id=lease_id,
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_id,
                status="active",
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                renewed_at=None,
                ended_at=None,
                fencing_token=1,
                assignment_reason={"source": "integration"},
                version=1,
            )
        )
        db_session.add(
            ExecutionSessionModel(
                id=session_id,
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_id,
                adapter_session_id="input-session",
                status="active",
                last_sequence=0,
                last_activity_at=NOW,
                input_mode="queued",
                adapter_module_id=None,
                adapter_config_version=None,
                workspace_module_id=None,
                workspace_config_version=None,
                capability_snapshot=None,
                process_ref="fake://input-session",
                workspace_ref="fake://input-workspace",
                version=1,
            )
        )
        db_session.flush()
        db_session.execute(
            update(ExecutionModel)
            .where(
                ExecutionModel.id == execution_id,
                ExecutionModel.workspace_id == workspace_id,
            )
            .values(active_lease_id=lease_id)
        )
        db_session.commit()
    return {
        "task_id": task_id,
        "plan_snapshot_id": plan_snapshot_id,
        "worker_id": worker_id,
        "execution_id": execution_id,
        "lease_id": lease_id,
        "session_id": session_id,
    }


def _envelope(
    workspace_id: UUID,
    session_id: UUID,
    execution_id: UUID,
    command_type: str,
    client_key: str,
    *,
    payload: dict[str, object] | None = None,
    context: dict[str, object] | None = None,
) -> CommandEnvelope:
    command_payload: dict[str, object] = {
        "execution_id": str(execution_id),
        "client_idempotency_key": client_key,
        **(payload or {}),
    }
    if command_type == "SendExecutionInput":
        if context is None:
            raise AssertionError("SendExecutionInput tests require an exact context")
        command_payload["context"] = context
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=workspace_id,
        command_type=command_type,
        target_type="execution_session",
        actor_type="user",
        actor_id="user:integration",
        target_id=session_id,
        payload=command_payload,
        expected_version=1,
        idempotency_key=f"command:{command_type}:{client_key}:{uuid4()}",
        correlation_id=uuid4(),
    )


def _context(ids: dict[str, UUID]) -> dict[str, object]:
    return {
        "plan_snapshot_id": str(ids["plan_snapshot_id"]),
        "root_task_id": str(ids["task_id"]),
        "work_item_key": "input",
        "worker_slot_key": "input",
        "worker_id": str(ids["worker_id"]),
        "execution_id": str(ids["execution_id"]),
        "session_id": str(ids["session_id"]),
    }


def _service(database: Database) -> CommandService:
    return CommandService(
        lambda: SqlAlchemyUnitOfWork(database),
        (ExecutionInputCommandHandler(lambda: NOW),),
    )


def test_input_command_persists_queue_outbox_and_client_replay(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_session(database, workspace_id)
    service = _service(database)
    envelope = _envelope(
        workspace_id,
        ids["session_id"],
        ids["execution_id"],
        "SendExecutionInput",
        "client-1",
        payload={"text": "  keep this text  ", "attachment_refs": []},
        context=_context(ids),
    )

    first = service.handle(envelope)
    replay = service.handle(replace(envelope, id=uuid4(), correlation_id=uuid4()))

    assert replay == first
    with database.session() as db_session:
        item = db_session.query(ExecutionInputModel).one()
        assert item.status == "queued"
        assert item.text == "  keep this text  "
        assert db_session.query(OutboxMessageModel).filter(
            OutboxMessageModel.topic == "execution_input.ExecutionInputAccepted"
        ).count() == 1
        assert db_session.query(CommandModel).count() == 1


def test_input_commands_enforce_one_pending_and_allow_update_cancel(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_session(database, workspace_id)
    service = _service(database)
    first = _envelope(
        workspace_id,
        ids["session_id"],
        ids["execution_id"],
        "SendExecutionInput",
        "client-1",
        payload={"text": "first", "attachment_refs": []},
        context=_context(ids),
    )
    service.handle(first)

    with pytest.raises(InvalidTransition):
        service.handle(
            _envelope(
                workspace_id,
                ids["session_id"],
                ids["execution_id"],
                "SendExecutionInput",
                "client-2",
                payload={"text": "second", "attachment_refs": []},
                context=_context(ids),
            )
        )

    updated = service.handle(
        _envelope(
            workspace_id,
            ids["session_id"],
            ids["execution_id"],
            "UpdatePendingExecutionInput",
            "client-1",
            payload={"text": "updated"},
        )
    )
    assert updated.aggregate_version == 3
    cancelled = service.handle(
        _envelope(
            workspace_id,
            ids["session_id"],
            ids["execution_id"],
            "CancelExecutionInput",
            "client-1",
        )
    )
    assert cancelled.aggregate_version == 4
    with database.session() as db_session:
        item = db_session.query(ExecutionInputModel).one()
        assert item.status == "cancelled"
        assert item.text == "updated"
