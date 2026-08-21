import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from starlette.responses import StreamingResponse

from sidera_control_plane.api.dependencies import get_database, get_settings
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.projection_change_repository import (
    ProjectionChangeRepository,
)

router = APIRouter(prefix="/api/v1/workspaces")
SUPPORTED_SCHEMA_VERSION = 1


@router.get("/{workspace_id}/projection-changes")
def projection_changes(
    workspace_id: UUID,
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    after: Annotated[int, Query(ge=0)] = 0,
    schema_version: Annotated[int, Query(ge=1)] = SUPPORTED_SCHEMA_VERSION,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    start_cursor = _resolve_start_cursor(after, last_event_id)

    return StreamingResponse(
        _event_stream(
            database,
            settings,
            workspace_id,
            start_cursor=start_cursor,
            schema_version=schema_version,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _resolve_start_cursor(after: int, last_event_id: str | None) -> int:
    if last_event_id is None or not last_event_id.strip():
        return after
    try:
        cursor = int(last_event_id.strip())
    except ValueError as exc:
        raise ValidationError("Last-Event-ID must be an integer") from exc
    if cursor < 0:
        raise ValidationError("Last-Event-ID must be non-negative")
    return cursor


async def _event_stream(
    database: Database,
    settings: Settings,
    workspace_id: UUID,
    *,
    start_cursor: int,
    schema_version: int,
) -> AsyncIterator[str]:
    cursor = start_cursor
    last_heartbeat = asyncio.get_running_loop().time()
    while True:
        with database.session() as session:
            repository = ProjectionChangeRepository(
                session,
                retention_seconds=settings.projection_change_retention_seconds,
            )
            if schema_version != SUPPORTED_SCHEMA_VERSION:
                yield _resync_event("unsupported_schema")
                return
            if repository.cursor_expired(workspace_id, cursor):
                yield _resync_event("cursor_expired")
                return
            changes = repository.list_after(workspace_id, cursor=cursor, limit=100)

        if changes:
            for change in changes:
                cursor = change.cursor
                yield _change_event(change.cursor, change.to_payload())
            continue

        now = asyncio.get_running_loop().time()
        if now - last_heartbeat >= settings.projection_stream_heartbeat_seconds:
            last_heartbeat = now
            yield ": heartbeat\n\n"
        await asyncio.sleep(settings.projection_stream_poll_interval_seconds)


def _change_event(cursor: int, payload: dict[str, object]) -> str:
    return (
        f"id: {cursor}\n"
        "event: projection.changed\n"
        f"data: {json.dumps(payload, sort_keys=True, separators=(',', ':'))}\n\n"
    )


def _resync_event(reason: str) -> str:
    return (
        "event: resync_required\n"
        f"data: {json.dumps({'reason': reason, 'schema_version': SUPPORTED_SCHEMA_VERSION}, separators=(',', ':'))}\n\n"
    )
