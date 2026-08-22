from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Self, cast
from uuid import uuid4

import pytest

from sidera_control_plane.application.command_service import (
    CommandService,
    hash_request,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import (
    CommandEnvelope,
    CommandResult,
    StoredCommand,
)
from sidera_control_plane.domain.errors import InvalidTransition, ReplayedDomainError


class FakeCommandRepository:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str, str], StoredCommand] = {}

    def find_duplicate(
        self,
        workspace_id,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> StoredCommand | None:
        return self.records.get((str(workspace_id), actor_type, idempotency_key))

    def record_applied(
        self,
        envelope: CommandEnvelope,
        request_hash: str,
        result: CommandResult,
    ) -> None:
        self.records[
            (str(envelope.workspace_id), envelope.actor_type, envelope.idempotency_key)
        ] = StoredCommand(
            id=envelope.id,
            target_type=envelope.target_type,
            request_hash=request_hash,
            status="applied",
            result={
                "command_id": str(result.command_id),
                "aggregate_id": str(result.aggregate_id),
                "aggregate_version": result.aggregate_version,
                "event_ids": [str(event_id) for event_id in result.event_ids],
            },
            rejection_reason=None,
        )

    def record_rejected(
        self,
        envelope: CommandEnvelope,
        request_hash: str,
        error_code: str,
        error_result: dict[str, object],
    ) -> None:
        self.records[
            (str(envelope.workspace_id), envelope.actor_type, envelope.idempotency_key)
        ] = StoredCommand(
            id=envelope.id,
            target_type=envelope.target_type,
            request_hash=request_hash,
            status="rejected",
            result=dict(error_result),
            rejection_reason=error_code,
        )


class FakeUnitOfWork:
    def __init__(self, commands: FakeCommandRepository) -> None:
        self.commands = commands
        self.mutations = 0
        self.savepoint_calls = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    @contextmanager
    def savepoint(self):
        self.savepoint_calls += 1
        previous_mutations = self.mutations
        try:
            yield
        except Exception:
            self.mutations = previous_mutations
            raise


class RecordingHandler:
    command_types = frozenset({"RecordThing"})

    def __init__(self) -> None:
        self.calls = 0

    def handle(
        self, _uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        self.calls += 1
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=envelope.target_id or uuid4(),
            aggregate_version=1,
            event_ids=(),
        )


class RejectingHandler:
    command_types = frozenset({"RejectThing"})

    def handle(
        self, _uow: ControlPlaneUnitOfWork, _envelope: CommandEnvelope
    ) -> CommandResult:
        raise InvalidTransition("thing cannot be rejected", details={"step": "validate"})


class MutatingRejectingHandler:
    command_types = frozenset({"MutateAndReject"})

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        del envelope
        unit = cast(FakeUnitOfWork, uow)
        unit.mutations += 1
        raise InvalidTransition("mutation rejected")


def make_envelope(command_type: str, *, idempotency_key: str = "thing-001") -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=uuid4(),
        command_type=command_type,
        target_type="task",
        actor_type="test",
        actor_id="actor:test",
        target_id=None,
        payload={"value": 1},
        expected_version=None,
        idempotency_key=idempotency_key,
        correlation_id=uuid4(),
    )


def make_service(
    commands: FakeCommandRepository,
    handlers: tuple[Any, ...],
) -> CommandService:
    factory: Callable[[], ControlPlaneUnitOfWork] = lambda: cast(
        ControlPlaneUnitOfWork, FakeUnitOfWork(commands)
    )
    return CommandService(factory, handlers)


def test_dispatcher_routes_and_replays_without_reinvoking_handler() -> None:
    commands = FakeCommandRepository()
    handler = RecordingHandler()
    service = make_service(commands, (handler,))
    envelope = make_envelope("RecordThing")

    first = service.handle(envelope)
    replay = service.handle(replace(envelope, id=uuid4(), correlation_id=uuid4()))

    assert replay == first
    assert handler.calls == 1


def test_dispatcher_rejects_duplicate_handler_registration() -> None:
    with pytest.raises(ValueError, match="duplicate command handler"):
        make_service(
            FakeCommandRepository(), (RecordingHandler(), RecordingHandler())
        )


def test_dispatcher_replays_domain_rejection_with_stable_details() -> None:
    commands = FakeCommandRepository()
    service = make_service(commands, (RejectingHandler(),))
    envelope = make_envelope("RejectThing")

    with pytest.raises(InvalidTransition) as first_error:
        service.handle(envelope)
    with pytest.raises(ReplayedDomainError) as replay_error:
        service.handle(replace(envelope, id=uuid4(), correlation_id=uuid4()))

    assert replay_error.value.code == first_error.value.code
    assert str(replay_error.value) == str(first_error.value)
    assert replay_error.value.details == first_error.value.details


def test_rejected_handler_rolls_back_mutations_inside_savepoint() -> None:
    commands = FakeCommandRepository()
    unit = FakeUnitOfWork(commands)
    service = CommandService(
        lambda: cast(ControlPlaneUnitOfWork, unit),
        (MutatingRejectingHandler(),),
    )
    envelope = make_envelope("MutateAndReject")

    with pytest.raises(InvalidTransition, match="mutation rejected"):
        service.handle(envelope)

    assert unit.savepoint_calls == 1
    assert unit.mutations == 0
    with pytest.raises(ReplayedDomainError, match="mutation rejected"):
        service.handle(replace(envelope, id=uuid4(), correlation_id=uuid4()))


def test_request_hash_uses_semantics_not_transport_ids() -> None:
    envelope = CommandEnvelope(
        id=uuid4(),
        workspace_id=uuid4(),
        command_type="CreateTask",
        target_type="task",
        actor_type="user",
        actor_id="user:test",
        target_id=None,
        payload={"title": "Build API", "priority": 50},
        expected_version=None,
        idempotency_key="create-task-001",
        correlation_id=uuid4(),
    )

    replay = replace(envelope, id=uuid4(), correlation_id=uuid4())
    changed = replace(envelope, payload={**envelope.payload, "priority": 90})
    changed_target = replace(envelope, target_type="worker")

    assert hash_request(replay) == hash_request(envelope)
    assert hash_request(changed) != hash_request(envelope)
    assert hash_request(changed_target) != hash_request(envelope)
