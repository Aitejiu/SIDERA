"""R6.3.2 Main Agent governance loop against a local Control Plane."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from agent.tools.base import ToolExecutionContext, tool_execution_context_scope

import plugin
from sidera_task_bridge.client import (
    CommandDispatcher,
    CommandExecution,
    CommandResultUnknown,
    ControlPlaneClient,
    TransportUnavailable,
)
from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.source_routes import SourceContextResolver
from sidera_task_bridge.state import PrivateRoute
from sidera_task_bridge.tools import SideraTaskTools

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE_ROOT = REPOSITORY_ROOT / "services/control-plane"
SEED_SCRIPT = CONTROL_PLANE_ROOT / "tests/e2e/seed_r6_3_2_governance.py"
BASELINE_RUNTIME_SCRIPT = CONTROL_PLANE_ROOT / "tests/e2e/r6_3_1_fake_runtime.py"
GOVERNANCE_RUNTIME_SCRIPT = CONTROL_PLANE_ROOT / "tests/e2e/r6_3_2_governance_runtime.py"
DEFAULT_PYTHON = CONTROL_PLANE_ROOT / ".venv/bin/python"

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000007")
PROFILE_ID = UUID("20000000-0000-0000-0000-000000000008")
SOURCE_REF = "src_r6_3_2_akashic"
SESSION_KEY = "telegram:r6-3-2-akashic"
ACTOR_TYPE = "local"
ACTOR_ID = "aitejiu"


class MemoryKV:
    def __init__(self) -> None:
        self.value: object = None

    def get(self, key: str, default: object = None) -> object:
        del key
        return self.value if self.value is not None else default

    def set(self, key: str, value: object) -> None:
        del key
        self.value = value


class MainAgentContext:
    def __init__(self, config: TaskBridgeConfig) -> None:
        self.config = config
        self.kv_store = MemoryKV()
        self.tool_registry = object()
        self.session_manager = None
        self.data_dir = Path("/tmp/sidera/plugin-validation/r6-3-2")
        self.generation_id = ""
        self._can_start_tasks: Any = None

    def create_task(
        self, coroutine: object, *, name: str | None = None
    ) -> asyncio.Task[object]:
        return asyncio.create_task(cast(Any, coroutine), name=name)


class ForwardThenUnknownClient:
    """Make the POST durable, then hide its response like a lost network reply."""

    def __init__(self, client: ControlPlaneClient) -> None:
        self._client = client
        self.sent = 0

    def build_request_json(self, envelope: Any) -> dict[str, Any]:
        return self._client.build_request_json(envelope)

    def build_intent_request_json(self, intent: Any) -> dict[str, Any]:
        return self._client.build_intent_request_json(intent)

    async def execute_command_json(
        self, request_json: dict[str, Any], command_id: UUID
    ) -> CommandExecution:
        self.sent += 1
        await self._client.execute_command_json(request_json, command_id)
        raise CommandResultUnknown(command_id)

    async def get_command_status(self, command_id: UUID) -> Any:
        return await self._client.get_command_status(command_id)


class QueryOutageTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fixture query service is offline", request=request)


def _require_gate() -> tuple[str, str]:
    if os.environ.get("SIDERA_RUN_R6_3_2_AKASHIC") != "1":
        pytest.skip("SIDERA_RUN_R6_3_2_AKASHIC is not enabled")
    database_url = os.environ.get("SIDERA_DATABASE_URL")
    if not database_url:
        pytest.fail("SIDERA_DATABASE_URL is required when the Akashic gate is enabled")
    return (
        database_url,
        os.environ.get("SIDERA_CONTROL_PLANE_URL", "http://127.0.0.1:8000").rstrip(
            "/"
        ),
    )


def _decode(value: str) -> dict[str, Any]:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise TypeError("Akashic tool returned a non-object envelope")
    return cast(dict[str, Any], payload)


def _config(control_plane_url: str, profile_id: UUID) -> TaskBridgeConfig:
    return TaskBridgeConfig.model_validate(
        {
            "workspace_id": WORKSPACE_ID,
            "planning_profile_id": profile_id,
            "planning_profile_version": 1,
            "control_plane_url": control_plane_url,
            "web_base_url": os.environ.get(
                "SIDERA_WEB_URL", "http://127.0.0.1:4173"
            ),
            "governance_actor_type": ACTOR_TYPE,
            "governance_actor_id": ACTOR_ID,
        }
    )


def _fixture_python() -> str:
    return os.environ.get("SIDERA_E2E_PYTHON", str(DEFAULT_PYTHON))


def _run_fixture_script(
    script: Path,
    database_url: str,
    *extra_args: str,
    allow_seed: bool = False,
) -> dict[str, Any]:
    result = subprocess.run(
        [
            _fixture_python(),
            str(script),
            "--workspace-id",
            str(WORKSPACE_ID),
            *extra_args,
        ],
        cwd=CONTROL_PLANE_ROOT,
        env={
            **os.environ,
            "SIDERA_DATABASE_URL": database_url,
            **({"SIDERA_ALLOW_E2E_SEED": "1"} if allow_seed else {}),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"{script.name} failed with exit code {result.returncode}: "
            f"{result.stderr[-2000:]}"
        )
    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise TypeError(f"{script.name} returned a non-object summary")
    return cast(dict[str, Any], payload)


async def _run_loop(database_url: str, control_plane_url: str) -> None:
    fixture = _run_fixture_script(SEED_SCRIPT, database_url, "--reset", allow_seed=True)
    _run_fixture_script(BASELINE_RUNTIME_SCRIPT, database_url)
    root_task_id = UUID(str(fixture["root_task_id"]))
    downstream_worker_item_id = UUID(
        str(fixture["worker_item_ids"]["downstream"])
    )

    context = MainAgentContext(_config(control_plane_url, PROFILE_ID))
    bridge = plugin.SideraTaskBridge()
    cast(Any, bridge).context = context
    await bridge.prepare()
    client = cast(ControlPlaneClient, bridge._client)
    state = bridge._state
    if state is None:
        raise AssertionError("Akashic bridge state was not initialized")
    await state.save_route(
        SOURCE_REF,
        PrivateRoute(
            channel="telegram",
            chat_id="r6-3-2-akashic",
            session_key=SESSION_KEY,
        ),
    )
    await state.bind_resource_source(
        "task", root_task_id, "akashic_private", SOURCE_REF
    )
    source_context = ToolExecutionContext(
        origin_channel="telegram",
        origin_chat_id="r6-3-2-akashic",
        origin_session_key=SESSION_KEY,
    )
    wrong_context = ToolExecutionContext(
        origin_channel="telegram",
        origin_chat_id="other-chat",
        origin_session_key="telegram:other-chat",
    )

    try:
        with tool_execution_context_scope(source_context):
            governance_read = _decode(
                await bridge.sidera_get_collaboration_governance(
                    None, str(downstream_worker_item_id)
                )
            )
        assert governance_read["outcome"] == "ok"
        governance = governance_read["data"]["governance"]
        assert governance["governance_mode"] == "automatic"

        worker_workspace = await client.get_worker_item_workspace(
            downstream_worker_item_id
        )
        worker_item = worker_workspace["worker_item"]
        assert isinstance(worker_item, dict)
        worker_version_value = worker_item["version"]
        assert isinstance(worker_version_value, int)
        worker_version = worker_version_value
        active_plan = await client.get_active_plan(root_task_id)
        active_generation = active_plan["active_generation"]
        assert isinstance(active_generation, int)
        with tool_execution_context_scope(source_context):
            paused = _decode(
                await bridge.sidera_pause_collaboration_release(
                    None,
                    str(downstream_worker_item_id),
                    worker_version,
                    active_generation,
                    "pause for Main Agent review",
                )
            )
        assert paused["outcome"] == "ok", paused

        _run_fixture_script(GOVERNANCE_RUNTIME_SCRIPT, database_url)

        exact_proposal = await client.get_collaboration_governance_worker_item(
            downstream_worker_item_id
        )
        governance = exact_proposal["governance"]
        assert isinstance(governance, dict)
        active_proposal = governance["active_proposal"]
        assert isinstance(active_proposal, dict)
        proposal_id = UUID(str(active_proposal["proposal_id"]))
        proposal_version_value = active_proposal["resource_version"]
        assert isinstance(proposal_version_value, int)
        proposal_version = proposal_version_value

        with tool_execution_context_scope(wrong_context):
            wrong_source = _decode(
                await bridge.sidera_approve_collaboration_release(
                    None, str(proposal_id), proposal_version, True, "wrong source"
                )
            )
        assert wrong_source["outcome"] == "conflict", wrong_source
        assert wrong_source["error_code"] == "source_binding_conflict"

        with tool_execution_context_scope(source_context):
            approved = _decode(
                await bridge.sidera_approve_collaboration_release(
                    None, str(proposal_id), proposal_version, True, "exact approval"
                )
            )
        assert approved["outcome"] == "ok", approved
        assert approved["data"]["proposal_status"] == "approved"
        assert approved["data"]["application_status"] == "applied"
        assert approved["data"]["release_id"]

        with tool_execution_context_scope(source_context):
            replay = _decode(
                await bridge.sidera_approve_collaboration_release(
                    None, str(proposal_id), proposal_version, True, "replay"
                )
            )
        assert replay["outcome"] == "conflict"

        raw_proposal = await client.get_collaboration_governance_proposal(proposal_id)
        assert raw_proposal["status"] == "approved"
        async with httpx.AsyncClient(base_url=control_plane_url, trust_env=False) as web:
            web_response = await web.get(
                f"/api/v1/workspaces/{WORKSPACE_ID}/collaboration-governance/proposals/{proposal_id}"
            )
        web_response.raise_for_status()
        assert web_response.json()["status"] == raw_proposal["status"]

        flaky_client = ForwardThenUnknownClient(client)
        flaky_dispatcher = CommandDispatcher(
            flaky_client,
            state,
            owned_command_types={
                "PauseCollaborationRelease",
                "ApproveCollaborationReleaseProposal",
                "RejectCollaborationReleaseProposal",
                "ResumeAutomaticCollaboration",
            },
        )
        direct_tools = SideraTaskTools(
            context.config,
            client=client,
            dispatcher=flaky_dispatcher,
            state=state,
            source_resolver=SourceContextResolver(
                source_ref_factory=lambda: SOURCE_REF
            ),
        )
        hold_id = UUID(str(paused["data"]["hold_id"]))
        hold_version = int(paused["data"]["hold_version"])
        with tool_execution_context_scope(source_context):
            recovered = _decode(
                await direct_tools.sidera_resume_automatic_collaboration(
                    None,
                    str(downstream_worker_item_id),
                    str(hold_id),
                    hold_version,
                    "result unknown recovery",
                )
            )
        assert recovered["outcome"] == "ok", recovered
        assert flaky_client.sent == 1
        assert await state.list_unresolved() == ()
    finally:
        await bridge.terminate()


async def _query_outage_is_read_only(control_plane_url: str, profile_id: UUID) -> None:
    config = _config(control_plane_url, profile_id)
    client = ControlPlaneClient(config, transport=QueryOutageTransport())
    try:
        with pytest.raises(TransportUnavailable):
            await client.get_collaboration_governance_worker_item(UUID(int=1))
    finally:
        await client.close()


def test_real_akashic_main_agent_governance_loop() -> None:
    database_url, control_plane_url = _require_gate()
    asyncio.run(_run_loop(database_url, control_plane_url))


def test_query_outage_does_not_write_ledger() -> None:
    _database_url, control_plane_url = _require_gate()
    asyncio.run(
        _query_outage_is_read_only(
            control_plane_url,
            PROFILE_ID,
        )
    )
