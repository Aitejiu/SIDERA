import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import OperationalError

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.application import worker_gateway as worker_gateway_module
from sidera_control_plane.application.event_ingestor import (
    ArtifactRegistry,
    EventIngestionReport,
    TerminalEventObservation,
)
from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
from sidera_control_plane.application.ports import (
    ArtifactContentWriter,
    ControlPlaneUnitOfWork,
)
from sidera_control_plane.application.worker_gateway import (
    ASSIGNMENT_TOPIC,
    STOP_TOPIC,
    AssignmentLeaseKeeper,
    GatewayAssignmentRunner,
    GatewayMessageError,
    GatewayObsoleteAssignmentError,
    GatewayRecoveryRequired,
    GatewayTerminalAssignmentError,
    OutboxClaimKeeper,
    WorkerGateway,
    _bind_spec_to_revision,
    _load_module,
)
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.domain.context_packages import ContextAcknowledgement
from sidera_control_plane.domain.gateway import (
    RuntimeModuleOperation,
    runtime_module_operation_allowed,
)
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    LOCAL_VERIFICATION_PROVIDER,
    ModuleInstance,
    ModuleLifecycle,
)
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    RepositoryCheckoutSpec,
)
from tests.unit.test_context_package_v2 import _materialization_v1

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _completion_context(
    *,
    policy: str | None,
    turn_completion_enabled: bool,
) -> SimpleNamespace:
    config = {} if policy is None else {"fake.completion_policy": policy}
    instance = ModuleInstance(
        id=uuid4(),
        workspace_id=uuid4(),
        definition_id=uuid4(),
        instance_name="fake-worker",
        config_version=1,
        config_namespace="fake",
        config=config,
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    binding = SimpleNamespace(
        capability_snapshot=FakeWorkerAdapter(
            turn_completion_enabled=turn_completion_enabled
        ).probe({})
    )
    return SimpleNamespace(adapter_instance=instance, binding=binding)


def _message(topic: str = ASSIGNMENT_TOPIC) -> OutboxMessage:
    return OutboxMessage(
        id=uuid4(),
        event_id=uuid4(),
        topic=topic,
        payload={"workspace_id": str(uuid4())},
        attempts=1,
        claim_token=uuid4(),
    )


class RecordingStore:
    def __init__(self, messages: tuple[OutboxMessage, ...]) -> None:
        self.messages = messages
        self.claim_topics_history: list[set[str]] = []
        self.claimed_ids: set[UUID] = set()
        self.acknowledged: list[tuple[object, object]] = []
        self.rescheduled: list[tuple[object, object, datetime, str]] = []
        self.renewed: list[tuple[object, object]] = []
        self.renew_result = True

    def claim(self, batch_size: int, *, topics=None):
        assert 1 <= batch_size <= 20
        selected_topics = set(topics or ())
        self.claim_topics_history.append(selected_topics)
        available = [
            message
            for message in self.messages
            if message.id not in self.claimed_ids
            and (not selected_topics or message.topic in selected_topics)
        ]
        selected = tuple(available[:batch_size])
        self.claimed_ids.update(message.id for message in selected)
        return selected

    def acknowledge(self, message_id, claim_token) -> bool:
        self.acknowledged.append((message_id, claim_token))
        return True

    def renew_claim(self, message_id, claim_token) -> bool:
        self.renewed.append((message_id, claim_token))
        return self.renew_result

    def reschedule(self, message_id, claim_token, next_attempt_at, error) -> bool:
        self.rescheduled.append((message_id, claim_token, next_attempt_at, error))
        return True


class RecordingGateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[OutboxMessage] = []
        self.fail = fail
        self.active_session_syncs = 0

    def handle(self, message: OutboxMessage) -> object:
        self.messages.append(message)
        if self.fail:
            raise RuntimeError("gateway unavailable")

    def sync_active_demo_sessions(self) -> int:
        self.active_session_syncs += 1
        return 1


class BarrierGateway(RecordingGateway):
    def __init__(self, participants: int) -> None:
        super().__init__()
        self.barrier = threading.Barrier(participants)

    def handle(self, message: OutboxMessage) -> object:
        result = super().handle(message)
        self.barrier.wait(timeout=2)
        return result


class PublishingStore(RecordingStore):
    def __init__(self, messages: tuple[OutboxMessage, ...]) -> None:
        super().__init__(messages)
        self._lock = threading.Lock()

    def publish(self, message: OutboxMessage) -> None:
        with self._lock:
            self.messages = (*self.messages, message)

    def claim(self, batch_size: int, *, topics=None):
        with self._lock:
            return super().claim(batch_size, topics=topics)


class GatedGateway(RecordingGateway):
    def __init__(
        self,
        first_message: OutboxMessage,
        *,
        fail_ids: frozenset[UUID] = frozenset(),
    ) -> None:
        super().__init__()
        self.first_message = first_message
        self.fail_ids = fail_ids
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.late_started = threading.Event()
        self._lock = threading.Lock()
        self.active_handlers = 0
        self.max_active_handlers = 0

    def handle(self, message: OutboxMessage) -> object:
        with self._lock:
            self.messages.append(message)
            self.active_handlers += 1
            self.max_active_handlers = max(
                self.max_active_handlers, self.active_handlers
            )
        try:
            if message.id == self.first_message.id:
                self.first_started.set()
                if not self.release_first.wait(timeout=5):
                    raise RuntimeError("test did not release the first handler")
            else:
                self.late_started.set()
            if message.id in self.fail_ids:
                raise RuntimeError("gateway unavailable")
        finally:
            with self._lock:
                self.active_handlers -= 1


class RecordingArtifactContentWriter(ArtifactContentWriter):
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID, str, bytes]] = []

    def put(
        self,
        workspace_id: UUID,
        execution_id: UUID,
        expected_hash: str,
        content: bytes,
    ) -> str:
        self.calls.append((workspace_id, execution_id, expected_hash, content))
        return f"artifact-local://{workspace_id}/{execution_id}/{expected_hash}"


class RecordingLeaseRepository:
    def __init__(self, lease: AssignmentLease) -> None:
        self.lease = lease
        self.saved: list[tuple[int, int]] = []

    def get(self, lease_id, workspace_id, *, for_update=False):
        assert for_update is True
        assert lease_id == self.lease.id
        assert workspace_id == self.lease.workspace_id
        return self.lease

    def save(self, lease, previous_version):
        self.saved.append((previous_version, lease.version))


class RecordingEventStore:
    def __init__(self) -> None:
        self.events = []

    def append_with_outbox(self, event) -> None:
        self.events.append(event)


class RecordingCommandRepository:
    def __init__(self) -> None:
        self.commands = []

    def record_applied(self, command, request_hash, result) -> None:
        self.commands.append((command, request_hash, result))


class RecordingLeaseUow:
    def __init__(self, leases, events, commands) -> None:
        self.leases = leases
        self.events = events
        self.commands = commands

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback


class RecordingResultCapture:
    def __init__(self) -> None:
        self.observations: list[dict[str, object]] = []
        self.captures: list[dict[str, object]] = []
        self.snapshot = object()

    def observe_terminal(self, **kwargs):
        self.observations.append(kwargs)
        return object()

    def capture_execution(self, **kwargs):
        self.captures.append(kwargs)
        return self.snapshot


def test_assignment_lease_keeper_renews_and_audits_with_fencing() -> None:
    workspace_id = UUID("20000000-0000-0000-0000-000000000001")
    execution_id = UUID("70000000-0000-0000-0000-000000000001")
    worker_id = UUID("80000000-0000-0000-0000-000000000001")
    lease = AssignmentLease.issue(
        lease_id=UUID("90000000-0000-0000-0000-000000000001"),
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        fencing_token=4,
        assignment_reason={"source": "unit-test"},
    )
    leases = RecordingLeaseRepository(lease)
    events = RecordingEventStore()
    commands = RecordingCommandRepository()
    uow = RecordingLeaseUow(leases, events, commands)
    keeper = AssignmentLeaseKeeper(
        uow_factory=lambda: cast(ControlPlaneUnitOfWork, uow),
        lease_id=lease.id,
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        fencing_token=4,
        lease_ttl_seconds=30,
        renew_interval_seconds=10,
        clock=lambda: NOW + timedelta(seconds=5),
    )

    assert keeper.renew_once() is True
    assert lease.expires_at == NOW + timedelta(seconds=35)
    assert lease.renewed_at == NOW + timedelta(seconds=5)
    assert leases.saved == [(1, 2)]
    assert len(events.events) == 1
    assert events.events[0].event_type == "LeaseRenewed"
    assert events.events[0].aggregate_version == 2
    assert events.events[0].payload["fencing_token"] == 4
    assert commands.commands[0][0].command_type == "RenewLease"
    assert commands.commands[0][2].event_ids == (events.events[0].id,)


def test_artifact_registration_retries_a_postgres_deadlock(monkeypatch) -> None:
    class Deadlock:
        sqlstate = "40P01"

    class RetryingRegistry:
        def __init__(self) -> None:
            self.calls = 0
            self.result = object()

        def register(self, **kwargs):
            del kwargs
            self.calls += 1
            if self.calls == 1:
                raise OperationalError("insert", {}, Deadlock())
            return self.result

    monkeypatch.setattr(worker_gateway_module.time, "sleep", lambda _: None)
    registry = RetryingRegistry()
    gateway = WorkerGateway(
        lambda: cast(ControlPlaneUnitOfWork, object()),
        adapter_factories={},
        workspace_factories={},
        artifact_registry=cast(ArtifactRegistry, registry),
    )

    result = gateway._register_artifacts_with_deadlock_retry(
        workspace_id=uuid4(),
        task_id=uuid4(),
        execution_id=uuid4(),
        session_id=uuid4(),
        candidates=(),
        acceptance_criteria=(),
        producer="test",
        environment_ref="test",
    )

    assert result is registry.result
    assert registry.calls == 2


def test_gateway_rejects_initial_turn_without_bound_capability_before_side_effects() -> None:
    gateway = WorkerGateway(
        lambda: cast(ControlPlaneUnitOfWork, object()),
        adapter_factories={},
        workspace_factories={},
    )

    with pytest.raises(GatewayMessageError) as caught:
        gateway._validate_completion_contract(
            _completion_context(policy="initial_turn", turn_completion_enabled=False)
        )

    assert caught.value.code == "turn_completion_capability_unsupported"


def test_gateway_rejects_invalid_completion_policy_before_side_effects() -> None:
    gateway = WorkerGateway(
        lambda: cast(ControlPlaneUnitOfWork, object()),
        adapter_factories={},
        workspace_factories={},
    )

    with pytest.raises(GatewayMessageError) as caught:
        gateway._validate_completion_contract(
            _completion_context(policy="guess", turn_completion_enabled=True)
        )

    assert caught.value.code == "completion_policy_invalid"


def test_gateway_keeps_default_completion_policy_explicit() -> None:
    gateway = WorkerGateway(
        lambda: cast(ControlPlaneUnitOfWork, object()),
        adapter_factories={},
        workspace_factories={},
    )

    gateway._validate_completion_contract(
        _completion_context(policy=None, turn_completion_enabled=False)
    )


def test_outbox_claim_keeper_renews_and_records_claim_loss() -> None:
    message = _message()
    store = RecordingStore((message,))
    keeper = OutboxClaimKeeper(store, message, interval_seconds=1)

    assert keeper.renew_once() is True
    assert store.renewed == [(message.id, message.claim_token)]
    store.renew_result = False
    assert keeper.renew_once() is False
    assert keeper.lost is True


def test_assignment_lease_keeper_requires_renew_interval_below_half_ttl() -> None:
    with pytest.raises(ValueError, match="less than half"):
        AssignmentLeaseKeeper(
            uow_factory=lambda: None,  # type: ignore[return-value]
            lease_id=uuid4(),
            workspace_id=uuid4(),
            execution_id=uuid4(),
            worker_id=uuid4(),
            fencing_token=1,
            lease_ttl_seconds=30,
            renew_interval_seconds=15,
        )


def test_gateway_runner_claims_assignment_and_stop_topics_and_acknowledges() -> None:
    message = _message()
    store = RecordingStore((message,))
    gateway = RecordingGateway()
    runner = GatewayAssignmentRunner(store, gateway, clock=lambda: NOW)

    assert runner.run_once() == (1, 1, 0)
    assert store.claim_topics_history == [{STOP_TOPIC}, {ASSIGNMENT_TOPIC}]
    assert gateway.messages == [message]
    assert store.acknowledged == [(message.id, message.claim_token)]


def test_gateway_runner_prioritizes_stop_messages_before_assignments() -> None:
    assignment = _message(ASSIGNMENT_TOPIC)
    stop = _message(STOP_TOPIC)
    store = RecordingStore((assignment, stop))
    gateway = RecordingGateway()
    runner = GatewayAssignmentRunner(store, gateway, clock=lambda: NOW)

    assert runner.run_once() == (2, 2, 0)
    assert gateway.messages == [stop, assignment]
    assert store.claim_topics_history == [{STOP_TOPIC}, {ASSIGNMENT_TOPIC}]


def test_gateway_runner_limits_assignment_concurrency() -> None:
    assignments = (_message(), _message())
    store = RecordingStore(assignments)
    gateway = BarrierGateway(participants=2)
    runner = GatewayAssignmentRunner(
        store,
        gateway,
        clock=lambda: NOW,
        start_concurrency=2,
        claim_renew_interval_seconds=100,
    )

    assert runner.run_once() == (2, 2, 0)
    assert len(gateway.messages) == 2
    assert store.claim_topics_history[:2] == [{STOP_TOPIC}, {ASSIGNMENT_TOPIC}]
    assert all(
        topics in ({STOP_TOPIC}, {ASSIGNMENT_TOPIC})
        for topics in store.claim_topics_history[2:]
    )


def test_gateway_runner_starts_assignment_published_during_active_start() -> None:
    first, second = _message(), _message()
    store = PublishingStore((first,))
    gateway = GatedGateway(first)
    runner = GatewayAssignmentRunner(store, gateway, start_concurrency=2)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runner.run_once, 2)
        first_started = gateway.first_started.wait(timeout=2)
        late_started = False
        try:
            if first_started:
                store.publish(second)
                late_started = gateway.late_started.wait(timeout=2)
        finally:
            gateway.release_first.set()
        result = future.result(timeout=3)

    assert first_started
    assert late_started
    assert result == (2, 2, 0)
    assert {message.id for message in gateway.messages} == {first.id, second.id}
    assert {message_id for message_id, _ in store.acknowledged} == {
        first.id,
        second.id,
    }
    assert len(store.acknowledged) == 2


def test_gateway_runner_handles_late_stop_while_start_is_active() -> None:
    assignment = _message()
    stop = _message(STOP_TOPIC)
    store = PublishingStore((assignment,))
    gateway = GatedGateway(assignment)
    runner = GatewayAssignmentRunner(store, gateway, start_concurrency=2)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runner.run_once, 2)
        first_started = gateway.first_started.wait(timeout=2)
        stop_started = False
        stop_was_last_before_release = False
        try:
            if first_started:
                store.publish(stop)
                stop_started = gateway.late_started.wait(timeout=2)
                stop_was_last_before_release = (
                    bool(gateway.messages) and gateway.messages[-1].id == stop.id
                )
        finally:
            gateway.release_first.set()
        result = future.result(timeout=3)

    assert first_started
    assert stop_started
    assert stop_was_last_before_release
    assert result == (2, 2, 0)
    assert {message_id for message_id, _ in store.acknowledged} == {
        assignment.id,
        stop.id,
    }
    assert len(store.acknowledged) == 2


def test_gateway_runner_does_not_preclaim_assignment_behind_initial_stop() -> None:
    stop = _message(STOP_TOPIC)
    assignment = _message()
    store = PublishingStore((stop, assignment))
    gateway = GatedGateway(stop)
    runner = GatewayAssignmentRunner(store, gateway, start_concurrency=2)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runner.run_once, 2)
        stop_started = gateway.first_started.wait(timeout=2)
        assignment_claimed_while_stop_blocked = False
        try:
            if stop_started:
                assignment_claimed_while_stop_blocked = (
                    assignment.id in store.claimed_ids
                )
        finally:
            gateway.release_first.set()
        result = future.result(timeout=3)

    assert stop_started
    assert not assignment_claimed_while_stop_blocked
    assert result == (2, 2, 0)
    assert gateway.messages == [stop, assignment]
    assert {message_id for message_id, _ in store.acknowledged} == {
        stop.id,
        assignment.id,
    }


def test_gateway_runner_live_claim_respects_batch_cap_and_concurrency() -> None:
    first, second, third = _message(), _message(), _message()
    store = PublishingStore((first,))
    gateway = GatedGateway(first)
    runner = GatewayAssignmentRunner(store, gateway, start_concurrency=2)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runner.run_once, 2)
        first_started = gateway.first_started.wait(timeout=2)
        late_started = False
        try:
            if first_started:
                store.publish(second)
                store.publish(third)
                late_started = gateway.late_started.wait(timeout=2)
        finally:
            gateway.release_first.set()
        result = future.result(timeout=3)

    assert first_started
    assert late_started
    assert result == (2, 2, 0)
    assert store.claimed_ids == {first.id, second.id}
    assert third.id not in store.claimed_ids
    assert gateway.max_active_handlers <= 2
    assert len(store.acknowledged) == 2


def test_gateway_runner_reschedules_failure_in_late_batch_once() -> None:
    first, late = _message(), _message()
    store = PublishingStore((first,))
    gateway = GatedGateway(first, fail_ids=frozenset({late.id}))
    runner = GatewayAssignmentRunner(store, gateway, start_concurrency=2)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runner.run_once, 2)
        first_started = gateway.first_started.wait(timeout=2)
        late_started = False
        try:
            if first_started:
                store.publish(late)
                late_started = gateway.late_started.wait(timeout=2)
        finally:
            gateway.release_first.set()
        result = future.result(timeout=3)

    assert first_started
    assert late_started
    assert result == (2, 1, 1)
    assert store.acknowledged == [(first.id, first.claim_token)]
    assert len(store.rescheduled) == 1
    assert store.rescheduled[0][0:2] == (late.id, late.claim_token)


def test_gateway_runner_syncs_active_demo_sessions_after_batch() -> None:
    store = RecordingStore(())
    gateway = RecordingGateway()
    runner = GatewayAssignmentRunner(
        store,
        gateway,
        active_session_sync=gateway.sync_active_demo_sessions,
        clock=lambda: NOW,
    )

    assert runner.run_once() == (0, 0, 0)
    assert gateway.active_session_syncs == 1


def test_gateway_runner_reschedules_failures_without_acknowledging() -> None:
    message = _message()
    store = RecordingStore((message,))
    runner = GatewayAssignmentRunner(
        store,
        RecordingGateway(fail=True),
        clock=lambda: NOW,
    )

    assert runner.run_once() == (1, 0, 1)
    assert store.acknowledged == []
    assert store.rescheduled[0][2] == NOW + timedelta(seconds=2)
    assert "gateway_unavailable" in store.rescheduled[0][3]


def test_gateway_runner_reschedules_database_read_failures_without_acknowledging() -> None:
    workspace_id = uuid4()
    execution_id = uuid4()
    lease_id = uuid4()
    message = replace(
        _message(),
        payload={
            "workspace_id": str(workspace_id),
            "aggregate_id": str(execution_id),
            "data": {"lease_id": str(lease_id), "fencing_token": 1},
        },
    )

    class UnavailableUnitOfWork:
        def __enter__(self):
            raise OperationalError(
                "SELECT execution", {}, RuntimeError("database unavailable")
            )

        def __exit__(self, *_values):
            return False

    gateway = WorkerGateway(
        UnavailableUnitOfWork,
        adapter_factories={},
        workspace_factories={},
    )
    store = RecordingStore((message,))
    runner = GatewayAssignmentRunner(store, gateway, clock=lambda: NOW)

    assert runner.run_once() == (1, 0, 1)
    assert store.acknowledged == []
    assert store.rescheduled[0][0:2] == (message.id, message.claim_token)


def test_gateway_runner_keeps_recovery_required_operations_retryable() -> None:
    message = _message()
    store = RecordingStore((message,))

    class RecoveringGateway(RecordingGateway):
        def handle(self, claimed_message: OutboxMessage) -> object:
            self.messages.append(claimed_message)
            raise GatewayRecoveryRequired(
                "external operation requires reconciliation",
                code="operation_outcome_unknown",
            )

    runner = GatewayAssignmentRunner(store, RecoveringGateway(), clock=lambda: NOW)

    assert runner.run_once() == (1, 0, 1)
    assert store.acknowledged == []
    assert store.rescheduled[0][0:2] == (message.id, message.claim_token)


@pytest.mark.parametrize(
    "terminal_error",
    (
        GatewayTerminalAssignmentError(
            "acceptance start budget exhausted", code="acceptance_budget_exhausted"
        ),
        GatewayObsoleteAssignmentError("assignment lease is no longer current"),
    ),
)
def test_gateway_runner_acknowledges_only_typed_terminal_dispositions(
    terminal_error: Exception,
) -> None:
    message = _message()
    store = RecordingStore((message,))

    class TerminalGateway(RecordingGateway):
        def handle(self, claimed_message: OutboxMessage) -> object:
            self.messages.append(claimed_message)
            raise terminal_error

    runner = GatewayAssignmentRunner(store, TerminalGateway(), clock=lambda: NOW)

    assert runner.run_once() == (1, 1, 0)
    assert store.acknowledged == [(message.id, message.claim_token)]
    assert store.rescheduled == []


def test_gateway_observes_all_terminals_but_captures_only_success() -> None:
    from contextlib import nullcontext

    from sidera_control_plane.application.code_result_capture import (
        code_capture_operation_id,
    )

    workspace_id = UUID("20000000-0000-0000-0000-000000000001")
    execution_id = UUID("70000000-0000-0000-0000-000000000001")

    def legacy_link(scope_id, target_id) -> None:
        assert (scope_id, target_id) == (workspace_id, execution_id)

    uow = SimpleNamespace(execution_authorities=SimpleNamespace(get_link=legacy_link))
    capture = RecordingResultCapture()
    gateway = WorkerGateway(
        lambda: nullcontext(uow),  # type: ignore[return-value]
        adapter_factories={},
        workspace_factories={},
        code_result_capture_service=capture,  # type: ignore[arg-type]
    )
    report = EventIngestionReport(
        events=(),
        duplicate_sequences=(),
        terminal_observations=(
            TerminalEventObservation(
                event_type="session.failed",
                sequence=1,
                outcome="failed",
                payload={"failure_code": "worker_failed"},
            ),
            TerminalEventObservation(
                event_type="execution.succeeded",
                sequence=2,
                outcome="succeeded",
                payload={"summary": "ready"},
            ),
        ),
    )

    result = gateway._capture_observed_terminal(
        workspace_id=workspace_id,
        execution_id=execution_id,
        fencing_token=1,
        report=report,
    )

    assert result is capture.snapshot
    assert [item["outcome"] for item in capture.observations] == [
        "failed",
        "succeeded",
    ]
    assert len(capture.captures) == 1
    assert all(
        item["operation_id"] == code_capture_operation_id(execution_id)
        for item in (*capture.observations, *capture.captures)
    )


def test_gateway_closes_known_local_verification_failure(monkeypatch) -> None:
    command_envelopes = []

    class RecordingCommandService:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def handle(self, envelope):
            command_envelopes.append(envelope)

    class RequestRepository:
        def get_by_execution_lineage(self, lineage_id, workspace_id):
            assert lineage_id == execution.lineage_id
            assert workspace_id == execution.workspace_id
            return SimpleNamespace(root_task_id=uuid4())

    class ExecutionRepository:
        def get(self, execution_id, workspace_id):
            assert execution_id == execution.id
            assert workspace_id == execution.workspace_id
            return SimpleNamespace(version=4)

    class Uow:
        execution_requests = RequestRepository()
        executions = ExecutionRepository()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            del exc_type, exc_value, traceback

    monkeypatch.setattr(
        worker_gateway_module, "CommandService", RecordingCommandService
    )
    execution = SimpleNamespace(
        id=uuid4(),
        lineage_id=uuid4(),
        workspace_id=uuid4(),
        version=3,
    )
    worker = SimpleNamespace(id=uuid4())
    context = SimpleNamespace(
        adapter_definition=SimpleNamespace(provider=LOCAL_VERIFICATION_PROVIDER),
        execution=execution,
        worker=worker,
        lease=SimpleNamespace(id=uuid4()),
        binding=SimpleNamespace(fencing_token=7),
    )
    report = EventIngestionReport(
        events=(),
        duplicate_sequences=(),
        terminal_observations=(
            TerminalEventObservation(
                event_type="session.failed",
                sequence=2,
                outcome="failed",
                payload={
                    "purpose": "verification",
                    "provider": LOCAL_VERIFICATION_PROVIDER,
                    "status": "failed",
                },
            ),
        ),
    )
    gateway = WorkerGateway(
        lambda: Uow(),
        adapter_factories={},
        workspace_factories={},
    )

    gateway._report_local_verification_completion(context, report)

    assert len(command_envelopes) == 1
    assert command_envelopes[0].command_type == "ReportExecutionSucceeded"
    assert command_envelopes[0].target_id == execution.id
    assert command_envelopes[0].expected_version == 4


def test_gateway_does_not_close_unknown_local_verification_outcome(monkeypatch) -> None:
    command_envelopes = []

    class RecordingCommandService:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def handle(self, envelope):
            command_envelopes.append(envelope)

    monkeypatch.setattr(worker_gateway_module, "CommandService", RecordingCommandService)
    execution = SimpleNamespace(
        id=uuid4(),
        lineage_id=uuid4(),
        workspace_id=uuid4(),
        version=3,
    )
    worker = SimpleNamespace(id=uuid4())
    context = SimpleNamespace(
        adapter_definition=SimpleNamespace(provider=LOCAL_VERIFICATION_PROVIDER),
        execution=execution,
        worker=worker,
        lease=SimpleNamespace(id=uuid4()),
        binding=SimpleNamespace(fencing_token=7),
    )
    report = EventIngestionReport(
        events=(),
        duplicate_sequences=(),
        terminal_observations=(
            TerminalEventObservation(
                event_type="session.failed",
                sequence=2,
                outcome="failed",
                payload={
                    "purpose": "verification",
                    "provider": LOCAL_VERIFICATION_PROVIDER,
                    "status": "unknown",
                    "execution_id": str(execution.id),
                    "worker_id": str(worker.id),
                },
            ),
        ),
    )
    gateway = WorkerGateway(
        lambda: None,  # type: ignore[return-value]
        adapter_factories={},
        workspace_factories={},
    )

    gateway._report_local_verification_completion(context, report)

    assert command_envelopes == []


def test_worker_gateway_stores_content_before_registry_and_strips_bytes() -> None:
    workspace_id = UUID("20000000-0000-0000-0000-000000000001")
    execution_id = UUID("70000000-0000-0000-0000-000000000001")
    content = b"report"
    content_writer = RecordingArtifactContentWriter()
    gateway = WorkerGateway(
        lambda: None,  # type: ignore[return-value]
        adapter_factories={},
        workspace_factories={},
        artifact_content_store=content_writer,
    )
    candidate = ArtifactCandidate(
        artifact_type=ArtifactType.REPORT,
        storage_ref="artifact://provider/report",
        content_hash=sha256(content).hexdigest(),
        size_bytes=len(content),
        media_type="text/plain",
        provenance={},
        content=content,
    )

    prepared = gateway._prepare_artifact_candidates(
        workspace_id=workspace_id,
        execution_id=execution_id,
        candidates=(candidate,),
    )

    assert prepared[0].storage_ref.startswith("artifact-local://")
    assert prepared[0].content is None
    assert content_writer.calls == [
        (workspace_id, execution_id, candidate.content_hash, content)
    ]


def test_worker_gateway_rejects_content_metadata_mismatch() -> None:
    content_writer = RecordingArtifactContentWriter()
    gateway = WorkerGateway(
        lambda: None,  # type: ignore[return-value]
        adapter_factories={},
        workspace_factories={},
        artifact_content_store=content_writer,
    )
    candidate = ArtifactCandidate(
        artifact_type=ArtifactType.REPORT,
        storage_ref="artifact://provider/report",
        content_hash=sha256(b"report").hexdigest(),
        size_bytes=100,
        media_type="text/plain",
        provenance={},
        content=b"report",
    )

    try:
        gateway._prepare_artifact_candidates(
            workspace_id=UUID("20000000-0000-0000-0000-000000000001"),
            execution_id=UUID("70000000-0000-0000-0000-000000000001"),
            candidates=(candidate,),
        )
    except GatewayMessageError as exc:
        assert "size" in str(exc)
    else:
        raise AssertionError("expected GatewayMessageError")


def test_runtime_module_lifecycle_matrix() -> None:
    for lifecycle in (ModuleLifecycle.ACTIVE, ModuleLifecycle.DRAINING):
        for operation in RuntimeModuleOperation:
            assert runtime_module_operation_allowed(lifecycle, operation)

    allowed_when_disabled = {
        RuntimeModuleOperation.OBSERVE,
        RuntimeModuleOperation.STOP,
        RuntimeModuleOperation.CLEANUP,
    }
    for operation in RuntimeModuleOperation:
        assert runtime_module_operation_allowed(
            ModuleLifecycle.DISABLED, operation
        ) is (operation in allowed_when_disabled)


def test_gateway_message_error_preserves_stable_code() -> None:
    error = GatewayMessageError(
        "the runtime binding is missing", code="runtime_binding_missing"
    )

    assert error.code == "runtime_binding_missing"


def test_verification_spec_is_pinned_to_the_candidate_revision() -> None:
    baseline = "a" * 40
    candidate = "b" * 40
    spec = ExecutionSpec(
        schema_version=1,
        workspace_id=uuid4(),
        task_id=uuid4(),
        execution_id=uuid4(),
        worker_id=uuid4(),
        lease_id=uuid4(),
        fencing_token=1,
        goal="run verification",
        acceptance_criteria=("the candidate passes",),
        context_package_ref=None,
        repositories=(
            RepositoryCheckoutSpec(
                repository_ref="git-repository://demo",
                base_revision=baseline,
                checkout_mode=CheckoutMode.WORKTREE,
                writable_paths=(".",),
                readable_paths=(".",),
                branch_policy=BranchPolicy.EXECUTION,
            ),
        ),
        tool_policy={},
        sandbox_policy={"mode": "filesystem"},
        budget={"max_seconds": 60},
        deadline=NOW,
        expected_outputs=("verification",),
        adapter_module_ref=uuid4(),
        adapter_config_version=1,
        workspace_module_ref=uuid4(),
        workspace_config_version=1,
    )

    bound = _bind_spec_to_revision(spec, candidate)

    assert bound.repositories[0].base_revision == candidate
    assert bound.repositories[0].repository_ref == spec.repositories[0].repository_ref
    assert spec.repositories[0].base_revision == baseline


def test_gateway_binds_context_ack_to_exact_adapter_runtime_snapshot() -> None:
    materialization = _materialization_v1()
    operation_id = uuid4()
    metadata = materialization.metadata
    acknowledgement = ContextAcknowledgement(
        package_id=metadata.package_id,
        package_hash=metadata.package_hash,
        consumer_type=metadata.consumer_type,
        consumer_id=metadata.consumer_id,
        context_version=metadata.context_version,
        adapter_module_id=uuid4(),
        adapter_config_version=1,
        acknowledged_at=NOW,
        schema_version=materialization.schema_version,
        operation_id=operation_id,
    )
    adapter_module_id = uuid4()

    bound = WorkerGateway._validate_context_acknowledgement(
        acknowledgement,
        materialization=materialization,
        operation_id=operation_id,
        adapter_module_id=adapter_module_id,
        adapter_config_version=1,
    )

    assert bound.adapter_module_id == adapter_module_id
    assert bound.adapter_config_version == 1


@pytest.mark.parametrize(
    "mutation",
    ("operation", "consumer", "package", "config"),
)
def test_gateway_rejects_context_ack_identity_or_stale_config(
    mutation: str,
) -> None:
    materialization = _materialization_v1()
    operation_id = uuid4()
    metadata = materialization.metadata
    acknowledgement = ContextAcknowledgement(
        package_id=metadata.package_id,
        package_hash=metadata.package_hash,
        consumer_type=metadata.consumer_type,
        consumer_id=metadata.consumer_id,
        context_version=metadata.context_version,
        adapter_module_id=uuid4(),
        adapter_config_version=1,
        acknowledged_at=NOW,
        schema_version=materialization.schema_version,
        operation_id=operation_id,
    )
    if mutation == "operation":
        acknowledgement = replace(acknowledgement, operation_id=None)
    elif mutation == "consumer":
        acknowledgement = replace(acknowledgement, consumer_id=uuid4())
    elif mutation == "package":
        acknowledgement = replace(acknowledgement, package_id=uuid4())
    elif mutation == "config":
        acknowledgement = replace(acknowledgement, adapter_config_version=2)
    else:
        raise AssertionError(f"unsupported mutation: {mutation}")

    with pytest.raises(GatewayMessageError) as caught:
        WorkerGateway._validate_context_acknowledgement(
            acknowledgement,
            materialization=materialization,
            operation_id=operation_id,
            adapter_module_id=uuid4(),
            adapter_config_version=1,
        )

    assert caught.value.code == "context_acknowledgement_mismatch"


def test_gateway_rejects_a_binding_to_a_missing_module_instance() -> None:
    class EmptyModules:
        def get_instance(self, module_id, workspace_id):
            del module_id, workspace_id

    class FakeUnitOfWork:
        modules = EmptyModules()

    with pytest.raises(GatewayMessageError) as caught:
        _load_module(
            FakeUnitOfWork(),  # type: ignore[arg-type]
            uuid4(),
            uuid4(),
            expected_type="worker_adapter",
        )

    assert caught.value.code == "runtime_binding_missing"
