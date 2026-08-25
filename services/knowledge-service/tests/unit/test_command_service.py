from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sidera_knowledge.application.command_service import (
    CommandOutcome,
    KnowledgeCommandService,
)
from sidera_knowledge.domain.commands import CommandEnvelope, EventEnvelope
from sidera_knowledge.domain.knowledge_errors import CommandFailure


@dataclass
class FakeCommandRow:
    command_id: Any
    request_hash: str
    status: str = "pending"
    result: dict[str, Any] | None = None
    error_code: str | None = None


@dataclass
class FakeCommands:
    rows: dict[Any, FakeCommandRow] = field(default_factory=dict)

    def save_or_replay(self, command: CommandEnvelope) -> FakeCommandRow:
        row = self.rows.get(command.command_id)
        if row is None:
            row = FakeCommandRow(command.command_id, command.request_hash)
            self.rows[command.command_id] = row
        return row

    def mark_applied(self, command_id, workspace_id, result) -> None:
        row = self.rows[command_id]
        row.status = "applied"
        row.result = result

    def mark_rejected(self, command_id, workspace_id, error_code, details) -> None:
        row = self.rows[command_id]
        row.status = "rejected"
        row.error_code = error_code
        row.result = details



@dataclass
class FakeEvents:
    events: list[EventEnvelope] = field(default_factory=list)

    def append_with_outbox(self, event: EventEnvelope) -> None:
        self.events.append(event)


@dataclass
class FakeUow:
    commands: FakeCommands = field(default_factory=FakeCommands)
    events: FakeEvents = field(default_factory=FakeEvents)
    commits: int = 0
    rollbacks: int = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None:
            self.rollbacks += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def flush(self):
        return None


def _command(command_type: str) -> CommandEnvelope:
    return CommandEnvelope(
        command_id=uuid4(),
        workspace_id=uuid4(),
        command_type=command_type,
        actor_type="local_owner",
        actor_id="owner",
        target_type="knowledge_candidate",
        target_id=uuid4(),
        payload={},
        expected_version=None,
        idempotency_key=uuid4().hex,
        correlation_id=uuid4(),
    )


def test_unknown_command_is_a_terminal_safe_rejection() -> None:
    uow = FakeUow()
    service = KnowledgeCommandService(lambda: uow, handlers={})

    outcome = service.handle(_command("UnknownKnowledgeCommand"))

    assert outcome == CommandOutcome(
        status="rejected",
        command_id=next(iter(uow.commands.rows)),
        result={},
        error_code="unsupported_command",
    )
    assert uow.commands.rows[outcome.command_id].status == "rejected"
    assert uow.commits == 1
    assert not uow.events.events


def test_same_command_replay_does_not_run_handler_twice() -> None:
    uow = FakeUow()
    calls = 0

    def handler(command, current_uow):
        nonlocal calls
        calls += 1
        current_uow.events.append_with_outbox(
            EventEnvelope(
                event_id=uuid4(),
                workspace_id=command.workspace_id,
                aggregate_type="knowledge_candidate",
                aggregate_id=command.target_id,
                aggregate_version=1,
                event_type="KnowledgeCandidateSubmitted",
                payload={"candidate_id": str(command.target_id)},
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                correlation_id=command.correlation_id,
            )
        )
        return {"candidate_id": str(command.target_id)}

    command = _command("SubmitKnowledgeCandidate")
    service = KnowledgeCommandService(lambda: uow, {command.command_type: handler})

    first = service.handle(command)
    second = service.handle(command)

    assert first == second
    assert first.status == "applied"
    assert calls == 1
    assert len(uow.events.events) == 1


def test_domain_failure_is_persisted_as_rejected_terminal() -> None:
    uow = FakeUow()
    command = _command("SubmitKnowledgeCandidate")

    def handler(command, current_uow):
        raise CommandFailure("source_invalid", {"field": "sources"})

    service = KnowledgeCommandService(lambda: uow, {command.command_type: handler})
    outcome = service.handle(command)

    assert outcome.status == "rejected"
    assert outcome.error_code == "source_invalid"
    assert uow.commands.rows[command.command_id].status == "rejected"
    assert uow.commits == 1
