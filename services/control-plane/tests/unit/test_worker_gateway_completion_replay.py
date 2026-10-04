from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sidera_control_plane.application import worker_gateway as gateway_module
from sidera_control_plane.application.command_service import (
    CommandService,
    hash_request,
)
from sidera_control_plane.application.errors import IdempotencyConflict
from sidera_control_plane.application.execution_commands import (
    ExecutionCompletionDeferred,
)
from sidera_control_plane.application.worker_completion import CompletionDecision
from sidera_control_plane.application.worker_gateway import (
    GatewayMessageError,
    WorkerGateway,
)
from sidera_control_plane.domain.commands import (
    CommandEnvelope,
    CommandResult,
    StoredCommand,
)
from sidera_control_plane.domain.errors import InvalidTransition, ReplayedDomainError
from sidera_control_plane.persistence.repositories import (
    _command_model,
    _stored_command,
)
from tests.unit.test_command_service import FakeCommandRepository, FakeUnitOfWork


class CompletionCommands(FakeCommandRepository):
    def _persist_version(self, envelope):
        key = (
            str(envelope.workspace_id),
            envelope.actor_type,
            envelope.actor_id,
            envelope.idempotency_key,
        )
        stored = self.records[key]
        self.records[key] = _stored_command(
            _command_model(
                envelope,
                request_hash=stored.request_hash,
                status=stored.status,
                result=stored.result,
                rejection_reason=stored.rejection_reason,
            )
        )

    def record_applied(self, envelope, request_hash, result):
        super().record_applied(envelope, request_hash, result)
        self._persist_version(envelope)

    def record_rejected(self, envelope, request_hash, error_code, error_result):
        super().record_rejected(envelope, request_hash, error_code, error_result)
        self._persist_version(envelope)


@pytest.fixture
def completion_case(monkeypatch):
    commands = CompletionCommands()
    uow = FakeUnitOfWork(commands)
    execution = SimpleNamespace(
        id=uuid4(), workspace_id=uuid4(), lineage_id=uuid4(), version=4
    )
    context = SimpleNamespace(
        execution=execution,
        task=SimpleNamespace(id=uuid4()),
        worker=SimpleNamespace(id=uuid4()),
        lease=SimpleNamespace(id=uuid4()),
        binding=SimpleNamespace(fencing_token=1),
    )
    uow.execution_requests = SimpleNamespace(
        get_by_execution_lineage=lambda *args: None
    )
    uow.executions = SimpleNamespace(get=lambda *args: execution)
    decisions = []
    behavior = [None]

    class CompletionHandler:
        command_types = frozenset({"ReportExecutionSucceeded", "ReportExecutionFailed"})

        def __init__(self, *args, **kwargs):
            pass

        def handle(self, unit, envelope):
            decisions.append(envelope)
            if behavior[0] is not None:
                raise behavior[0]
            execution.version += 1
            return CommandResult(
                envelope.id, execution.id, execution.version, (uuid4(),)
            )

    monkeypatch.setattr(gateway_module, "ExecutionCommandHandler", CompletionHandler)
    gateway = WorkerGateway(
        lambda: uow,
        adapter_factories={},
        workspace_factories={},
        clock=lambda: datetime(2026, 10, 4, tzinfo=UTC),
    )
    decision = CompletionDecision("succeeded", "session.completed", 2, None, 1, None)
    session_id = uuid4()
    return SimpleNamespace(
        commands=commands,
        uow=uow,
        context=context,
        gateway=gateway,
        decisions=decisions,
        behavior=behavior,
        decision=decision,
        session_id=session_id,
        report=lambda: gateway._report_execution_completion(
            context, session_id=session_id, decision=decision
        ),
    )


def test_command_mapper_exposes_original_expected_version_without_breaking_old_positional_callers():
    envelope = CommandEnvelope(
        uuid4(),
        uuid4(),
        "RecordThing",
        "task",
        "test",
        "actor:test",
        uuid4(),
        {},
        4,
        "mapper",
        uuid4(),
    )
    row = _command_model(
        envelope,
        request_hash=hash_request(envelope),
        status="applied",
        result={},
        rejection_reason=None,
    )
    stored = _stored_command(row)
    assert stored.expected_version == 4
    legacy = StoredCommand(
        envelope.id,
        envelope.workspace_id,
        "test",
        "actor:test",
        "mapper",
        "task",
        "hash",
        "applied",
        {},
        None,
    )
    assert legacy.expected_version is None


def test_completion_replays_original_saved_version_after_execution_advances(
    completion_case,
):
    s = completion_case
    assert s.report() is True
    assert s.context.execution.version == 5
    original = next(iter(s.commands.records.values()))
    assert s.report() is True
    assert len(s.decisions) == len(s.commands.records) == 1
    assert s.decisions[0].expected_version == 4
    assert next(iter(s.commands.records.values())) == original


@pytest.mark.parametrize("invalid", (None, 0, -1, True, "4", 4.0))
def test_saved_completion_requires_strict_positive_original_version(
    completion_case, invalid
):
    s = completion_case
    assert s.report() is True
    key, stored = next(iter(s.commands.records.items()))
    s.commands.records[key] = SimpleNamespace(
        **{**asdict(stored), "expected_version": invalid}
    )
    with pytest.raises(GatewayMessageError) as denied:
        s.report()
    assert denied.value.code == "execution_completion_receipt_invalid"
    assert len(s.decisions) == 1


@pytest.mark.parametrize(
    "changed",
    ("lease", "fence", "worker", "receipt_actor", "receipt_target", "receipt_payload"),
)
def test_original_completion_replay_preserves_strict_identity_and_payload(
    completion_case, changed
):
    s = completion_case
    assert s.report() is True
    if changed == "lease":
        s.context.lease.id = uuid4()
    elif changed == "fence":
        s.context.binding.fencing_token += 1
    elif changed == "worker":
        s.context.worker.id = uuid4()
    else:
        key, stored = next(iter(s.commands.records.items()))
        if changed == "receipt_actor":
            altered = replace(stored, actor_id="another-worker")
        elif changed == "receipt_target":
            altered = replace(stored, target_type="task")
        else:
            envelope = replace(
                s.decisions[0], target_id=uuid4(), payload={"fencing_token": 99}
            )
            altered = replace(stored, request_hash=hash_request(envelope))
        s.commands.records[key] = altered
    with pytest.raises(IdempotencyConflict):
        s.report()
    assert len(s.decisions) == len(s.commands.records) == 1


def test_original_rejected_completion_stays_rejected_at_saved_version(completion_case):
    s = completion_case
    s.behavior[0] = InvalidTransition("controlled rejection")
    with pytest.raises(InvalidTransition):
        s.report()
    s.context.execution.version += 1
    with pytest.raises(ReplayedDomainError) as rejected:
        s.report()
    assert rejected.value.code == "invalid_transition"
    assert len(s.decisions) == len(s.commands.records) == 1
    assert next(iter(s.commands.records.values())).status == "rejected"


@pytest.mark.parametrize("winner", ("exact", "different_payload", "different_id"))
def test_completion_first_receipt_race_reloads_only_exact_original_once(
    completion_case, monkeypatch, winner
):
    s = completion_case
    s.context.execution.version = 5
    handle = CommandService.handle
    attempts = []

    def commit_before_handle(service, envelope):
        attempts.append(envelope)
        if len(attempts) == 1:
            original = replace(envelope, expected_version=4)
            if winner == "different_payload":
                original = replace(
                    original, payload={**original.payload, "fencing_token": 99}
                )
            elif winner == "different_id":
                original = replace(original, id=uuid4())
            s.context.execution.version = 4
            handle(service, original)
        return handle(service, envelope)

    monkeypatch.setattr(CommandService, "handle", commit_before_handle)
    if winner == "exact":
        assert s.report() is True
        assert [envelope.expected_version for envelope in attempts] == [5, 4]
    else:
        with pytest.raises(IdempotencyConflict):
            s.report()
        assert len(attempts) == (2 if winner == "different_payload" else 1)
    assert len(s.decisions) == len(s.commands.records) == 1
    assert s.context.execution.version == 5


def test_completion_deferred_preserves_no_receipt_and_returns_false(completion_case):
    s = completion_case
    s.behavior[0] = ExecutionCompletionDeferred("controlled pending input")
    assert s.report() is False
    assert not s.commands.records and s.context.execution.version == 4
