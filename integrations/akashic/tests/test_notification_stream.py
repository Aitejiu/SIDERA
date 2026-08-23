import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest

from sidera_task_bridge.client import (
    ControlPlaneClient,
    ProjectionResyncRequired,
    ProjectionStreamDisconnected,
)
from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.contracts import ProjectionChange

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")
RESOURCE_ID = UUID("00000000-0000-0000-0000-000000000010")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000011")


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


def config() -> TaskBridgeConfig:
    return TaskBridgeConfig.model_validate(
        {
            "workspace_id": WORKSPACE_ID,
            "planning_profile_id": PROFILE_ID,
            "planning_profile_version": 1,
        }
    )


def change(cursor: int, *, workspace_id: UUID = WORKSPACE_ID, schema_version: int = 1) -> dict[str, Any]:
    return {
        "workspace_id": str(workspace_id),
        "cursor": cursor,
        "schema_version": schema_version,
        "projection_type": "planning_request",
        "resource_id": str(RESOURCE_ID),
        "change_type": "proposal_ready",
        "resource_version": cursor,
        "occurred_at": "2026-08-24T03:30:00Z",
        "correlation_id": str(CORRELATION_ID),
    }


def event(cursor: int, *, data: dict[str, Any] | None = None, newline: str = "\n") -> str:
    payload = data or change(cursor)
    encoded = json.dumps(payload, separators=(",", ":"))
    return f"id: {cursor}{newline}event: projection.changed{newline}data: {encoded}{newline}{newline}"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_projection_stream_parses_split_events_heartbeats_and_multiple_data_lines() -> None:
    async def scenario() -> None:
        body = (
            b": heartbeat\r\n\r\n"
            b"id: 1\r\n"
            b"event: projection.changed\r\n"
            b'data: {"workspace_id":"00000000-0000-0000-0000-000000000001",\r\n'
            b'data: "cursor":1,"schema_version":1,"projection_type":"planning_request",\r\n'
            b'data: "resource_id":"00000000-0000-0000-0000-000000000010",\r\n'
            b'data: "change_type":"proposal_ready","resource_version":1,\r\n'
            b'data: "occurred_at":"2026-08-24T03:30:00Z",\r\n'
            b'data: "correlation_id":"00000000-0000-0000-0000-000000000011"}\r\n\r\n'
        )
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["accept"] = request.headers.get("accept")
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ChunkStream([body[index : index + 3] for index in range(0, len(body), 3)]),
            )

        client = ControlPlaneClient(config(), transport=httpx.MockTransport(handler))
        received: list[ProjectionChange] = []
        with pytest.raises(ProjectionStreamDisconnected):
            async for item in client.iter_projection_changes(after=0):
                received.append(item)
        await client.close()

        assert received == [ProjectionChange.model_validate(change(1))]
        assert seen["accept"] == "text/event-stream"
        assert seen["url"] == (
            f"http://127.0.0.1:8000/api/v1/workspaces/{WORKSPACE_ID}/projection-changes"
            "?after=0&schema_version=1"
        )

    run(scenario())


def test_projection_stream_ignores_duplicate_cursor_and_rejects_gap() -> None:
    async def scenario() -> None:
        body = (event(1) + event(1) + event(3)).encode()

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=ChunkStream([body]))

        client = ControlPlaneClient(config(), transport=httpx.MockTransport(handler))
        received: list[ProjectionChange] = []
        with pytest.raises(ProjectionResyncRequired) as error:
            async for item in client.iter_projection_changes(after=0):
                received.append(item)
        await client.close()

        assert [item.cursor for item in received] == [1]
        assert error.value.reason == "cursor_gap"
        assert error.value.last_confirmed_cursor == 1

    run(scenario())


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("event: projection.changed\ndata: not-json\n\n", "invalid_payload"),
        (event(1, data=change(1, schema_version=2)), "unsupported_schema"),
        (event(1, data=change(1, workspace_id=UUID("00000000-0000-0000-0000-000000000099"))), "workspace_mismatch"),
        ("event: resync_required\ndata: {\"reason\":\"cursor_expired\"}\n\n", "cursor_expired"),
    ],
)
def test_projection_stream_converts_invalid_events_to_stable_resync(
    body: str, reason: str
) -> None:
    async def scenario() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=ChunkStream([body.encode()]))

        client = ControlPlaneClient(config(), transport=httpx.MockTransport(handler))
        with pytest.raises(ProjectionResyncRequired) as error:
            async for _ in client.iter_projection_changes(after=0):
                pass
        await client.close()
        assert error.value.reason == reason
        assert error.value.last_confirmed_cursor == 0

    run(scenario())


def test_projection_stream_rejects_oversized_event_and_unterminated_eof() -> None:
    async def scenario() -> None:
        oversized = (
            "event: projection.changed\n"
            f"data: {json.dumps({**change(1), 'change_type': 'x' * 262_200})}\n\n"
        ).encode()
        incomplete = event(1).rstrip().encode()

        def oversized_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=ChunkStream([oversized]))

        client = ControlPlaneClient(
            config(), transport=httpx.MockTransport(oversized_handler)
        )
        with pytest.raises(ProjectionResyncRequired) as error:
            async for _ in client.iter_projection_changes(after=0):
                pass
        await client.close()
        assert error.value.reason == "event_too_large"

        def incomplete_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=ChunkStream([incomplete]))

        client = ControlPlaneClient(
            config(), transport=httpx.MockTransport(incomplete_handler)
        )
        with pytest.raises(ProjectionStreamDisconnected):
            async for _ in client.iter_projection_changes(after=0):
                pass
        await client.close()

    run(scenario())


def test_projection_stream_maps_transport_failure_to_disconnected() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("secret transport detail", request=request)

        client = ControlPlaneClient(config(), transport=httpx.MockTransport(handler))
        with pytest.raises(ProjectionStreamDisconnected) as error:
            async for _ in client.iter_projection_changes(after=5):
                pass
        await client.close()
        assert error.value.last_confirmed_cursor == 5
        assert "secret transport detail" not in str(error.value)

    run(scenario())


def test_projection_change_requires_fixed_schema_and_types() -> None:
    with pytest.raises(ValueError):
        ProjectionChange.model_validate(
            {
                **change(1),
                "schema_version": 2,
            }
        )
