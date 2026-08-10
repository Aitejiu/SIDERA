from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.task import Task
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import EventModel, OutboxMessageModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


def make_domain_task(workspace_id: UUID) -> Task:
    return Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="Build atomic persistence",
        description="Persist the first control-plane transaction",
        acceptance_criteria=("All records commit or roll back together",),
        priority=50,
        requested_by="user:test",
    )


def make_command(task: Task) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=task.workspace_id,
        command_type="CreateTask",
        actor_type="user",
        actor_id="user:test",
        target_id=None,
        payload={"title": task.title},
        expected_version=None,
        idempotency_key=f"create-{task.id}",
        correlation_id=uuid4(),
    )


def make_created_event(task: Task, command: CommandEnvelope) -> EventData:
    return EventData(
        id=uuid4(),
        workspace_id=task.workspace_id,
        aggregate_type="task",
        aggregate_id=task.id,
        aggregate_version=task.version,
        event_type="TaskCreated",
        payload={"title": task.title, "status": task.status.value},
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        causation_id=command.id,
        correlation_id=command.correlation_id,
    )


def stage_created_task(
    uow: SqlAlchemyUnitOfWork, workspace_id: UUID
) -> tuple[Task, CommandEnvelope, EventData]:
    task = make_domain_task(workspace_id)
    command = make_command(task)
    event = make_created_event(task, command)
    result = CommandResult(
        command_id=command.id,
        aggregate_id=task.id,
        aggregate_version=task.version,
        event_ids=(event.id,),
    )

    uow.tasks.add(task)
    uow.commands.record_applied(command, "request-hash", result)
    uow.events.append_with_outbox(event)
    return task, command, event


def test_unit_of_work_rolls_back_task_command_event_and_outbox(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
    row_counts: Callable[[], dict[str, int]],
) -> None:
    with pytest.raises(RuntimeError, match="force rollback"), uow_factory() as uow:
        stage_created_task(uow, workspace_id)
        raise RuntimeError("force rollback")

    assert row_counts() == {
        "tasks": 0,
        "commands": 0,
        "events": 0,
        "outbox_messages": 0,
    }


def test_unit_of_work_commits_task_command_event_and_outbox_atomically(
    database: Database,
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    workspace_id: UUID,
    row_counts: Callable[[], dict[str, int]],
) -> None:
    with uow_factory() as uow:
        task, command, event = stage_created_task(uow, workspace_id)

    assert row_counts() == {
        "tasks": 1,
        "commands": 1,
        "events": 1,
        "outbox_messages": 1,
    }
    with uow_factory() as uow:
        stored_task = uow.tasks.get(task.id, workspace_id)
        stored_command = uow.commands.find_duplicate(
            workspace_id,
            command.actor_type,
            command.actor_id,
            command.idempotency_key,
        )
    assert stored_task == task
    assert stored_command is not None
    assert stored_command.id == command.id
    assert stored_command.status == "applied"
    assert stored_command.result["event_ids"] == [str(event.id)]

    with database.session() as session:
        event_row = session.scalar(select(EventModel))
        outbox_row = session.scalar(select(OutboxMessageModel))
    assert event_row is not None
    assert outbox_row is not None
    assert event_row.id == outbox_row.event_id
    assert outbox_row.topic == "task.TaskCreated"
    assert outbox_row.payload == {
        "event_id": str(event.id),
        "workspace_id": str(workspace_id),
        "aggregate_type": "task",
        "aggregate_id": str(task.id),
        "aggregate_version": 1,
        "event_type": "TaskCreated",
        "data": event.payload,
        "correlation_id": str(command.correlation_id),
    }
