from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from sidera_main_agent_gateway.api.app import create_app
from sidera_main_agent_gateway.config import Settings
from sidera_main_agent_gateway.contracts import (
    JsonValue,
    MainAgentEvent,
    MainAgentThread,
    MainAgentThreadPage,
    MainAgentTurn,
    MainAgentTurnHandle,
)
from sidera_main_agent_gateway.errors import (
    ProviderUnavailable,
    ThreadAccessDenied,
    ThreadBusy,
)
from sidera_main_agent_gateway.relay import TurnRelayRegistry

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def thread() -> MainAgentThread:
    return MainAgentThread(
        id="thread-1",
        title="Gateway thread",
        created_at=NOW,
        updated_at=NOW,
        context_refs=("task:1",),
    )


def detailed_thread() -> MainAgentThread:
    base = thread()
    return MainAgentThread(
        id=base.id,
        title=base.title,
        created_at=base.created_at,
        updated_at=base.updated_at,
        context_refs=base.context_refs,
        turns=(turn(),),
    )


def turn(status: str = "completed") -> MainAgentTurn:
    return MainAgentTurn(
        id="turn-1",
        thread_id="thread-1",
        status=status,  # type: ignore[arg-type]
        created_at=NOW,
        started_at=NOW,
        completed_at=NOW if status == "completed" else None,
        final_response="done" if status == "completed" else None,
        items=(),
        usage={"inputTokens": 1},
        error=None,
    )


class FakeHandle(MainAgentTurnHandle):
    thread_id = "thread-1"
    id = "turn-1"

    def __init__(self) -> None:
        self._events = (
            MainAgentEvent(1, "started", {"status": "in_progress"}),
            MainAgentEvent(2, "completed", {"status": "completed"}),
        )

    def events(self) -> AsyncIterator[MainAgentEvent]:
        async def stream() -> AsyncIterator[MainAgentEvent]:
            for event in self._events:
                yield event

        return stream()

    async def result(self) -> MainAgentTurn:
        return turn()

    async def interrupt(self) -> MainAgentTurn:
        return turn("interrupted")


class FakeAdapter:
    def __init__(self) -> None:
        self.start_error: BaseException | None = None
        self.read_error: BaseException | None = None
        self.health_error: BaseException | None = None
        self.closed = False
        self.created: list[tuple[str, tuple[str, ...]]] = []
        self.thread_detail = thread()

    async def health(self) -> Mapping[str, JsonValue]:
        if self.health_error is not None:
            raise self.health_error
        return {"status": "ok"}

    async def capabilities(self) -> Mapping[str, JsonValue]:
        return {"thread_rename": False}

    async def list_threads(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> MainAgentThreadPage:
        return MainAgentThreadPage((thread(),), None)

    async def create_thread(
        self,
        *,
        title: str,
        context_refs: Sequence[str],
    ) -> MainAgentThread:
        self.created.append((title, tuple(context_refs)))
        return thread()

    async def read_thread(self, thread_id: str) -> MainAgentThread:
        if self.read_error is not None:
            raise self.read_error
        return self.thread_detail

    async def start_turn(
        self,
        thread_id: str,
        input_text: str,
    ) -> MainAgentTurnHandle:
        if self.start_error is not None:
            raise self.start_error
        return FakeHandle()

    async def read_turn(self, thread_id: str, turn_id: str) -> MainAgentTurn:
        return turn()

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> MainAgentTurn:
        return turn("interrupted")

    async def close(self) -> None:
        self.closed = True


def build_client(
    adapter: FakeAdapter | None = None,
) -> tuple[TestClient, FakeAdapter]:
    resolved_adapter = adapter or FakeAdapter()
    app = create_app(
        Settings(
            akashic_endpoint="/tmp/akashic/control.sock",
            sidera_workspace_id=WORKSPACE_ID,
        ),
        adapter=resolved_adapter,
        relay_registry=TurnRelayRegistry(event_limit=32),
    )
    return TestClient(app), resolved_adapter


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FakeAdapter]]:
    test_client, adapter = build_client()
    with test_client:
        yield test_client, adapter


def test_health_and_capabilities_are_provider_neutral(
    client: tuple[TestClient, FakeAdapter],
) -> None:
    test_client, _ = client

    health = test_client.get("/main-agent/v1/health")
    capabilities = test_client.get("/main-agent/v1/capabilities")

    assert health.status_code == 200
    assert health.json()["provider"] == "available"
    assert capabilities.json()["thread_rename"] is False
    assert capabilities.json()["stream_resume_after_gateway_restart"] is False


def test_health_stays_200_when_provider_is_unavailable() -> None:
    adapter = FakeAdapter()
    adapter.health_error = ProviderUnavailable("offline")
    test_client, _ = build_client(adapter)
    with test_client:
        response = test_client.get("/main-agent/v1/health")

    assert response.status_code == 200
    assert response.json()["provider"] == "unavailable"


def test_cors_allows_only_configured_origin(
    client: tuple[TestClient, FakeAdapter],
) -> None:
    test_client, _ = client

    allowed = test_client.options(
        "/main-agent/v1/health",
        headers={
            "Origin": "http://127.0.0.1:4174",
            "Access-Control-Request-Method": "GET",
        },
    )
    denied = test_client.options(
        "/main-agent/v1/health",
        headers={
            "Origin": "http://127.0.0.1:9999",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:4174"
    assert "access-control-allow-origin" not in denied.headers


def test_events_without_relay_return_pollable_conflict(
    client: tuple[TestClient, FakeAdapter],
) -> None:
    test_client, _ = client

    response = test_client.get(
        "/main-agent/v1/threads/thread-1/turns/turn-1/events"
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "turn_stream_unavailable"
    assert response.json()["error"]["retryable"] is False


def test_thread_create_and_turn_start_use_expected_status_codes(
    client: tuple[TestClient, FakeAdapter],
) -> None:
    test_client, adapter = client

    created = test_client.post(
        "/main-agent/v1/threads",
        json={"title": "Web chat", "context_refs": ["task:1"]},
    )
    started = test_client.post(
        "/main-agent/v1/threads/thread-1/turns",
        json={"input": "hello"},
    )

    assert created.status_code == 201
    assert started.status_code == 202
    assert adapter.created == [("Web chat", ("task:1",))]
    assert started.json()["id"] == "turn-1"


def test_thread_create_accepts_public_camelcase_context_refs(
    client: tuple[TestClient, FakeAdapter],
) -> None:
    test_client, adapter = client

    created = test_client.post(
        "/main-agent/v1/threads",
        json={"title": "Web chat", "contextRefs": ["task:1"]},
    )

    assert created.status_code == 201
    assert adapter.created == [("Web chat", ("task:1",))]


def test_thread_read_returns_persisted_turns(
    client: tuple[TestClient, FakeAdapter],
) -> None:
    test_client, adapter = client
    adapter.thread_detail = detailed_thread()

    response = test_client.get("/main-agent/v1/threads/thread-1")

    assert response.status_code == 200
    assert response.json()["turns"][0]["id"] == "turn-1"
    assert response.json()["turns"][0]["streamAvailable"] is False


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ThreadBusy("busy"), 409, "thread_busy"),
        (ThreadAccessDenied("hidden"), 404, "thread_not_found"),
        (ProviderUnavailable("offline"), 503, "provider_unavailable"),
    ],
)
def test_gateway_errors_are_uniform_and_do_not_leak_details(
    error: BaseException,
    status: int,
    code: str,
) -> None:
    adapter = FakeAdapter()
    if isinstance(error, ThreadBusy):
        adapter.start_error = error
        path = "/main-agent/v1/threads/thread-1/turns"
        payload: dict[str, object] = {"input": "hello"}
    elif isinstance(error, ThreadAccessDenied):
        adapter.read_error = error
        path = "/main-agent/v1/threads/thread-1"
        payload = {}
    else:
        adapter.start_error = error
        path = "/main-agent/v1/threads/thread-1/turns"
        payload = {"input": "hello"}

    test_client, _ = build_client(adapter)
    with test_client:
        response = (
            test_client.post(path, json=payload)
            if payload
            else test_client.get(path)
        )

    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert "offline" not in response.text
    assert "hidden" not in response.text


def test_invalid_body_uses_uniform_422_envelope(
    client: tuple[TestClient, FakeAdapter],
) -> None:
    test_client, _ = client

    response = test_client.post(
        "/main-agent/v1/threads/thread-1/turns",
        json={"input": ""},
    )

    assert response.status_code == 422
    assert set(response.json()) == {"error"}
    assert response.json()["error"]["code"] == "invalid_request"
    assert "detail" not in response.text


def test_events_use_sse_sequences_and_last_event_id(
    client: tuple[TestClient, FakeAdapter],
) -> None:
    test_client, _ = client
    assert test_client.post(
        "/main-agent/v1/threads/thread-1/turns",
        json={"input": "hello"},
    ).status_code == 202

    with test_client.stream(
        "GET",
        "/main-agent/v1/threads/thread-1/turns/turn-1/events",
        headers={"Last-Event-ID": "1"},
    ) as response:
        text = response.read().decode()

    assert response.status_code == 200
    assert "id: 2" in text
    assert "event: completed" in text
    assert "data: {" in text


def test_expired_event_cursor_emits_resync_required() -> None:
    app = create_app(
        Settings(
            akashic_endpoint="/tmp/akashic/control.sock",
            sidera_workspace_id=WORKSPACE_ID,
        ),
        adapter=FakeAdapter(),
        relay_registry=TurnRelayRegistry(event_limit=1),
    )
    with TestClient(app) as test_client:
        assert test_client.post(
            "/main-agent/v1/threads/thread-1/turns",
            json={"input": "hello"},
        ).status_code == 202
        with test_client.stream(
            "GET",
            "/main-agent/v1/threads/thread-1/turns/turn-1/events",
        ) as response:
            text = response.read().decode()

    assert response.status_code == 200
    assert "event: resync_required" in text


def test_openapi_is_neutral_and_contains_all_gateway_paths() -> None:
    app = create_app(
        Settings(
            akashic_endpoint="/tmp/akashic/control.sock",
            sidera_workspace_id=WORKSPACE_ID,
        ),
        adapter=FakeAdapter(),
        relay_registry=TurnRelayRegistry(event_limit=32),
    )
    document = app.openapi()
    paths = set(document["paths"])
    expected = {
        "/main-agent/v1/health",
        "/main-agent/v1/capabilities",
        "/main-agent/v1/threads",
        "/main-agent/v1/threads/{thread_id}",
        "/main-agent/v1/threads/{thread_id}/turns",
        "/main-agent/v1/threads/{thread_id}/turns/{turn_id}",
        "/main-agent/v1/threads/{thread_id}/turns/{turn_id}/events",
        "/main-agent/v1/threads/{thread_id}/turns/{turn_id}/interrupt",
    }

    assert expected <= paths
    serialized = str(document)
    assert "Akashic" not in serialized
    assert "akashic" not in serialized
    assert "token" not in serialized.lower()
