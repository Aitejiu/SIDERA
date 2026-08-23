from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from akashic_sdk import AsyncAkashic, ConnectionClosedError, RemoteError, TurnHandle

from sidera_main_agent_gateway.config import Settings
from sidera_main_agent_gateway.contracts import (
    EventType,
    JsonValue,
    MainAgentAdapter,
    MainAgentEvent,
    MainAgentItem,
    MainAgentThread,
    MainAgentThreadPage,
    MainAgentTurn,
    MainAgentTurnHandle,
    TurnStatus,
)
from sidera_main_agent_gateway.errors import (
    ProviderProtocolError,
    ProviderUnavailable,
    ThreadAccessDenied,
    ThreadBusy,
    ThreadNotFound,
    TurnNotFound,
    TurnStreamUnavailable,
)

ClientFactory = Callable[..., Awaitable[Any]]
_CONNECTION_ERRORS = (ConnectionClosedError, ConnectionError, OSError, TimeoutError)
_THREAD_NOT_FOUND_CODE = -32010
_THREAD_BUSY_CODE = -32011
_TURN_NOT_FOUND_CODE = -32012
_UNAUTHORIZED_CODE = -32004
logger = logging.getLogger(__name__)


def _as_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ProviderProtocolError(f"{field_name} must be a JSON object")
    return cast(Mapping[str, Any], value)


def _as_json_mapping(value: object, field_name: str) -> Mapping[str, JsonValue]:
    return cast(Mapping[str, JsonValue], _as_mapping(value, field_name))


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ProviderProtocolError(f"{field_name} must be an RFC3339 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProviderProtocolError(f"{field_name} is not valid RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderProtocolError(f"{field_name} must include a timezone")
    return parsed


def _parse_optional_datetime(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value, field_name)


def _normalize_context_refs(context_refs: Sequence[str]) -> tuple[str, ...]:
    unique: list[str] = []
    seen: set[str] = set()
    for context_ref in context_refs:
        if not isinstance(context_ref, str) or not context_ref.strip():
            raise ValueError("context ref must be a non-empty string")
        if len(context_ref) > 200:
            raise ValueError("context ref must be at most 200 characters")
        if context_ref not in seen:
            seen.add(context_ref)
            unique.append(context_ref)
    if len(unique) > 20:
        raise ValueError("context refs must contain at most 20 items")
    return tuple(unique)


class AkashicAdapter(MainAgentAdapter):
    """Translate the Akashic SDK into the provider-neutral Gateway contract."""

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: ClientFactory = AsyncAkashic.connect,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory
        self._client: Any | None = None
        self._client_lock = asyncio.Lock()

    async def _connect(self) -> Any:
        token = (
            self._settings.akashic_workspace_token.get_secret_value()
            if self._settings.akashic_workspace_token is not None
            else None
        )
        try:
            return await self._client_factory(
                self._settings.akashic_endpoint,
                workspace_token=token,
                max_message_bytes=self._settings.max_message_bytes,
            )
        except _CONNECTION_ERRORS as exc:
            raise ProviderUnavailable("Akashic provider is unavailable") from exc

    async def _get_client(self) -> Any:
        async with self._client_lock:
            if self._client is None:
                self._client = await self._connect()
            return self._client

    async def _invalidate(self, client: Any) -> None:
        async with self._client_lock:
            if self._client is not client:
                return
            self._client = None
        try:
            await client.close()
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.debug("provider client close failed", exc_info=exc)

    @staticmethod
    def _map_remote_error(error: RemoteError) -> Exception:
        if error.code == _THREAD_NOT_FOUND_CODE:
            return ThreadNotFound("thread was not found")
        if error.code == _THREAD_BUSY_CODE:
            return ThreadBusy("thread already has an active turn")
        if error.code == _TURN_NOT_FOUND_CODE:
            return TurnNotFound("turn was not found")
        if error.code == _UNAUTHORIZED_CODE:
            return ProviderUnavailable("Akashic provider authorization failed")
        return ProviderProtocolError(
            f"provider request failed (code={error.code}, retryable={error.retryable})"
        )

    async def _read_with_reconnect(
        self,
        operation: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        client = await self._get_client()
        try:
            return await operation(client)
        except RemoteError as exc:
            raise self._map_remote_error(exc) from exc
        except _CONNECTION_ERRORS:
            await self._invalidate(client)
            retry_client = await self._get_client()
            try:
                return await operation(retry_client)
            except RemoteError as retry_exc:
                raise self._map_remote_error(retry_exc) from retry_exc
            except _CONNECTION_ERRORS as retry_exc:
                await self._invalidate(retry_client)
                raise ProviderUnavailable("Akashic provider is unavailable") from retry_exc

    async def _write_without_retry(
        self,
        operation: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        client = await self._get_client()
        try:
            return await operation(client)
        except RemoteError as exc:
            raise self._map_remote_error(exc) from exc
        except _CONNECTION_ERRORS as exc:
            await self._invalidate(client)
            raise ProviderUnavailable("Akashic provider is unavailable") from exc

    def _assert_scope(self, raw_thread: object) -> Mapping[str, Any]:
        record = _as_mapping(raw_thread, "thread")
        metadata = _as_mapping(record.get("metadata"), "thread metadata")
        if metadata.get("source") != "sidera_web":
            raise ThreadAccessDenied("thread is outside the web source scope")
        if metadata.get("sidera_workspace_id") != str(
            self._settings.sidera_workspace_id
        ):
            raise ThreadAccessDenied("thread is outside the configured workspace")
        return record

    async def _map_thread(self, raw_thread: object) -> MainAgentThread:
        record = self._assert_scope(raw_thread)
        metadata = _as_mapping(record.get("metadata"), "thread metadata")
        thread_id = record.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ProviderProtocolError("thread id must be a non-empty string")
        created_at = _parse_datetime(record.get("createdAt"), "thread createdAt")
        updated_at = _parse_datetime(record.get("updatedAt"), "thread updatedAt")
        raw_title = metadata.get("title")
        if raw_title is not None and not isinstance(raw_title, str):
            raise ProviderProtocolError("thread metadata title must be a string")
        title = (
            raw_title.strip()
            if isinstance(raw_title, str) and raw_title.strip()
            else f"对话 {created_at.astimezone().date().isoformat()}"
        )
        raw_refs = metadata.get("context_refs", [])
        if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, str):
            raise ProviderProtocolError("thread context_refs must be a string array")
        try:
            context_refs = _normalize_context_refs(raw_refs)
        except (TypeError, ValueError) as exc:
            raise ProviderProtocolError(str(exc)) from exc
        return MainAgentThread(
            id=thread_id,
            title=title,
            created_at=created_at,
            updated_at=updated_at,
            context_refs=context_refs,
        )

    async def health(self) -> Mapping[str, JsonValue]:
        async def status(client: Any) -> object:
            wire = getattr(client, "_wire", None)
            request = getattr(wire, "request", None)
            if request is None:
                return {"status": "configured"}
            return await request("server/status", {})

        raw = await self._read_with_reconnect(status)
        return _as_json_mapping(raw, "provider status")

    async def capabilities(self) -> Mapping[str, JsonValue]:
        return {
            "thread_rename": False,
            "stream_resume_after_gateway_restart": False,
            "turn_interrupt": True,
        }

    async def list_threads(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> MainAgentThreadPage:
        raw_page = await self._read_with_reconnect(
            lambda client: client.thread_list(cursor=cursor, limit=limit)
        )
        page = _as_mapping(raw_page, "thread page")
        raw_threads = page.get("data")
        if not isinstance(raw_threads, list):
            raise ProviderProtocolError("thread page data must be an array")
        threads: list[MainAgentThread] = []
        for raw_thread in raw_threads:
            try:
                threads.append(await self._map_thread(raw_thread))
            except ThreadAccessDenied:
                continue
        next_cursor = page.get("nextCursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ProviderProtocolError("thread page nextCursor must be a string")
        return MainAgentThreadPage(tuple(threads), next_cursor)

    async def create_thread(
        self,
        *,
        title: str,
        context_refs: Sequence[str],
    ) -> MainAgentThread:
        normalized_refs = _normalize_context_refs(context_refs)
        metadata = {
            "source": "sidera_web",
            "sidera_workspace_id": str(self._settings.sidera_workspace_id),
            "title": title,
            "context_refs": list(normalized_refs),
        }
        thread = await self._write_without_retry(
            lambda client: client.thread_start(metadata=metadata)
        )
        record = getattr(thread, "record", thread)
        return await self._map_thread(record)

    async def read_thread(self, thread_id: str) -> MainAgentThread:
        raw_thread = await self._read_with_reconnect(
            lambda client: client.thread_read(thread_id, include_turns=False)
        )
        return await self._map_thread(raw_thread)

    async def start_turn(
        self,
        thread_id: str,
        input_text: str,
    ) -> MainAgentTurnHandle:
        await self.read_thread(thread_id)
        thread = await self._read_with_reconnect(
            lambda client: client.thread_resume(thread_id)
        )
        try:
            provider_handle = await thread.turn(input_text)
        except RemoteError as exc:
            raise self._map_remote_error(exc) from exc
        except _CONNECTION_ERRORS as exc:
            client = self._client
            if client is not None:
                await self._invalidate(client)
            raise ProviderUnavailable("Akashic provider is unavailable") from exc
        return self.wrap_turn_handle(provider_handle)

    async def read_turn(self, thread_id: str, turn_id: str) -> MainAgentTurn:
        await self.read_thread(thread_id)
        raw_turn = await self._read_with_reconnect(
            lambda client: client.turn_read(thread_id, turn_id)
        )
        return await self._map_turn(raw_turn)

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> MainAgentTurn:
        await self.read_thread(thread_id)
        client = await self._get_client()
        wire = getattr(client, "_wire", None)
        if wire is None:
            raise ProviderUnavailable("Akashic turn interrupt is unavailable")
        handle = TurnHandle(wire, thread_id, turn_id)
        try:
            raw_turn = await handle.interrupt()
        except RemoteError as exc:
            raise self._map_remote_error(exc) from exc
        except _CONNECTION_ERRORS as exc:
            await self._invalidate(client)
            raise ProviderUnavailable("Akashic provider is unavailable") from exc
        return await self._map_turn(raw_turn)

    async def close(self) -> None:
        async with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            await client.close()

    def wrap_turn_handle(self, provider_handle: Any) -> MainAgentTurnHandle:
        return _AkashicTurnHandle(self, provider_handle)

    async def _map_item(self, raw_item: object) -> MainAgentItem:
        item = _as_mapping(raw_item, "turn item")
        item_id = item.get("id")
        item_type = item.get("type")
        if not isinstance(item_id, str) or not isinstance(item_type, str):
            raise ProviderProtocolError("turn item id and item type are required")
        try:
            return MainAgentItem(
                id=item_id,
                type=cast(Any, item_type),
                data=_as_json_mapping(item.get("data"), "turn item data"),
            )
        except (TypeError, ValueError) as exc:
            raise ProviderProtocolError(str(exc)) from exc

    async def _map_turn(
        self,
        raw_turn: object,
        *,
        default_created_at: datetime | None = None,
    ) -> MainAgentTurn:
        turn = _as_mapping(raw_turn, "turn")
        turn_id = turn.get("id")
        thread_id = turn.get("threadId")
        status = turn.get("status")
        if not isinstance(turn_id, str) or not isinstance(thread_id, str):
            raise ProviderProtocolError("turn id and threadId are required")
        if not isinstance(status, str):
            raise ProviderProtocolError("turn status is required")
        try:
            created_at = (
                _parse_datetime(turn.get("createdAt"), "turn createdAt")
                if turn.get("createdAt") is not None
                else default_created_at or datetime.now(UTC)
            )
            raw_items = turn.get("items", [])
            if not isinstance(raw_items, list):
                raise ProviderProtocolError("turn items must be an array")
            items = tuple([await self._map_item(item) for item in raw_items])
            raw_usage = turn.get("usage")
            raw_error = turn.get("error")
            usage = (
                None
                if raw_usage is None
                else _as_json_mapping(raw_usage, "turn usage")
            )
            error = (
                None
                if raw_error is None
                else _as_json_mapping(raw_error, "turn error")
            )
            return MainAgentTurn(
                id=turn_id,
                thread_id=thread_id,
                status=cast(TurnStatus, status),
                created_at=created_at,
                started_at=_parse_optional_datetime(
                    turn.get("startedAt"), "turn startedAt"
                ),
                completed_at=_parse_optional_datetime(
                    turn.get("completedAt"), "turn completedAt"
                ),
                final_response=cast(str | None, turn.get("finalResponse")),
                items=items,
                usage=usage,
                error=error,
            )
        except (TypeError, ValueError) as exc:
            raise ProviderProtocolError(str(exc)) from exc

    async def _map_event(
        self,
        raw_event: object,
        sequence: int,
    ) -> MainAgentEvent:
        event = _as_mapping(raw_event, "turn event")
        method = event.get("method")
        params = _as_mapping(event.get("params"), "turn event params")
        if not isinstance(method, str):
            raise ProviderProtocolError("turn event method is required")

        data: dict[str, JsonValue] = dict(
            _as_json_mapping(params, "turn event params")
        )
        event_type: EventType
        if method == "turn/queued":
            event_type = "queued"
        elif method == "turn/started":
            event_type = "started"
        elif method == "turn/completed":
            raw_turn = params.get("turn")
            mapped_turn = await self._map_turn(raw_turn)
            event_type = cast(EventType, mapped_turn.status)
        elif method.startswith("item/"):
            if "item" in params:
                item = await self._map_item(params["item"])
                data = {
                    "item": cast(
                        JsonValue,
                        {
                            "id": item.id,
                            "type": item.type,
                            "data": dict(item.data),
                        },
                    )
                }
                for key, value in params.items():
                    if key != "item":
                        data[key] = cast(JsonValue, value)
            else:
                data = dict(_as_json_mapping(params, "turn item event"))
            event_type = "item"
        else:
            raise ProviderProtocolError(f"unknown provider event method: {method}")

        if event_type not in {
            "queued",
            "started",
            "item",
            "completed",
            "failed",
            "interrupted",
            "cancelled",
        }:
            raise ProviderProtocolError(f"unknown provider event type: {event_type}")
        return MainAgentEvent(sequence=sequence, type=event_type, data=data)


class _AkashicTurnHandle(MainAgentTurnHandle):
    def __init__(self, adapter: AkashicAdapter, provider_handle: Any) -> None:
        self._adapter = adapter
        self._provider_handle = provider_handle
        self.thread_id = str(provider_handle.thread_id)
        self.id = str(provider_handle.id)
        self._created_at = datetime.now(UTC)

    async def events(self) -> AsyncIterator[MainAgentEvent]:
        sequence = 1
        try:
            async for raw_event in self._provider_handle.events():
                yield await self._adapter._map_event(raw_event, sequence)
                sequence += 1
        except (ProviderProtocolError, TurnStreamUnavailable):
            raise
        except _CONNECTION_ERRORS as exc:
            raise TurnStreamUnavailable("Akashic turn stream is unavailable") from exc
        except RemoteError as exc:
            raise TurnStreamUnavailable("Akashic turn stream failed") from exc

    async def result(self) -> MainAgentTurn:
        try:
            raw_turn = await self._provider_handle.result()
        except _CONNECTION_ERRORS as exc:
            raise TurnStreamUnavailable("Akashic turn result is unavailable") from exc
        return await self._adapter._map_turn(
            raw_turn,
            default_created_at=self._created_at,
        )

    async def interrupt(self) -> MainAgentTurn:
        try:
            raw_turn = await self._provider_handle.interrupt()
        except RemoteError as exc:
            raise self._adapter._map_remote_error(exc) from exc
        except _CONNECTION_ERRORS as exc:
            raise ProviderUnavailable("Akashic provider is unavailable") from exc
        return await self._adapter._map_turn(
            raw_turn,
            default_created_at=self._created_at,
        )
