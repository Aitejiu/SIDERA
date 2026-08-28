from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Self, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.application.input_dispatcher import (
    InputDispatcher,
    InputDispatchReport,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_gateway import GatewayRecoveryRequired
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.gateway import DeliveryOutcome, DeliveryResult
from sidera_control_plane.domain.inputs import ExecutionInput, ExecutionInputStatus

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class RecordingInputRepository:
    def __init__(self, items: list[ExecutionInput]) -> None:
        self.items = {item.id: item for item in items}

    def claim_queued(self, limit: int) -> tuple[ExecutionInput, ...]:
        queued = sorted(
            (
                item
                for item in self.items.values()
                if item.status is ExecutionInputStatus.QUEUED
            ),
            key=lambda item: (item.created_at, str(item.id)),
        )[:limit]
        for item in queued:
            item.begin_dispatch(
                operation_id=uuid5(NAMESPACE_URL, f"sidera.input:{item.id}")
            )
        return tuple(queued)

    def get(
        self, input_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ExecutionInput | None:
        del for_update
        item = self.items.get(input_id)
        return item if item is not None and item.workspace_id == workspace_id else None

    def save(self, item: ExecutionInput, previous_version: int) -> None:
        assert item.version == previous_version + 1
        self.items[item.id] = item


class RecordingEvents:
    def __init__(self) -> None:
        self.events: list[EventData] = []

    def append_with_outbox(self, event: EventData) -> None:
        self.events.append(event)


class FakeUnitOfWork(AbstractContextManager["FakeUnitOfWork"]):
    def __init__(
        self,
        repository: RecordingInputRepository,
        events: RecordingEvents,
        *,
        worker_item_id: UUID | None = None,
        active_transition: object | None = None,
    ) -> None:
        self.execution_inputs = repository
        self.events = events
        self.executions = _ExecutionRepository(worker_item_id)
        self.worker_transitions = _WorkerTransitionRepository(active_transition)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _ExecutionRepository:
    def __init__(self, worker_item_id: UUID | None) -> None:
        self.worker_item_id = worker_item_id

    def get(
        self,
        execution_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> object:
        del execution_id, workspace_id, for_update
        return SimpleNamespace(worker_item_id=self.worker_item_id)


class _WorkerTransitionRepository:
    def __init__(self, active_transition: object | None) -> None:
        self.active_transition = active_transition

    def get_active_for_worker_item(
        self,
        worker_item_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> object | None:
        del worker_item_id, workspace_id, for_update
        return self.active_transition


class RecordingGateway:
    def __init__(self, *outcomes: DeliveryResult | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[ExecutionInput] = []

    def send_input(self, item: ExecutionInput) -> DeliveryResult:
        self.calls.append(item)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return DeliveryResult(
            operation_id=item.operation_id or uuid4(),
            outcome=outcome.outcome,
            accepted_sequence=outcome.accepted_sequence,
            failure_code=outcome.failure_code,
        )


def _input() -> ExecutionInput:
    item = ExecutionInput.accept(
        id=uuid4(),
        workspace_id=uuid4(),
        execution_id=uuid4(),
        session_id=uuid4(),
        client_idempotency_key="client-1",
        text="continue",
        attachment_refs=(),
        now=NOW,
    )
    item.queue()
    return item


def _dispatcher(
    item: ExecutionInput,
    gateway: RecordingGateway,
    *,
    worker_item_id: UUID | None = None,
    active_transition: object | None = None,
) -> tuple[InputDispatcher, RecordingInputRepository, RecordingEvents]:
    repository = RecordingInputRepository([item])
    events = RecordingEvents()
    uow_factory: Callable[[], FakeUnitOfWork] = lambda: FakeUnitOfWork(
        repository,
        events,
        worker_item_id=worker_item_id,
        active_transition=active_transition,
    )
    return (
        InputDispatcher(
            lambda: cast(ControlPlaneUnitOfWork, uow_factory()),
            gateway,
            clock=lambda: NOW,
        ),
        repository,
        events,
    )


def test_dispatcher_persists_delivery_and_emits_audit_event() -> None:
    item = _input()
    gateway = RecordingGateway(
        DeliveryResult(
            operation_id=uuid4(),
            outcome=DeliveryOutcome.DELIVERED,
            accepted_sequence=7,
            failure_code=None,
        )
    )
    dispatcher, repository, events = _dispatcher(item, gateway)

    assert dispatcher.run_once() == InputDispatchReport(
        claimed=1, delivered=1, deferred=0, blocked=0, unknown=0, failed=0
    )
    assert repository.items[item.id].status is ExecutionInputStatus.DELIVERED
    assert events.events[-1].event_type == "ExecutionInputDelivered"
    assert events.events[-1].payload["accepted_sequence"] == 7


def test_prompt_busy_returns_input_to_queue_without_failure() -> None:
    item = _input()
    gateway = RecordingGateway(
        DeliveryResult(
            operation_id=uuid4(),
            outcome=DeliveryOutcome.BLOCKED,
            accepted_sequence=None,
            failure_code="prompt_busy",
        ),
        DeliveryResult(
            operation_id=uuid4(),
            outcome=DeliveryOutcome.DELIVERED,
            accepted_sequence=8,
            failure_code=None,
        ),
    )
    dispatcher, repository, _events = _dispatcher(item, gateway)

    assert dispatcher.run_once().deferred == 1
    assert repository.items[item.id].status is ExecutionInputStatus.QUEUED
    assert dispatcher.run_once().delivered == 1
    assert repository.items[item.id].status is ExecutionInputStatus.DELIVERED
    assert len(gateway.calls) == 2


def test_unknown_result_is_blocked_and_never_automatically_retried() -> None:
    item = _input()
    gateway = RecordingGateway(
        DeliveryResult(
            operation_id=uuid4(),
            outcome=DeliveryOutcome.UNKNOWN,
            accepted_sequence=None,
            failure_code="acp_response_timeout",
        )
    )
    dispatcher, repository, _events = _dispatcher(item, gateway)

    first = dispatcher.run_once()
    second = dispatcher.run_once()

    assert first == InputDispatchReport(
        claimed=1, delivered=0, deferred=0, blocked=1, unknown=1, failed=0
    )
    assert second.claimed == 0
    assert repository.items[item.id].status is ExecutionInputStatus.BLOCKED
    assert repository.items[item.id].failure_code == "acp_response_timeout"
    assert len(gateway.calls) == 1


def test_gateway_recovery_error_is_conservatively_blocked() -> None:
    item = _input()
    gateway = RecordingGateway(GatewayRecoveryRequired("reconcile"))
    dispatcher, repository, _events = _dispatcher(item, gateway)

    dispatcher.run_once()

    assert repository.items[item.id].status is ExecutionInputStatus.BLOCKED
    assert repository.items[item.id].failure_code == "gateway_recovery_required"


def test_claimed_input_is_requeued_when_worker_transition_holds_the_fence() -> None:
    item = _input()
    gateway = RecordingGateway(
        DeliveryResult(
            operation_id=uuid4(),
            outcome=DeliveryOutcome.DELIVERED,
            accepted_sequence=9,
            failure_code=None,
        )
    )
    transition = SimpleNamespace(status="waiting_safe_boundary")
    dispatcher, repository, _events = _dispatcher(
        item,
        gateway,
        worker_item_id=uuid4(),
        active_transition=transition,
    )

    report = dispatcher.run_once()

    assert report.deferred == 1
    assert repository.items[item.id].status is ExecutionInputStatus.QUEUED
    assert gateway.calls == []
