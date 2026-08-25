from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.attention_commands import (
    AcknowledgeAttentionItemHandler,
)
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.domain.attention import (
    AttentionAcknowledgement,
    AttentionSource,
)
from sidera_control_plane.domain.commands import CommandEnvelope, StoredCommand
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.task import Task

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
RESOURCE_ID = UUID("50000000-0000-0000-0000-000000000001")
SOURCE_CORRELATION_ID = UUID("60000000-0000-0000-0000-000000000001")
ATTENTION_KEY = f"execution:{RESOURCE_ID}:blocked"
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


class FakeTaskRepository:
    def __init__(self) -> None:
        self.task = Task.create(
            task_id=ROOT_TASK_ID,
            workspace_id=WORKSPACE_ID,
            title="Root task",
            description="A root task",
            acceptance_criteria=("done",),
            priority=50,
            requested_by="test",
        )
        self.locked = False

    def get(self, task_id, workspace_id, *, for_update=False):
        if task_id != ROOT_TASK_ID or workspace_id != WORKSPACE_ID:
            return None
        self.locked = self.locked or for_update
        return deepcopy(self.task)


class FakeAttentionRepository:
    def __init__(self) -> None:
        self.source = AttentionSource(
            attention_key=ATTENTION_KEY,
            root_task_id=ROOT_TASK_ID,
            resource_id=RESOURCE_ID,
            resource_version=4,
            correlation_id=SOURCE_CORRELATION_ID,
        )
        self.acknowledgements: dict[tuple[str, str, str], AttentionAcknowledgement] = {}

    def get_current_source(self, root_task_id, workspace_id, attention_key):
        if (
            root_task_id == self.source.root_task_id
            and workspace_id == WORKSPACE_ID
            and attention_key == self.source.attention_key
        ):
            return self.source
        return None

    def get(self, workspace_id, actor_type, actor_id, attention_key):
        return self.acknowledgements.get((str(workspace_id), actor_type, f"{actor_id}:{attention_key}"))

    def upsert(self, acknowledgement):
        key = (
            str(acknowledgement.workspace_id),
            acknowledgement.actor_type,
            f"{acknowledgement.actor_id}:{acknowledgement.attention_key}",
        )
        self.acknowledgements[key] = acknowledgement


class FakeCommandRepository:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], StoredCommand] = {}

    def find_by_id(self, workspace_id, command_id):
        return next(
            (
                stored
                for stored in self.records.values()
                if stored.workspace_id == workspace_id and stored.id == command_id
            ),
            None,
        )

    def find_duplicate(self, workspace_id, actor_type, actor_id, idempotency_key):
        return self.records.get((actor_id, idempotency_key))

    def record_applied(self, envelope, request_hash, result):
        self.records[(envelope.actor_id, envelope.idempotency_key)] = StoredCommand(
            id=envelope.id,
            workspace_id=envelope.workspace_id,
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            idempotency_key=envelope.idempotency_key,
            target_type=envelope.target_type,
            request_hash=request_hash,
            status="applied",
            result={
                "command_id": str(result.command_id),
                "aggregate_id": str(result.aggregate_id),
                "aggregate_version": result.aggregate_version,
                "event_ids": [str(event_id) for event_id in result.event_ids],
                "data": result.data,
            },
            rejection_reason=None,
        )

    def record_rejected(self, envelope, request_hash, error_code, error_result):
        self.records[(envelope.actor_id, envelope.idempotency_key)] = StoredCommand(
            id=envelope.id,
            workspace_id=envelope.workspace_id,
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            idempotency_key=envelope.idempotency_key,
            target_type=envelope.target_type,
            request_hash=request_hash,
            status="rejected",
            result=error_result,
            rejection_reason=error_code,
        )


class FakeEventRepository:
    def __init__(self) -> None:
        self.events: list[EventData] = []

    def append_with_outbox(self, event: EventData) -> None:
        self.events.append(event)


class FakeUnitOfWork:
    tasks: Any
    attention: Any
    commands: Any
    events: Any
    plans: Any
    projection_changes: Any
    workers: Any
    executions: Any
    leases: Any
    sessions: Any
    modules: Any
    bindings: Any
    gateway_operations: Any
    workspaces: Any
    worker_items: Any
    execution_events: Any
    artifacts: Any
    evidence_claims: Any
    execution_inputs: Any
    execution_requests: Any
    planning_profiles: Any
    planning_requests: Any
    planning_runs: Any
    planning_leases: Any
    planning_records: Any
    proposal_materializations: Any
    worker_runtime_configurations: Any
    scheduling_blocks: Any
    work_contexts: Any
    context_assemblies: Any
    context_packages: Any
    knowledge_candidate_submissions: Any

    def __init__(self) -> None:
        self.tasks = FakeTaskRepository()
        self.attention = FakeAttentionRepository()
        self.commands = FakeCommandRepository()
        self.events = FakeEventRepository()
        self.worker_items = None
        self.worker_runtime_configurations = None
        self.scheduling_blocks = None
        self.work_contexts = None
        self.context_assemblies = None
        self.context_packages = None
        self.knowledge_candidate_submissions = None

    def __enter__(self):
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def flush(self) -> None:
        return None

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        acknowledgements = dict(self.attention.acknowledgements)
        events = list(self.events.events)
        try:
            yield
        except Exception:
            self.attention.acknowledgements = acknowledgements
            self.events.events = events
            raise


def make_envelope(*, idempotency_key: str = "ack-001", actor_id: str = "aitejiu") -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        command_type="AcknowledgeAttentionItem",
        target_type="task",
        actor_type="local",
        actor_id=actor_id,
        target_id=ROOT_TASK_ID,
        expected_version=1,
        payload={
            "attention_key": ATTENTION_KEY,
            "source_correlation_id": str(SOURCE_CORRELATION_ID),
            "source_resource_version": 4,
        },
        idempotency_key=idempotency_key,
        correlation_id=uuid4(),
    )


def test_acknowledgement_is_actor_local_and_does_not_mutate_root_task() -> None:
    uow = FakeUnitOfWork()
    handler = AcknowledgeAttentionItemHandler(clock=lambda: NOW)

    result = handler.handle(uow, make_envelope())

    assert result.aggregate_id == ROOT_TASK_ID
    assert result.aggregate_version == 1
    assert uow.tasks.locked is True
    assert len(uow.attention.acknowledgements) == 1
    assert uow.events.events[0].event_type == "attention.item_acknowledged"
    assert uow.events.events[0].payload == {
        "attention_key": ATTENTION_KEY,
        "root_task_id": str(ROOT_TASK_ID),
        "source_correlation_id": str(SOURCE_CORRELATION_ID),
        "source_resource_version": 4,
    }
    assert uow.tasks.task.version == 1

    other_actor = handler.handle(uow, make_envelope(actor_id="other-user", idempotency_key="ack-002"))
    assert other_actor.aggregate_version == 1
    assert len(uow.attention.acknowledgements) == 2
    assert len(uow.events.events) == 2


def test_acknowledgement_replay_returns_same_result_without_duplicate_event() -> None:
    uow = FakeUnitOfWork()
    service = CommandService(lambda: uow, (AcknowledgeAttentionItemHandler(clock=lambda: NOW),))
    envelope = make_envelope()

    first = service.handle(envelope)
    replay = service.handle(replace(envelope, correlation_id=uuid4()))

    assert replay == first
    assert len(uow.events.events) == 1


def test_acknowledgement_requires_the_current_attention_source() -> None:
    uow = FakeUnitOfWork()
    uow.attention.source = AttentionSource(
        attention_key=ATTENTION_KEY,
        root_task_id=ROOT_TASK_ID,
        resource_id=RESOURCE_ID,
        resource_version=5,
        correlation_id=uuid4(),
    )

    with pytest.raises(ValidationError) as exc_info:
        AcknowledgeAttentionItemHandler(clock=lambda: NOW).handle(uow, make_envelope())

    assert exc_info.value.details["current_resource_version"] == 5
    assert uow.attention.acknowledgements == {}
    assert uow.events.events == []
