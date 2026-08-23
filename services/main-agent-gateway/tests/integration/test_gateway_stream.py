from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sidera_main_agent_gateway.contracts import MainAgentEvent
from sidera_main_agent_gateway.relay import TurnRelayRegistry


class QueueHandle:
    thread_id = "thread-queue"
    id = "turn-queue"

    def __init__(self) -> None:
        self.queue: asyncio.Queue[MainAgentEvent | None] = asyncio.Queue()

    async def events(self) -> Any:
        while True:
            event = await self.queue.get()
            if event is None:
                return
            yield event

    async def result(self) -> Any:
        return None

    async def interrupt(self) -> Any:
        return None


@pytest.mark.asyncio
async def test_gateway_relay_keeps_provider_stream_alive_after_browser_disconnect() -> None:
    registry = TurnRelayRegistry(event_limit=16)
    provider = QueueHandle()
    relay = await registry.start(provider)
    subscriber = relay.events()

    await provider.queue.put(MainAgentEvent(1, "started", {"phase": "work"}))
    first = await anext(subscriber)
    await subscriber.aclose()

    await provider.queue.put(MainAgentEvent(2, "completed", {"phase": "done"}))
    await provider.queue.put(None)
    await relay.wait_closed()
    replay = [event async for event in relay.events()]

    assert first.event == "started"
    assert [event.event for event in replay] == ["started", "completed"]
    await registry.shutdown()
