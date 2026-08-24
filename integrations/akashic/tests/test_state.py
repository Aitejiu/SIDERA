import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from sidera_task_bridge.state import (
    STATE_KEY,
    BridgeStateStore,
    NotificationSnapshot,
    OutboundCommand,
    PendingNotification,
    PrivateRoute,
    ResourceSourceBinding,
    StateConflict,
    StateCorrupt,
    UndeliverableAudit,
)


class MemoryKV:
    def __init__(self, value: object = None) -> None:
        self.value = value
        self.writes = 0

    def get(self, key: str, default: object = None) -> object:
        assert key == STATE_KEY
        return self.value if self.value is not None else default

    def set(self, key: str, value: object) -> None:
        assert key == STATE_KEY
        self.value = value
        self.writes += 1


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


def notification(
    notice_id: str,
    *,
    cursor: int,
    created_at: datetime,
) -> PendingNotification:
    return PendingNotification(
        notice_id=notice_id,
        cursor=cursor,
        resource_key="planning_request:00000000-0000-0000-0000-000000000040",
        source_ref="src_private",
        kind="planning_clarification",
        message="需要补充规划信息",
        web_url="http://127.0.0.1:4174/workspaces/ws/inbox",
        created_at=created_at,
        last_attempted_at=None,
        attempts=0,
    )


def test_v1_state_migrates_once_and_preserves_every_legacy_field() -> None:
    async def scenario() -> None:
        item = command()
        legacy = {
            "schema_version": 1,
            "unresolved_commands": {str(item.command_id): item.model_dump(mode="json")},
            "source_routes": {
                "src_private": {
                    "channel": "telegram",
                    "chat_id": "chat-42",
                    "session_key": "telegram:chat-42",
                }
            },
            "settled_commands": [
                {
                    "command_id": str(item.command_id),
                    "outcome": "rejected",
                    "error_code": "conflict",
                    "settled_at": item.created_at.isoformat(),
                }
            ],
        }
        kv = MemoryKV(legacy)
        state = await BridgeStateStore(kv).snapshot()

        assert state.schema_version == 2
        assert state.unresolved_commands[str(item.command_id)] == item
        assert state.source_routes["src_private"].chat_id == "chat-42"
        assert state.settled_commands[0].error_code == "conflict"
        assert state.notification_cursor == 0
        assert state.pending_notifications == []
        assert isinstance(kv.value, dict)
        assert kv.value["schema_version"] == 2
        assert kv.writes == 1

    asyncio.run(scenario())


def test_v2_state_rejects_unknown_schema_bad_uuid_negative_cursor_and_duplicate_pending() -> None:
    async def scenario() -> None:
        invalid_states = (
            {"schema_version": 3},
            {
                "schema_version": 2,
                "resource_sources": {
                    "task:not-a-uuid": {
                        "resource_kind": "task",
                        "resource_id": "not-a-uuid",
                        "source_type": "akashic_private",
                        "source_ref": "src",
                        "bound_at": "2026-08-24T03:30:00Z",
                    }
                },
            },
            {"schema_version": 2, "notification_cursor": -1},
            {
                "schema_version": 2,
                "pending_notifications": [
                    notification(
                        "duplicate",
                        cursor=1,
                        created_at=datetime(2026, 8, 24, 3, 30, tzinfo=UTC),
                    ).model_dump(mode="json"),
                    notification(
                        "duplicate",
                        cursor=2,
                        created_at=datetime(2026, 8, 24, 3, 31, tzinfo=UTC),
                    ).model_dump(mode="json"),
                ],
            },
        )
        for raw in invalid_states:
            with pytest.raises(StateCorrupt):
                await BridgeStateStore(MemoryKV(raw)).snapshot()

    asyncio.run(scenario())


def test_notification_state_is_sorted_bounded_and_cursor_cannot_cross_pending() -> None:
    async def scenario() -> None:
        store = BridgeStateStore(MemoryKV())
        first_at = datetime(2026, 8, 24, 3, 30, tzinfo=UTC)
        await store.enqueue_notification(notification("second", cursor=2, created_at=first_at + timedelta(seconds=2)))
        await store.enqueue_notification(notification("first", cursor=1, created_at=first_at))
        assert [item.notice_id for item in await store.list_pending_notifications()] == [
            "first",
            "second",
        ]

        with pytest.raises(StateConflict):
            await store.advance_notification_cursor(3)
        await store.advance_notification_cursor(1)
        assert (await store.snapshot()).notification_cursor == 1

        for index in range(140):
            await store.save_notification_snapshot(
                f"task:{index}",
                NotificationSnapshot(
                    status="running",
                    version=index,
                    milestone_fingerprint=f"milestone-{index}",
                    attention_fingerprint=None,
                ),
            )
            await store.record_undeliverable(
                UndeliverableAudit(
                    notice_id=f"notice-{index}",
                    resource_key=f"task:{index}",
                    kind="task_blocked",
                    reason_code="route_missing",
                    recorded_at=first_at + timedelta(seconds=index),
                )
            )
        state = await store.snapshot()
        assert len(state.notification_snapshots) == 128
        assert len(state.undeliverable_notifications) == 128
        assert "task:0" not in state.notification_snapshots
        assert state.undeliverable_notifications[0].notice_id == "notice-12"

    asyncio.run(scenario())


def test_resource_source_binding_is_immutable_and_materialized_task_inherits_owner() -> None:
    async def scenario() -> None:
        store = BridgeStateStore(MemoryKV())
        request_id = UUID("00000000-0000-0000-0000-000000000040")
        task_id = UUID("00000000-0000-0000-0000-000000000041")
        await store.bind_resource_source(
            "planning_request",
            request_id,
            "akashic_private",
            "src_private",
        )
        await store.bind_resource_source(
            "planning_request",
            request_id,
            "akashic_private",
            "src_private",
        )
        with pytest.raises(StateConflict):
            await store.bind_resource_source(
                "planning_request",
                request_id,
                "akashic_private",
                "src_other",
            )

        await store.bind_materialized_task(request_id, task_id)
        state = await store.snapshot()
        binding = state.resource_sources[f"task:{task_id}"]
        assert isinstance(binding, ResourceSourceBinding)
        assert binding.source_ref == "src_private"
        assert binding.source_type == "akashic_private"

    asyncio.run(scenario())
