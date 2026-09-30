from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, Mapping
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from sidera_main_agent_gateway.api.schemas import (
    CapabilitiesResponse,
    CreateThreadRequest,
    HealthResponse,
    StartTurnRequest,
    ThreadListResponse,
    ThreadResponse,
    TurnItemResponse,
    TurnResponse,
)
from sidera_main_agent_gateway.contracts import (
    JsonValue,
    MainAgentAdapter,
    MainAgentThread,
    MainAgentThreadPage,
    MainAgentTurn,
)
from sidera_main_agent_gateway.relay import (
    RelayCursorExpired,
    TurnRelay,
    TurnRelayRegistry,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/main-agent/v1")


def get_adapter(request: Request) -> MainAgentAdapter:
    return cast(MainAgentAdapter, request.app.state.main_agent_adapter)


def get_relays(request: Request) -> TurnRelayRegistry:
    return cast(TurnRelayRegistry, request.app.state.turn_relays)


def _thread_response(
    thread: MainAgentThread,
    relays: TurnRelayRegistry,
) -> ThreadResponse:
    return ThreadResponse(
        id=thread.id,
        title=thread.title,
        createdAt=thread.created_at,
        updatedAt=thread.updated_at,
        contextRefs=thread.context_refs,
        turns=tuple(_turn_response(turn, relays) for turn in thread.turns),
    )


def _turn_response(
    turn: MainAgentTurn,
    relays: TurnRelayRegistry,
) -> TurnResponse:
    availability = relays.availability(turn.thread_id, turn.id, turn.status)
    return TurnResponse(
        id=turn.id,
        threadId=turn.thread_id,
        status=turn.status,
        createdAt=turn.created_at,
        startedAt=turn.started_at,
        completedAt=turn.completed_at,
        finalResponse=turn.final_response,
        items=tuple(
            TurnItemResponse(id=item.id, type=item.type, data=dict(item.data))
            for item in turn.items
        ),
        usage=None if turn.usage is None else dict(turn.usage),
        error=None if turn.error is None else dict(turn.error),
        streamAvailable=availability.stream_available,
        recoveryMode=availability.recovery_mode,
    )


@router.get("/health", response_model=HealthResponse)
async def health(
    request: Request,
    adapter: Annotated[MainAgentAdapter, Depends(get_adapter)],
) -> HealthResponse:
    if request.app.state.settings.restore_read_only:
        return HealthResponse(provider="unavailable")
    try:
        await adapter.health()
    except Exception as exc:
        logger.info("main agent provider unavailable", exc_info=exc)
        return HealthResponse(provider="unavailable")
    return HealthResponse(provider="available")


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse()


@router.get("/threads", response_model=ThreadListResponse)
async def list_threads(
    adapter: Annotated[MainAgentAdapter, Depends(get_adapter)],
    relays: Annotated[TurnRelayRegistry, Depends(get_relays)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ThreadListResponse:
    page: MainAgentThreadPage = await adapter.list_threads(cursor=cursor, limit=limit)
    return ThreadListResponse(
        data=tuple(_thread_response(thread, relays) for thread in page.threads),
        nextCursor=page.next_cursor,
    )


@router.post("/threads", response_model=ThreadResponse, status_code=201)
async def create_thread(
    payload: CreateThreadRequest,
    adapter: Annotated[MainAgentAdapter, Depends(get_adapter)],
    relays: Annotated[TurnRelayRegistry, Depends(get_relays)],
) -> ThreadResponse:
    thread = await adapter.create_thread(
        title=payload.title or "",
        context_refs=payload.context_refs,
    )
    return _thread_response(thread, relays)


@router.get("/threads/{thread_id}", response_model=ThreadResponse)
async def read_thread(
    thread_id: str,
    adapter: Annotated[MainAgentAdapter, Depends(get_adapter)],
    relays: Annotated[TurnRelayRegistry, Depends(get_relays)],
) -> ThreadResponse:
    return _thread_response(await adapter.read_thread(thread_id), relays)


@router.post("/threads/{thread_id}/turns", response_model=TurnResponse, status_code=202)
async def start_turn(
    thread_id: str,
    payload: StartTurnRequest,
    adapter: Annotated[MainAgentAdapter, Depends(get_adapter)],
    relays: Annotated[TurnRelayRegistry, Depends(get_relays)],
) -> TurnResponse:
    if payload.operation_id is None:
        handle = await adapter.start_turn(thread_id, payload.input)
    else:
        handle = await adapter.start_turn(
            thread_id,
            payload.input,
            operation_id=payload.operation_id,
        )
    await relays.start(handle)
    turn = await adapter.read_turn(thread_id, handle.id)
    return _turn_response(turn, relays)


@router.get(
    "/threads/{thread_id}/turns/{turn_id}",
    response_model=TurnResponse,
)
async def read_turn(
    thread_id: str,
    turn_id: str,
    adapter: Annotated[MainAgentAdapter, Depends(get_adapter)],
    relays: Annotated[TurnRelayRegistry, Depends(get_relays)],
) -> TurnResponse:
    turn = await adapter.read_turn(thread_id, turn_id)
    return _turn_response(turn, relays)


def _sse_event(event_id: int, event_name: str, data: Mapping[str, JsonValue]) -> str:
    return (
        f"id: {event_id}\n"
        f"event: {event_name}\n"
        f"data: {json.dumps(dict(data), ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _sse_resync(reason: str) -> str:
    return (
        "event: resync_required\n"
        f"data: {json.dumps({'reason': reason}, separators=(',', ':'))}\n\n"
    )


async def _event_stream(relay: TurnRelay, start_cursor: int) -> AsyncGenerator[str, None]:
    try:
        async for event in relay.events(after=start_cursor):
            yield _sse_event(event.sequence, event.event, event.data)
    except RelayCursorExpired:
        yield _sse_resync("cursor_expired")


@router.get("/threads/{thread_id}/turns/{turn_id}/events", response_model=None)
async def turn_events(
    thread_id: str,
    turn_id: str,
    adapter: Annotated[MainAgentAdapter, Depends(get_adapter)],
    relays: Annotated[TurnRelayRegistry, Depends(get_relays)],
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID", ge=0)] = None,
) -> StreamingResponse | JSONResponse:
    start_cursor = max(after, last_event_id or 0)
    relay = relays.get(thread_id, turn_id)
    if relay is None:
        turn = await adapter.read_turn(thread_id, turn_id)
        if turn.status in {"queued", "in_progress"}:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "turn_stream_unavailable",
                        "message": "turn stream is unavailable; poll the turn",
                        "retryable": True,
                    }
                },
            )
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "turn_stream_unavailable",
                    "message": "turn stream is no longer available",
                    "retryable": False,
                }
            },
        )
    return StreamingResponse(
        _event_stream(relay, start_cursor),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/threads/{thread_id}/turns/{turn_id}/interrupt",
    response_model=TurnResponse,
)
async def interrupt_turn(
    thread_id: str,
    turn_id: str,
    adapter: Annotated[MainAgentAdapter, Depends(get_adapter)],
    relays: Annotated[TurnRelayRegistry, Depends(get_relays)],
) -> TurnResponse:
    turn = await adapter.interrupt_turn(thread_id, turn_id)
    await relays.shutdown_turn(thread_id, turn_id)
    return _turn_response(turn, relays)
