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
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    CommandModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionSessionModel,
    OutboxMessageModel,
    TaskModel,
    WorkerModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _seed_session(database: Database, workspace_id: UUID) -> dict[str, UUID]:
    task_id = uuid4()
    worker_id = uuid4()
    execution_id = uuid4()
    lease_id = uuid4()
    session_id = uuid4()
    with database.session() as db_session:
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
) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=workspace_id,
        command_type=command_type,
        target_type="execution_session",
        actor_type="user",
        actor_id="user:integration",
        target_id=session_id,
        payload={
            "execution_id": str(execution_id),
            "client_idempotency_key": client_key,
            **(payload or {}),
        },
        expected_version=1,
        idempotency_key=f"command:{command_type}:{client_key}:{uuid4()}",
        correlation_id=uuid4(),
    )


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

