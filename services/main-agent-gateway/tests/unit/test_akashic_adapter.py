from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from akashic_sdk.client import ConnectionClosedError, RemoteError

from sidera_main_agent_gateway.adapters.akashic import AkashicAdapter
from sidera_main_agent_gateway.config import Settings
from sidera_main_agent_gateway.errors import (
    ProviderProtocolError,
    ProviderUnavailable,
    ThreadAccessDenied,
    ThreadBusy,
)

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000002")
OTHER_WORKSPACE_ID = UUID("30000000-0000-0000-0000-000000000003")
CREATED_AT = "2026-08-24T12:00:00Z"


def settings() -> Settings:
    return Settings(
        akashic_endpoint="/tmp/akashic/control.sock",
        sidera_workspace_id=WORKSPACE_ID,
    )


def thread_record(
    thread_id: str,
    *,
    source: str = "sidera_web",
    workspace_id: UUID = WORKSPACE_ID,
    title: str | None = "Gateway thread",
    context_refs: list[str] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "source": source,
        "sidera_workspace_id": str(workspace_id),
    }
    if title is not None:
        metadata["title"] = title
    if context_refs is not None:
        metadata["context_refs"] = context_refs
    return {
        "id": thread_id,
        "source": "programmatic",
        "createdAt": CREATED_AT,
        "updatedAt": CREATED_AT,
        "metadata": metadata,
    }


def turn_record(
    thread_id: str,
    turn_id: str = "turn-1",
    *,
    status: str = "completed",
    item_type: str = "assistantMessage",
) -> dict[str, Any]:
    return {
        "id": turn_id,
        "threadId": thread_id,
        "status": status,
        "input": "hello",
        "createdAt": CREATED_AT,
        "startedAt": CREATED_AT,
        "completedAt": CREATED_AT if status == "completed" else None,
        "finalResponse": "done" if status == "completed" else None,
        "items": [
            {
                "id": "item-1",
                "type": item_type,
                "data": {"content": "done"},
            }
        ],
        "usage": {"inputTokens": 1},
        "error": None,
    }


class FakeTurnHandle:
    def __init__(self, thread_id: str, turn_id: str, record: dict[str, Any]) -> None:
        self.thread_id = thread_id
        self.id = turn_id
        self.record = record
        self.events_seen: list[dict[str, Any]] = []

    async def events(self) -> Any:
        for event in self.events_seen:
            yield event

    async def result(self) -> dict[str, Any]:
        return self.record

    async def interrupt(self) -> dict[str, Any]:
        return self.record


class FakeThread:
    def __init__(self, client: FakeClient, record: dict[str, Any]) -> None:
        self.client = client
        self.record = record
        self.id = str(record["id"])

    async def turn(self, input_text: str) -> FakeTurnHandle:
        self.client.turn_inputs.append(input_text)
        if self.client.turn_error is not None:
            raise self.client.turn_error
        handle = FakeTurnHandle(
            self.id,
            "turn-1",
            turn_record(self.id),
        )
        self.client.handles.append(handle)
        return handle


class FakeClient:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = {str(record["id"]): record for record in records}
        self.started_metadata: list[dict[str, Any]] = []
        self.turn_inputs: list[str] = []
        self.handles: list[FakeTurnHandle] = []
        self.turn_error: BaseException | None = None
        self.list_error: BaseException | None = None
        self.closed = False

    async def thread_list(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        if self.list_error is not None:
            raise self.list_error
        return {"data": list(self.records.values())[:limit], "nextCursor": None}

    async def thread_start(self, metadata: dict[str, Any]) -> FakeThread:
        self.started_metadata.append(metadata)
        record = thread_record(
            "new-thread",
            title=metadata.get("title"),
            context_refs=metadata.get("context_refs", []),
        )
        record["metadata"] = metadata
        self.records["new-thread"] = record
        return FakeThread(self, record)

    async def thread_read(self, thread_id: str, *, include_turns: bool = True) -> dict[str, Any]:
        return self.records[thread_id]

    async def thread_resume(self, thread_id: str) -> FakeThread:
        return FakeThread(self, self.records[thread_id])

    async def turn_read(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return turn_record(thread_id, turn_id)

    async def close(self) -> None:
        self.closed = True


def factory_for(
    *clients: FakeClient,
) -> Any:
    remaining = list(clients)

    async def factory(*_args: Any, **_kwargs: Any) -> FakeClient:
        if len(remaining) == 1:
            return remaining[0]
        return remaining.pop(0)

    return factory


@pytest.mark.asyncio
async def test_list_filters_private_and_other_workspace_threads() -> None:
    client = FakeClient(
        [
            thread_record("allowed"),
            thread_record("private", source="telegram"),
            thread_record("other", workspace_id=OTHER_WORKSPACE_ID),
        ]
    )
    adapter = AkashicAdapter(settings(), client_factory=factory_for(client))

    page = await adapter.list_threads()

    assert [thread.id for thread in page.threads] == ["allowed"]


@pytest.mark.asyncio
async def test_read_rejects_out_of_scope_thread_after_metadata_check() -> None:
    client = FakeClient([thread_record("private", source="telegram")])
    adapter = AkashicAdapter(settings(), client_factory=factory_for(client))

    with pytest.raises(ThreadAccessDenied):
        await adapter.read_thread("private")


@pytest.mark.asyncio
async def test_create_forces_web_scope_and_deduplicates_context_refs() -> None:
    client = FakeClient([])
    adapter = AkashicAdapter(settings(), client_factory=factory_for(client))

    created = await adapter.create_thread(
        title="New conversation",
        context_refs=("task:1", "task:1", "worker:2"),
    )

    metadata = client.started_metadata[0]
    assert metadata["source"] == "sidera_web"
    assert metadata["sidera_workspace_id"] == str(WORKSPACE_ID)
    assert metadata["title"] == "New conversation"
    assert metadata["context_refs"] == ["task:1", "worker:2"]
    assert created.context_refs == ("task:1", "worker:2")


@pytest.mark.asyncio
async def test_create_rejects_overlong_context_refs() -> None:
    client = FakeClient([])
    adapter = AkashicAdapter(settings(), client_factory=factory_for(client))

    with pytest.raises(ValueError, match="context ref"):
        await adapter.create_thread(title="New", context_refs=("x" * 201,))

    with pytest.raises(ValueError, match="20"):
        await adapter.create_thread(
            title="New",
            context_refs=tuple(f"task:{index}" for index in range(21)),
        )


@pytest.mark.asyncio
async def test_title_falls_back_to_local_created_date() -> None:
    client = FakeClient([thread_record("date-title", title=None)])
    adapter = AkashicAdapter(settings(), client_factory=factory_for(client))

    thread = await adapter.read_thread("date-title")

    assert thread.title == "对话 2026-08-24"


@pytest.mark.asyncio
async def test_read_reconnects_once_after_connection_loss() -> None:
    broken = FakeClient([])
    broken.list_error = ConnectionClosedError("closed")
    healthy = FakeClient([thread_record("allowed")])
    adapter = AkashicAdapter(
        settings(),
        client_factory=factory_for(broken, healthy),
    )

    page = await adapter.list_threads()

    assert [thread.id for thread in page.threads] == ["allowed"]
    assert broken.closed is True


@pytest.mark.asyncio
async def test_turn_start_does_not_retry_after_connection_loss() -> None:
    client = FakeClient([thread_record("allowed")])
    client.turn_error = ConnectionClosedError("closed during turn start")
    adapter = AkashicAdapter(settings(), client_factory=factory_for(client))

    with pytest.raises(ProviderUnavailable):
        await adapter.start_turn("allowed", "hello")

    assert client.turn_inputs == ["hello"]


@pytest.mark.asyncio
async def test_busy_remote_error_maps_to_thread_busy_without_body_leak() -> None:
    client = FakeClient([thread_record("allowed")])
    client.turn_error = RemoteError(-32011, "busy", {"retryable": True, "secret": "x"})
    adapter = AkashicAdapter(settings(), client_factory=factory_for(client))

    with pytest.raises(ThreadBusy) as captured:
        await adapter.start_turn("allowed", "hello")

    assert "secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_turn_mapping_rejects_unknown_provider_item_type() -> None:
    client = FakeClient([thread_record("allowed")])
    adapter = AkashicAdapter(settings(), client_factory=factory_for(client))

    with pytest.raises(ProviderProtocolError, match="item type"):
        await adapter._map_turn(turn_record("allowed", item_type="reasoning"))


@pytest.mark.asyncio
async def test_turn_handle_maps_neutral_events_and_terminal_result() -> None:
    client = FakeClient([thread_record("allowed")])
    adapter = AkashicAdapter(settings(), client_factory=factory_for(client))
    handle = FakeTurnHandle(
        "allowed",
        "turn-1",
        turn_record("allowed"),
    )
    handle.events_seen = [
        {
            "method": "turn/queued",
            "params": {"turn": turn_record("allowed", status="queued")},
        },
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "id": "item-1",
                    "type": "assistantMessage",
                    "data": {"content": "done"},
                }
            },
        },
        {
            "method": "turn/completed",
            "params": {"turn": turn_record("allowed")},
        },
    ]
    wrapped = adapter.wrap_turn_handle(handle)

    events = [event async for event in wrapped.events()]
    result = await wrapped.result()

    assert [event.type for event in events] == ["queued", "item", "completed"]
    assert [event.sequence for event in events] == [1, 2, 3]
    assert result.status == "completed"
