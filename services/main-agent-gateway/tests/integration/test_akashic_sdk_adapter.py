from __future__ import annotations

# The integration boundary imports the separately-owned Akashic host source.
# Runtime resolution is installed by tests/conftest.py.
# pyright: reportMissingImports=false
import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from agent.control.models import TurnRequest
from agent.control.runtime import ConversationRuntime
from agent.control.service import ControlService
from infra.control.socket import SocketAppServer
from session.manager import SessionManager
from sidera_runtime_guard import RuntimeBudgetGuard, prepare_run

from sidera_main_agent_gateway.adapters.akashic import AkashicAdapter
from sidera_main_agent_gateway.config import Settings

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000002")


def _acceptance_guard(tmp_path: Path) -> RuntimeBudgetGuard:
    run_root = tmp_path / "guard-run"
    approval = run_root / "approval.json"
    approval.parent.mkdir()
    run_id = "20000000-0000-0000-0000-000000000201"
    fingerprints = {
        "implementation_fingerprint": "a" * 64,
        "runtime_fingerprint": "b" * 64,
    }
    approval.write_text(
        json.dumps(
            {
                "approved": True,
                "run_id": run_id,
                "workspace_id": str(WORKSPACE_ID),
                **fingerprints,
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "workspace_id": str(WORKSPACE_ID),
        **fingerprints,
        "run_root": str(run_root),
        "approval_ref": str(approval),
        "resource_binding_mode": "persisted",
        "batch_limits": {
            "main_agent_turns": 1,
            "planner_runs": 0,
            "worker_sessions": 0,
            "adapter_prompts": 0,
        },
        "scenarios": {
            "profile": {
                "max_seconds": 60,
                "limits": {
                    "main_agent_turns": 1,
                    "planner_runs": 0,
                    "worker_sessions": 0,
                    "adapter_prompts": 0,
                },
            }
        },
        "allowed_routes": [
            {
                "route_id": "main-local",
                "role": "main_agent",
                "runtime": "akashic-socket",
                "provider": "local-akashic",
                "model_id": "local-control",
                "endpoint_host": "local-socket",
                "credential_ref": "none",
                "config_version": 1,
                "fallbacks": [],
                "costs": {"main_agent_turns": 1},
            }
        ],
    }
    manifest_path = tmp_path / "guard-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    prepare_run(manifest_path)
    return RuntimeBudgetGuard(manifest_path)


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
async def test_guarded_adapter_turn_runs_against_real_akashic_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = SessionManager(tmp_path)

    async def execute(request: TurnRequest) -> str:
        return f"guarded:{request.input}"

    runtime = ConversationRuntime(sessions.control_store, execute)
    server = SocketAppServer(
        Path("/tmp") / f"sga-{uuid4().hex}.sock",
        ControlService(runtime, sessions, tmp_path),
    )
    await server.start()
    monkeypatch.setenv(
        "SIDERA_ACCEPTANCE_ROUTE_FACTS",
        json.dumps(
            {
                "main_agent": {
                    "route_id": "main-local",
                    "runtime": "akashic-socket",
                    "provider": "local-akashic",
                    "model_id": "local-control",
                    "endpoint_host": "local-socket",
                    "credential_ref": "none",
                    "config_version": 1,
                    "fallbacks": [],
                }
            }
        ),
    )
    adapter = AkashicAdapter(
        Settings(
            akashic_endpoint=str(server.endpoint),
            sidera_workspace_id=WORKSPACE_ID,
        ),
        acceptance_guard=_acceptance_guard(tmp_path),
    )
    try:
        thread = await adapter.create_thread(
            title="Guarded real adapter thread",
            context_refs=("task:guarded-real",),
        )

        handle = await adapter.start_turn(
            thread.id,
            "hello guarded",
            operation_id="main-real-operation-1",
        )
        result = await handle.result()

        assert result.final_response == "guarded:hello guarded"
        state = json.loads(
            (tmp_path / "guard-run" / "acceptance-state.json").read_text(
                encoding="utf-8"
            )
        )
        operation = state["operations"]["main-real-operation-1"]
        assert operation["state"] == "completed"
        assert operation["resource_chain"] == [
            f"workspace:{WORKSPACE_ID}",
            f"thread:{thread.id}",
        ]
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
