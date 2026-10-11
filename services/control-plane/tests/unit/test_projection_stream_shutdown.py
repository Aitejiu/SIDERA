import asyncio
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from sidera_control_plane.api import projection_stream
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.projections import ProjectionChange


@pytest.fixture
def projection_case(monkeypatch):
    state = SimpleNamespace(
        active=0,
        opened=0,
        closed=0,
        expired=False,
        changes=(),
        cursor_checks=[],
        reads=[],
        polled=asyncio.Event(),
    )
    boundary = object()
    workspace_id = UUID("00000000-0000-0000-0000-000000000001")
    settings = Settings(
        database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused",
        projection_stream_poll_interval_seconds=0.01,
    )

    @contextmanager
    def session():
        state.active += 1
        state.opened += 1
        try:
            yield boundary
        finally:
            state.active -= 1
            state.closed += 1

    class Changes:
        def __init__(self, session, *, retention_seconds):
            assert session is boundary
            assert retention_seconds == settings.projection_change_retention_seconds

        def cursor_expired(self, workspace_id, cursor):
            state.cursor_checks.append((workspace_id, cursor))
            return state.expired

        def list_after(self, workspace_id, *, cursor, limit):
            state.reads.append((workspace_id, cursor, limit))
            state.polled.set()
            return state.changes

    monkeypatch.setattr(projection_stream, "ProjectionChangeRepository", Changes)
    database = SimpleNamespace(session=session)

    def stream(*, schema_version=1, **kwargs):
        return projection_stream._event_stream(
            database,
            settings,
            workspace_id,
            start_cursor=7,
            schema_version=schema_version,
            **kwargs,
        )

    return SimpleNamespace(
        state=state, workspace_id=workspace_id, settings=settings, stream=stream
    )


@pytest.mark.parametrize("after_connected", (False, True))
def test_shutdown_ends_before_opening_a_polling_session(
    projection_case, after_connected
):
    s = projection_case

    async def exercise():
        closing = asyncio.Event()
        stream = s.stream(shutdown_check=closing.is_set)
        try:
            if after_connected:
                assert await anext(stream) == ": connected\n\n"
            closing.set()
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert s.state.opened == s.state.closed == s.state.active == 0
            assert s.state.reads == [] and s.state.cursor_checks == []
        finally:
            await stream.aclose()

    asyncio.run(exercise())


def test_shutdown_ends_idle_read_after_one_actual_poll(projection_case):
    s = projection_case

    async def exercise():
        closing = asyncio.Event()
        stream = s.stream(shutdown_check=closing.is_set)
        read = None
        try:
            assert await anext(stream) == ": connected\n\n"
            read = asyncio.create_task(anext(stream))
            await asyncio.wait_for(s.state.polled.wait(), timeout=1)
            assert not read.done()
            assert s.state.reads == [(s.workspace_id, 7, 100)]
            assert s.state.opened == s.state.closed == 1 and s.state.active == 0
            closing.set()
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(read, timeout=0.5)
            assert s.state.opened == s.state.closed == 1 and s.state.active == 0
        finally:
            if read is not None:
                read.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await read
            await stream.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("with_callback", (False, True))
def test_default_and_false_shutdown_preserve_cursor_and_literal_events(
    projection_case, with_callback
):
    s = projection_case
    s.state.changes = (
        ProjectionChange(
            workspace_id=s.workspace_id,
            cursor=8,
            schema_version=1,
            projection_type="task",
            resource_id=UUID("00000000-0000-0000-0000-000000000003"),
            change_type="updated",
            resource_version=4,
            occurred_at=datetime(2026, 10, 8, tzinfo=UTC),
            correlation_id=UUID("00000000-0000-0000-0000-000000000002"),
        ),
    )

    async def exercise():
        stream = s.stream(
            **({"shutdown_check": lambda: False} if with_callback else {})
        )
        try:
            assert await anext(stream) == ": connected\n\n"
            assert await anext(stream) == (
                "id: 8\n"
                "event: projection.changed\n"
                'data: {"change_type":"updated",'
                '"correlation_id":"00000000-0000-0000-0000-000000000002",'
                '"cursor":8,"occurred_at":"2026-10-08T00:00:00+00:00",'
                '"projection_type":"task",'
                '"resource_id":"00000000-0000-0000-0000-000000000003",'
                '"resource_version":4,"schema_version":1,'
                '"workspace_id":"00000000-0000-0000-0000-000000000001"}\n\n'
            )
            assert s.state.opened == s.state.closed == 1 and s.state.active == 0
            s.state.changes = ()
            s.state.expired = True
            assert await anext(stream) == (
                "event: resync_required\n"
                'data: {"reason":"cursor_expired","schema_version":1}\n\n'
            )
            assert s.state.opened == s.state.closed == 2 and s.state.active == 0
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert s.state.cursor_checks == [(s.workspace_id, 7), (s.workspace_id, 8)]
            assert s.state.reads == [(s.workspace_id, 7, 100)]
        finally:
            await stream.aclose()

    asyncio.run(exercise())


def test_unsupported_schema_resync_closes_session_before_yield(projection_case):
    s = projection_case

    async def exercise():
        stream = s.stream(schema_version=2)
        try:
            assert await anext(stream) == ": connected\n\n"
            assert await anext(stream) == (
                "event: resync_required\n"
                'data: {"reason":"unsupported_schema","schema_version":1}\n\n'
            )
            assert s.state.opened == s.state.closed == 1 and s.state.active == 0
            assert s.state.cursor_checks == [] and s.state.reads == []
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
        finally:
            await stream.aclose()

    asyncio.run(exercise())


def test_idle_heartbeat_is_unchanged_without_shutdown_callback(projection_case):
    s = projection_case
    s.settings.projection_stream_heartbeat_seconds = 0.001

    async def exercise():
        stream = s.stream()
        try:
            assert await anext(stream) == ": connected\n\n"
            assert (
                await asyncio.wait_for(anext(stream), timeout=0.5) == ": heartbeat\n\n"
            )
            assert s.state.reads and s.state.active == 0
            assert s.state.opened == s.state.closed
        finally:
            await stream.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("after_connected", (False, True))
def test_shutdown_predicate_failure_ends_stream_without_database_access(
    projection_case, after_connected
):
    s = projection_case

    async def exercise():
        failing = asyncio.Event()

        def shutdown_check():
            if failing.is_set():
                raise RuntimeError("controlled shutdown predicate failure")
            return False

        stream = s.stream(shutdown_check=shutdown_check)
        try:
            if after_connected:
                assert await anext(stream) == ": connected\n\n"
            failing.set()
            with pytest.raises(
                RuntimeError, match="controlled shutdown predicate failure"
            ):
                await anext(stream)
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert s.state.opened == s.state.closed == s.state.active == 0
        finally:
            await stream.aclose()

    asyncio.run(exercise())
