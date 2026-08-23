from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from sidera_main_agent_gateway.contracts import MainAgentEvent
from sidera_main_agent_gateway.errors import TurnStreamUnavailable
from sidera_main_agent_gateway.relay import (
    RelayAlreadyActive,
    RelayCursorExpired,
    TurnRelayRegistry,
)


class FakeHandle:
    def __init__(
        self,
        thread_id: str,
        turn_id: str,
        events: list[MainAgentEvent] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.id = turn_id
        self._events = events or []
        self._error = error
        self.cancelled = False

    async def events(self) -> AsyncIterator[MainAgentEvent]:
        for event in self._events:
            await asyncio.sleep(0)
            yield event
        if self._error is not None:
            raise self._error

    async def result(self) -> Any:
        return None

    async def interrupt(self) -> Any:
        return None


def provider_event(event_type: str, sequence: int = 1) -> MainAgentEvent:
    return MainAgentEvent(
        sequence=sequence,
        type=event_type,  # type: ignore[arg-type]
        data={"providerSequence": sequence},
    )


async def collect_events(relay: Any, after: int = 0) -> list[Any]:
    return [event async for event in relay.events(after=after)]


@pytest.mark.asyncio
async def test_relay_assigns_monotonic_sequences_and_supports_two_subscribers() -> None:
    registry = TurnRelayRegistry(event_limit=8)
    handle = FakeHandle(
        "thread-1",
        "turn-1",
        [provider_event("started"), provider_event("completed", 2)],
    )
    relay = await registry.start(handle)

    first, second = await asyncio.gather(
        collect_events(relay),
        collect_events(relay),
    )

    assert [event.sequence for event in first] == [1, 2]
    assert [event.sequence for event in second] == [1, 2]
    assert [event.event for event in first] == ["started", "completed"]
    await registry.shutdown()


@pytest.mark.asyncio
async def test_browser_subscriber_disconnect_does_not_cancel_provider_consumer() -> None:
    registry = TurnRelayRegistry(event_limit=8)
    handle = FakeHandle(
        "thread-1",
        "turn-1",
        [provider_event("started"), provider_event("completed", 2)],
    )
    relay = await registry.start(handle)
    subscriber = relay.events()

    first = await anext(subscriber)
    await cast(Any, subscriber).aclose()
    await relay.wait_closed()
    replay = await collect_events(relay)

    assert first.event == "started"
    assert [event.event for event in replay] == ["started", "completed"]
    assert handle.cancelled is False
    await registry.shutdown()


@pytest.mark.asyncio
async def test_terminal_replay_and_cursor_expiry_are_explicit() -> None:
    registry = TurnRelayRegistry(event_limit=2)
    handle = FakeHandle(
        "thread-1",
        "turn-1",
        [
            provider_event("started"),
            provider_event("item", 2),
            provider_event("completed", 3),
        ],
    )
    relay = await registry.start(handle)
    await relay.wait_closed()

    with pytest.raises(RelayCursorExpired):
        _ = await collect_events(relay, after=0)

    replay = await collect_events(relay, after=1)
    assert [event.sequence for event in replay] == [2, 3]
    await registry.shutdown()


@pytest.mark.asyncio
async def test_stream_error_appends_one_stream_lost_and_closes_relay() -> None:
    registry = TurnRelayRegistry(event_limit=8)
    relay = await registry.start(
        FakeHandle(
            "thread-1",
            "turn-1",
            [provider_event("started")],
            error=TurnStreamUnavailable("provider disconnected"),
        )
    )
    await relay.wait_closed()

    events = await collect_events(relay)

    assert [event.event for event in events] == ["started", "stream_lost"]
    await registry.shutdown()


@pytest.mark.asyncio
async def test_registry_rejects_two_active_relays_for_same_thread_and_turn() -> None:
    registry = TurnRelayRegistry(event_limit=8)
    first = await registry.start(
        FakeHandle("thread-1", "turn-1", [provider_event("started")])
    )

    with pytest.raises(RelayAlreadyActive):
        await registry.start(
            FakeHandle("thread-1", "turn-1", [provider_event("started")])
        )

    with pytest.raises(RelayAlreadyActive):
        await registry.start(
            FakeHandle("thread-1", "turn-2", [provider_event("started")])
        )

    await first.shutdown()
    await registry.shutdown()


@pytest.mark.asyncio
async def test_active_turn_without_relay_reports_poll_recovery_mode() -> None:
    registry = TurnRelayRegistry(event_limit=8)
    availability = registry.availability("thread-1", "turn-1", "in_progress")

    assert availability.stream_available is False
    assert availability.recovery_mode == "poll"
    assert registry.availability("thread-1", "turn-1", "completed").recovery_mode is None
