from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from sidera_knowledge.domain.commands import CommandEnvelope, EventEnvelope
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.models import (
    CommandModel,
    EventModel,
    OutboxMessageModel,
)
from sidera_knowledge.persistence.repositories import PersistenceConflict
from sidera_knowledge.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


def _command(command_id=None, payload=None) -> CommandEnvelope:
    return CommandEnvelope(
        command_id=command_id or uuid4(),
        workspace_id=uuid4(),
        command_type="ArchiveKnowledgePage",
        actor_type="local_owner",
        actor_id="aitejiu",
        target_type="knowledge_page",
        target_id=uuid4(),
        payload=payload or {"reason": "retired"},
        expected_version=1,
        idempotency_key=f"archive-{uuid4().hex}",
        correlation_id=uuid4(),
    )


def _event(command: CommandEnvelope) -> EventEnvelope:
    assert command.target_id is not None
    return EventEnvelope(
        event_id=uuid4(),
        workspace_id=command.workspace_id,
        aggregate_type="knowledge_page",
        aggregate_id=command.target_id,
        aggregate_version=2,
        event_type="KnowledgePageArchived",
        payload={"page_id": str(command.target_id)},
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        correlation_id=command.correlation_id,
        causation_id=command.command_id,
    )


def test_command_event_and_outbox_commit_as_one_transaction(database: Database) -> None:
    command = _command()
    event = _event(command)
    with SqlAlchemyUnitOfWork(database) as uow:
        stored = uow.commands.save_or_replay(command)
        assert stored.status == "pending"
        uow.events.append_with_outbox(event)
        uow.commands.mark_applied(
            command.command_id, command.workspace_id, {"status": "archived"}
        )
        uow.commit()

    with database.session() as session:
        command_count = session.scalar(select(func.count()).select_from(CommandModel))
        event_count = session.scalar(select(func.count()).select_from(EventModel))
        assert command_count is not None and command_count >= 1
        assert event_count is not None and event_count >= 1
        outbox = session.scalar(
            select(OutboxMessageModel).where(OutboxMessageModel.event_id == event.event_id)
        )
        assert outbox is not None
        assert outbox.topic == "knowledge.knowledge_page_archived"
        assert outbox.payload == event.payload


def test_same_command_id_replays_terminal_result_and_changed_hash_conflicts(
    database: Database,
) -> None:
    command = _command()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.commands.save_or_replay(command)
        uow.commands.mark_applied(
            command.command_id, command.workspace_id, {"accepted": True}
        )
        uow.commit()

    with SqlAlchemyUnitOfWork(database) as uow:
        replay = uow.commands.save_or_replay(command)
        assert replay.status == "applied"
        assert replay.result == {"accepted": True}
        uow.commit()

    changed = _command(command.command_id, {"reason": "different"})
    changed = CommandEnvelope(
        command_id=changed.command_id,
        workspace_id=command.workspace_id,
        command_type=command.command_type,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        target_type=command.target_type,
        target_id=command.target_id,
        payload=changed.payload,
        expected_version=command.expected_version,
        idempotency_key=command.idempotency_key,
        correlation_id=changed.correlation_id,
    )
    with pytest.raises(
        PersistenceConflict, match="idempotency_conflict"
    ), SqlAlchemyUnitOfWork(database) as uow:
        uow.commands.save_or_replay(changed)


def test_handler_failure_rolls_back_command_event_and_outbox(database: Database) -> None:
    command = _command()
    event = _event(command)
    with pytest.raises(RuntimeError, match="handler failed"), SqlAlchemyUnitOfWork(
        database
    ) as uow:
        uow.commands.save_or_replay(command)
        uow.events.append_with_outbox(event)
        raise RuntimeError("handler failed")

    with database.session() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(CommandModel).where(
                    CommandModel.id == command.command_id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(EventModel).where(
                    EventModel.id == event.event_id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(OutboxMessageModel).where(
                    OutboxMessageModel.event_id == event.event_id
                )
            )
            == 0
        )
