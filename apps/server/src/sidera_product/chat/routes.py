from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

import anyio
from fastapi import APIRouter, Header, Query, Request, Response
from sidera_agents.errors import AgentError
from sidera_control_plane.composition.native_runtime import ControlPlaneIdentity
from starlette.responses import StreamingResponse

from .contracts import (
    ChatCapabilities,
    ChatHealth,
    CreateThreadBody,
    StartTurnBody,
    ThreadListView,
    ThreadStopView,
    ThreadView,
    TurnView,
)
from .projection import ProjectionError
from .service import ChatError

if TYPE_CHECKING:
    from sidera_product.lifecycle import ProductRuntime


def chat_router(owner: ProductRuntime) -> APIRouter:
    router = APIRouter(prefix="/main-agent/v1")

    async def call[T](operation: Callable[[], T]) -> T:
        try:
            return await anyio.to_thread.run_sync(operation)
        except AgentError as error:
            raise ChatError(
                error.code,
                status_code=403
                if error.code == "permission_denied"
                else 503
                if error.code == "product_unavailable"
                else 409,
                retryable=error.code in {"product_unavailable", "resource_busy"},
            ) from None

    def authenticate(request: Request) -> ControlPlaneIdentity:
        identity = owner.local_sessions.authenticate_request(request)
        if identity is None or identity.actor_type != "user":
            raise ChatError("local_session_required", status_code=401)
        return identity

    @router.get("/health", response_model=ChatHealth)
    async def health(request: Request) -> ChatHealth:
        await call(lambda: authenticate(request))
        execution = owner.status()["execution"]
        ready = isinstance(execution, dict) and execution.get("ready") is True
        return ChatHealth(provider="available" if ready else "unavailable")

    @router.get("/capabilities", response_model=ChatCapabilities)
    async def capabilities(request: Request) -> ChatCapabilities:
        await call(lambda: authenticate(request))
        return ChatCapabilities()

    @router.get("/threads", response_model=ThreadListView)
    async def list_threads(
        request: Request,
        cursor: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> ThreadListView:
        identity = await call(lambda: authenticate(request))
        conversations = await call(lambda: owner.chat.list_threads(identity))
        offset = 0
        if cursor is not None:
            matching = next(
                (
                    index
                    for index, item in enumerate(conversations)
                    if item.id == cursor
                ),
                None,
            )
            if matching is None:
                raise ChatError("invalid_cursor", status_code=422)
            offset = matching + 1
        selected = conversations[offset : offset + limit]
        views = tuple(
            [
                await call(
                    lambda item=item: owner.chat.read_thread_view(identity, item.id)
                )
                for item in selected
            ]
        )
        next_cursor = (
            str(selected[-1].id)
            if selected and offset + limit < len(conversations)
            else None
        )
        return ThreadListView(data=views, nextCursor=next_cursor)

    @router.post("/threads", response_model=ThreadView, status_code=201)
    async def create_thread(body: CreateThreadBody, request: Request) -> ThreadView:
        identity = await call(lambda: authenticate(request))
        await call(owner.require_write_admission)
        conversation = await call(lambda: owner.chat.create_thread(identity, body))
        return await call(
            lambda: owner.chat.read_thread_view(identity, conversation.id)
        )

    @router.get("/threads/{thread_id}", response_model=ThreadView)
    async def read_thread(thread_id: UUID, request: Request) -> ThreadView:
        identity = await call(lambda: authenticate(request))
        return await call(lambda: owner.chat.read_thread_view(identity, thread_id))

    @router.post(
        "/threads/{thread_id}/stop",
        response_model=ThreadStopView,
        responses={202: {"model": ThreadStopView}},
    )
    async def stop_thread(
        thread_id: UUID, request: Request, response: Response
    ) -> ThreadStopView:
        identity = await call(lambda: authenticate(request))
        result = await call(lambda: owner.chat.stop_thread(identity, thread_id))
        if not result.cleanup_confirmed:
            response.status_code = 202
        return result

    @router.post("/threads/{thread_id}/turns", response_model=TurnView, status_code=202)
    async def start_turn(
        thread_id: UUID, body: StartTurnBody, request: Request
    ) -> TurnView:
        identity = await call(lambda: authenticate(request))
        await call(owner.require_write_admission)
        turn = await call(lambda: owner.chat.start_turn(identity, thread_id, body))
        return await call(lambda: owner.chat.read_turn(identity, thread_id, turn.id))

    @router.get("/threads/{thread_id}/turns/{turn_id}", response_model=TurnView)
    async def read_turn(thread_id: UUID, turn_id: UUID, request: Request) -> TurnView:
        identity = await call(lambda: authenticate(request))
        return await call(lambda: owner.chat.read_turn(identity, thread_id, turn_id))

    @router.post(
        "/threads/{thread_id}/turns/{turn_id}/interrupt", response_model=TurnView
    )
    async def interrupt_turn(
        thread_id: UUID, turn_id: UUID, request: Request
    ) -> TurnView:
        identity = await call(lambda: authenticate(request))
        await call(owner.require_write_admission)
        return await call(
            lambda: owner.chat.interrupt_turn(identity, thread_id, turn_id)
        )

    @router.get("/threads/{thread_id}/turns/{turn_id}/events", response_model=None)
    async def turn_events(
        thread_id: UUID,
        turn_id: UUID,
        request: Request,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        identity = await call(lambda: authenticate(request))

        async def page(cursor: int, header: str | None = None):
            return await call(
                lambda: owner.chat.read_events(
                    identity, thread_id, turn_id, after=cursor, last_event_id=header
                )
            )

        try:
            initial = await page(after, last_event_id)
        except ProjectionError:
            initial = None

        async def stream() -> AsyncGenerator[str]:
            current = initial
            if current is None:
                yield 'event: resync_required\ndata: {"reason":"cursor_expired"}\n\n'
                return
            while True:
                frames, cursor, terminal = current
                for frame in frames:
                    yield f"id: {frame.sequence}\nevent: {frame.type}\ndata: {json.dumps(frame.data, separators=(',', ':'))}\n\n"
                if terminal or await request.is_disconnected():
                    return
                await anyio.sleep(0.2)
                try:
                    current = await page(cursor)
                except (AgentError, ChatError, ProjectionError) as error:
                    yield (
                        "event: resync_required\ndata: "
                        + json.dumps({"reason": error.code}, separators=(",", ":"))
                        + "\n\n"
                    )
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
