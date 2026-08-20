from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
from sidera_control_plane.application.worker_gateway import (
    ASSIGNMENT_TOPIC,
    STOP_TOPIC,
    GatewayAssignmentRunner,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


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
        self.claim_topics: set[str] | None = None
        self.acknowledged: list[tuple[object, object]] = []
        self.rescheduled: list[tuple[object, object, datetime, str]] = []

    def claim(self, batch_size: int, *, topics=None):
        assert batch_size == 20
        self.claim_topics = set(topics or ())
        return self.messages

    def acknowledge(self, message_id, claim_token) -> bool:
        self.acknowledged.append((message_id, claim_token))
        return True

    def reschedule(self, message_id, claim_token, next_attempt_at, error) -> bool:
        self.rescheduled.append((message_id, claim_token, next_attempt_at, error))
        return True


class RecordingGateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[OutboxMessage] = []
        self.fail = fail

    def handle(self, message: OutboxMessage) -> object:
        self.messages.append(message)
        if self.fail:
            raise RuntimeError("gateway unavailable")


def test_gateway_runner_claims_assignment_and_stop_topics_and_acknowledges() -> None:
    message = _message()
    store = RecordingStore((message,))
    gateway = RecordingGateway()
    runner = GatewayAssignmentRunner(store, gateway, clock=lambda: NOW)

    assert runner.run_once() == (1, 1, 0)
    assert store.claim_topics == {ASSIGNMENT_TOPIC, STOP_TOPIC}
    assert gateway.messages == [message]
    assert store.acknowledged == [(message.id, message.claim_token)]


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
