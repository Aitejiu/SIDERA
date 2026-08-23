import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from sidera_task_bridge.state import (
    STATE_KEY,
    BridgeStateStore,
    OutboundCommand,
    PrivateRoute,
    StateConflict,
    StateCorrupt,
)


class MemoryKV:
    def __init__(self, value: object = None) -> None:
        self.value = value

    def get(self, key: str, default: object = None) -> object:
        assert key == STATE_KEY
        return self.value if self.value is not None else default

    def set(self, key: str, value: object) -> None:
        assert key == STATE_KEY
        self.value = value


def command(
    command_id: str = "00000000-0000-0000-0000-000000000010",
    *,
    resource_lock_key: str = "planning:request-1",
    payload: str = "first",
) -> OutboundCommand:
    now = datetime(2026, 8, 24, 3, 30, tzinfo=UTC)
    return OutboundCommand(
        command_id=UUID(command_id),
        resource_lock_key=resource_lock_key,
        request_json={"command_id": command_id, "payload": payload},
        created_at=now,
        last_attempted_at=now,
        attempt_count=0,
    )


def test_reserve_writes_before_sender_and_reuses_exact_command() -> None:
    async def scenario() -> None:
        kv = MemoryKV()
        store = BridgeStateStore(kv)
        original = command()

        reserved = await store.reserve(original)
        replay = await store.reserve(original.model_copy(deep=True))

        assert reserved == original
        assert replay == original
        assert isinstance(kv.value, dict)
        assert str(original.command_id) in kv.value["unresolved_commands"]

        with pytest.raises(StateConflict):
            await store.reserve(command(payload="changed"))

    asyncio.run(scenario())


def test_terminal_settle_removes_body_and_keeps_bounded_audit() -> None:
    async def scenario() -> None:
        kv = MemoryKV()
        store = BridgeStateStore(kv)
        item = command()
        await store.reserve(item)
        await store.settle(item.command_id, outcome="applied", error_code=None)

        state = await store.snapshot()
        assert state.unresolved_commands == {}
        assert state.settled_commands[-1].command_id == item.command_id
        assert state.settled_commands[-1].error_code is None
        assert "payload" not in state.settled_commands[-1].model_dump_json()

        for index in range(130):
            current = command(
                command_id=f"00000000-0000-0000-0000-{index + 100:012d}",
            )
            await store.reserve(current)
            await store.settle(current.command_id, outcome="rejected", error_code="conflict")
        assert len((await store.snapshot()).settled_commands) == 128

    asyncio.run(scenario())


def test_unknown_attempt_preserves_json_equivalent_request() -> None:
    async def scenario() -> None:
        kv = MemoryKV()
        store = BridgeStateStore(kv)
        item = command()
        await store.reserve(item)
        await store.record_attempt(item.command_id, at=item.created_at + timedelta(seconds=5))

        unresolved = await store.get_unresolved(item.command_id)
        assert unresolved is not None
        assert json.loads(json.dumps(unresolved.request_json)) == {
            "command_id": str(item.command_id),
            "payload": "first",
        }
        assert unresolved.attempt_count == 1

    asyncio.run(scenario())


def test_state_fails_loud_on_corrupt_or_unknown_schema() -> None:
    async def scenario() -> None:
        for raw in (
            {"schema_version": 999},
            {"schema_version": 1, "unresolved_commands": "broken"},
        ):
            with pytest.raises(StateCorrupt):
                await BridgeStateStore(MemoryKV(raw)).snapshot()

    asyncio.run(scenario())


def test_concurrent_mutations_are_serialized_without_lost_updates() -> None:
    async def scenario() -> None:
        store = BridgeStateStore(MemoryKV())
        items = [
            command(
                command_id=f"00000000-0000-0000-0000-{index + 200:012d}",
                resource_lock_key=f"planning:{index}",
            )
            for index in range(20)
        ]

        await asyncio.gather(*(store.reserve(item) for item in items))
        state = await store.snapshot()
        assert set(state.unresolved_commands) == {
            str(item.command_id) for item in items
        }

    asyncio.run(scenario())


def test_private_route_is_plugin_data_and_does_not_accept_arbitrary_values() -> None:
    route = PrivateRoute(
        channel="telegram",
        chat_id="chat-42",
        session_key="telegram:chat-42",
    )
    assert route.model_dump() == {
        "channel": "telegram",
        "chat_id": "chat-42",
        "session_key": "telegram:chat-42",
    }
