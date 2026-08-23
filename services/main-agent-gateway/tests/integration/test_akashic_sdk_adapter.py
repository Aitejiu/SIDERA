from __future__ import annotations

# The integration boundary imports the separately-owned Akashic host source.
# Runtime resolution is installed by tests/conftest.py.
# pyright: reportMissingImports=false
import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from agent.control.models import TurnRequest
from agent.control.runtime import ConversationRuntime
from agent.control.service import ControlService
from infra.control.socket import SocketAppServer
from session.manager import SessionManager

from sidera_main_agent_gateway.adapters.akashic import AkashicAdapter
from sidera_main_agent_gateway.config import Settings

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000002")


@pytest.mark.asyncio
async def test_adapter_runs_against_real_akashic_socket(tmp_path: Path) -> None:
    sessions = SessionManager(tmp_path)

    async def execute(request: TurnRequest) -> str:
        return f"adapter:{request.input}"

    runtime = ConversationRuntime(sessions.control_store, execute)
    server = SocketAppServer(
        Path("/tmp") / f"sga-{uuid4().hex}.sock",
        ControlService(runtime, sessions, tmp_path),
    )
    await server.start()
    adapter = AkashicAdapter(
        Settings(
            akashic_endpoint=str(server.endpoint),
            sidera_workspace_id=WORKSPACE_ID,
        )
    )
    try:
        thread = await adapter.create_thread(
            title="Real adapter thread",
            context_refs=("task:real",),
        )
        assert thread.title == "Real adapter thread"

        page = await adapter.list_threads()
        assert [item.id for item in page.threads] == [thread.id]
        assert (await adapter.read_thread(thread.id)).context_refs == ("task:real",)

        handle = await adapter.start_turn(thread.id, "hello")
        events = [event async for event in handle.events()]
        result = await handle.result()
        assert [event.type for event in events][-1] == "completed"
        assert result.final_response == "adapter:hello"

        terminal = await adapter.interrupt_turn(thread.id, handle.id)
        assert terminal.status == "completed"
    finally:
        await adapter.close()
        await server.stop()
        await runtime.shutdown()
        sessions.close()


@pytest.mark.asyncio
async def test_adapter_interrupts_active_real_turn(tmp_path: Path) -> None:
    sessions = SessionManager(tmp_path)
    started = asyncio.Event()

    async def execute(_request: TurnRequest) -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    runtime = ConversationRuntime(sessions.control_store, execute)
    server = SocketAppServer(
        Path("/tmp") / f"sga-{uuid4().hex}.sock",
        ControlService(runtime, sessions, tmp_path),
    )
    await server.start()
    adapter = AkashicAdapter(
        Settings(
            akashic_endpoint=str(server.endpoint),
            sidera_workspace_id=WORKSPACE_ID,
        )
    )
    try:
        thread = await adapter.create_thread(title="Interrupt", context_refs=())
        handle = await adapter.start_turn(thread.id, "wait")
        await started.wait()

        terminal = await adapter.interrupt_turn(thread.id, handle.id)

        assert terminal.status == "interrupted"
    finally:
        await adapter.close()
        await server.stop()
        await runtime.shutdown()
        sessions.close()
