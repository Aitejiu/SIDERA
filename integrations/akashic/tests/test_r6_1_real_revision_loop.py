from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from agent.tools.base import ToolExecutionContext, tool_execution_context_scope

import plugin
from sidera_task_bridge.client import ControlPlaneClient
from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.contracts import ProjectionChange
from sidera_task_bridge.notifications import NotificationEvaluator
from sidera_task_bridge.state import BridgeStateStore

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000003")
TASK_ID = UUID("f7000000-0000-0000-0000-000000000001")
TRANSITION_ID = UUID("f7000000-0000-0000-0000-000000000002")
REQUEST_ID = UUID("f7000000-0000-0000-0000-000000000003")
CORRELATION_ID = UUID("f7000000-0000-0000-0000-000000000004")
SUBTASK_PATH = "Task/Running revision control room > Subtask/Blocked branch > WorkerItem/Backend"

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE_ROOT = REPOSITORY_ROOT / "services/control-plane"
REVISION_SEED = CONTROL_PLANE_ROOT / "tests/e2e/seed_r6_1_revision.py"
DEFAULT_PYTHON = CONTROL_PLANE_ROOT / ".venv/bin/python"


class MemoryKV:
    def __init__(self) -> None:
        self.value: object = None

    def get(self, key: str, default: object = None) -> object:
        del key
        return self.value if self.value is not None else default

    def set(self, key: str, value: object) -> None:
        del key
        self.value = value


class PluginContext:
    def __init__(self, config: TaskBridgeConfig) -> None:
        self.config = config
        self.kv_store = MemoryKV()
        self.tool_registry = object()
        self.session_manager = None
        self.data_dir = Path("/tmp/sidera/plugin-validation/r6-1")
        self.generation_id = ""
        self._can_start_tasks: Any = None

    def create_task(
        self, coroutine: object, *, name: str | None = None
    ) -> asyncio.Task[object]:
        return asyncio.create_task(cast(Any, coroutine), name=name)


class TransitionNotificationClient:
    def __init__(self) -> None:
        self.detail: dict[str, Any] = {
            "kind": "revision",
            "status": "proposal_ready",
            "version": 2,
            "source_type": "plan_revision",
            "source_ref": "revision-command",
            "revision_base": {"root_task_id": str(TASK_ID)},
            "current_proposal_revision": 1,
            "current_proposal_content_hash": "proposal-hash",
        }
        self.revision_diff: dict[str, Any] = {
            "affected_branch_paths": [SUBTASK_PATH],
        }
        self.transition: dict[str, Any] = {
            "plan_transition_id": str(TRANSITION_ID),
            "status": "degraded",
            "version": 2,
            "root_task": {
                "id": str(TASK_ID),
                "title": "Running revision control room",
                "path": "Task/Running revision control room",
            },
            "workers": [
                {
                    "path": SUBTASK_PATH,
                    "status": "blocked_unknown",
                    "failure_code": "adapter_result_unknown",
                }
            ],
        }

    async def get_planning_detail(self, _request_id: UUID) -> dict[str, Any]:
        return self.detail

    async def get_revision_diff(self, _request_id: UUID) -> dict[str, Any]:
        return self.revision_diff

    async def get_plan_transition(self, _transition_id: UUID) -> dict[str, Any]:
        return self.transition


def _change(
    projection_type: str,
    resource_id: UUID,
    cursor: int,
) -> ProjectionChange:
    return ProjectionChange.model_validate(
        {
            "workspace_id": str(WORKSPACE_ID),
            "cursor": cursor,
            "schema_version": 1,
            "projection_type": projection_type,
            "resource_id": str(resource_id),
            "change_type": "r6_1_vertical_gate",
            "resource_version": cursor,
            "occurred_at": "2026-08-29T12:00:00Z",
            "correlation_id": str(CORRELATION_ID),
        }
    )


def test_revision_notifications_include_owned_path_and_transition_state() -> None:
    async def scenario() -> None:
        client = TransitionNotificationClient()
        state = BridgeStateStore(MemoryKV())
        await state.bind_resource_source(
            "planning_request", REQUEST_ID, "akashic_private", "src_private"
        )
        await state.bind_resource_source(
            "task", TASK_ID, "akashic_private", "src_private"
        )
        config = TaskBridgeConfig.model_validate(
            {
                "workspace_id": WORKSPACE_ID,
                "planning_profile_id": UUID("f7000000-0000-0000-0000-000000000005"),
                "planning_profile_version": 1,
            }
        )
        evaluator = NotificationEvaluator(config, client=client, state=state)

        proposal = await evaluator.evaluate(
            _change("planning_request", REQUEST_ID, 1), mode="live"
        )
        assert proposal is not None and proposal.notice is not None
        assert proposal.notice.kind == "planning_proposal_ready"
        assert SUBTASK_PATH in proposal.notice.message
        assert proposal.notice.web_url.endswith(
            f"/tasks/{TASK_ID}/revisions/{REQUEST_ID}"
        )

        degraded = await evaluator.evaluate(
            _change("plan_transition", TRANSITION_ID, 2), mode="live"
        )
        assert degraded is not None and degraded.notice is not None
        assert degraded.notice.kind == "plan_transition_degraded"
        assert SUBTASK_PATH in degraded.notice.message
        assert degraded.notice.source_ref == "src_private"

        client.transition = {
            **client.transition,
            "status": "converged",
            "version": 3,
            "workers": [{"path": SUBTASK_PATH, "status": "converged"}],
        }
        converged = await evaluator.evaluate(
            _change("plan_transition", TRANSITION_ID, 3), mode="live"
        )
        assert converged is not None and converged.notice is not None
        assert converged.notice.kind == "plan_transition_converged"
        assert SUBTASK_PATH in converged.notice.message

    asyncio.run(scenario())


def test_real_akashic_revision_matches_web_query_and_replays_exact_command() -> None:
    config = _require_live_gate()
    fixture = _seed_fixture(config["database_url"], config["workspace_id"])
    asyncio.run(_run_real_revision_loop(config, fixture))


def _require_live_gate() -> dict[str, str]:
    if os.environ.get("SIDERA_RUN_R6_1_AKASHIC") != "1":
        pytest.skip("SIDERA_RUN_R6_1_AKASHIC is not enabled")
    database_url = os.environ.get("SIDERA_DATABASE_URL")
    if not database_url:
        pytest.fail("SIDERA_DATABASE_URL is required when the Akashic gate is enabled")
    return {
        "database_url": database_url,
        "control_plane_url": os.environ.get(
            "SIDERA_CONTROL_PLANE_URL", "http://127.0.0.1:8000"
        ).rstrip("/"),
        "web_url": os.environ.get(
            "SIDERA_WEB_URL", "http://127.0.0.1:4173"
        ).rstrip("/"),
        "workspace_id": os.environ.get(
            "SIDERA_R6_1_WORKSPACE_ID", str(WORKSPACE_ID)
        ),
    }


def _seed_fixture(database_url: str, workspace_id: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            os.environ.get("SIDERA_E2E_PYTHON", str(DEFAULT_PYTHON)),
            str(REVISION_SEED),
            "--workspace-id",
            workspace_id,
            "--reset",
        ],
        cwd=CONTROL_PLANE_ROOT,
        env={
            **os.environ,
            "SIDERA_ALLOW_E2E_SEED": "1",
            "SIDERA_DATABASE_URL": database_url,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"R6.1 Akashic fixture seed failed with exit code {result.returncode}"
        )
    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise TypeError("R6.1 Akashic fixture seed returned a non-object")
    return cast(dict[str, Any], payload)


def _run_control_plane_once(module: str, database_url: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            os.environ.get("SIDERA_E2E_PYTHON", str(DEFAULT_PYTHON)),
            "-m",
            module,
            "--once",
        ],
        cwd=CONTROL_PLANE_ROOT,
        env={
            **os.environ,
            "SIDERA_DATABASE_URL": database_url,
            "SIDERA_CONTEXT_PACKAGE_CUTOVER_AT": os.environ.get(
                "SIDERA_CONTEXT_PACKAGE_CUTOVER_AT", "2026-08-27T00:00:00+08:00"
            ),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"{module} failed with exit code {result.returncode}")
    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise TypeError(f"{module} returned a non-object summary")
    return cast(dict[str, Any], payload)


async def _run_real_revision_loop(
    config_values: Mapping[str, str], fixture: Mapping[str, Any]
) -> None:
    workspace_id = UUID(config_values["workspace_id"])
    root_task_id = UUID(str(fixture["root_task_id"]))
    profile_id = UUID(str(fixture["planning_profile_id"]))
    bridge_config = TaskBridgeConfig.model_validate(
        {
            "workspace_id": workspace_id,
            "planning_profile_id": profile_id,
            "planning_profile_version": 1,
            "control_plane_url": config_values["control_plane_url"],
            "web_base_url": config_values["web_url"],
        }
    )
    context = PluginContext(bridge_config)
    bridge = plugin.SideraTaskBridge()
    cast(Any, bridge).context = context
    await bridge.prepare()
    client = cast(ControlPlaneClient, bridge._client)
    original_execute = client.execute_command_json
    captured: dict[str, Any] = {}

    async def capture_command(
        request_json: dict[str, Any], command_id: UUID
    ) -> Any:
        captured["request"] = dict(request_json)
        captured["command_id"] = command_id
        return await original_execute(request_json, command_id)

    client.execute_command_json = capture_command  # type: ignore[method-assign]
    try:
        with tool_execution_context_scope(
            ToolExecutionContext(
                origin_channel="telegram",
                origin_chat_id="r6-1-akashic-gate",
                origin_session_key="telegram:r6-1-akashic-gate",
            )
        ):
            created = _decode(
                await bridge.sidera_create_plan_revision(
                    None,
                    str(root_task_id),
                    "Keep completed context and adjust only the blocked branch.",
                    ["Reuse existing WorkerItem identities where safe."],
                    ["The new execution graph is reviewable before confirmation."],
                )
            )
        assert created["outcome"] == "ok", created
        request_id = UUID(str(created["data"]["planning_request_id"]))
        assert created["resource_ref"] == f"revision:{request_id}"
        assert created["web_url"].endswith(
            f"/tasks/{root_task_id}/revisions/{request_id}"
        )
        assert captured["request"]["command_type"] == "SubmitPlanRevision"
        assert captured["request"]["target_id"] == str(root_task_id)

        state = bridge._state
        assert state is not None
        state_snapshot = await state.snapshot()
        binding = state_snapshot.resource_sources[f"planning_request:{request_id}"]
        assert binding.source_type == "akashic_private"
        assert binding.source_ref in state_snapshot.source_routes

        replayed = await original_execute(captured["request"], captured["command_id"])
        assert replayed.status == "applied"
        assert replayed.result is not None
        assert replayed.result.aggregate_id == request_id
        assert captured["request"]["command_id"] == str(captured["command_id"])

        detail_before = await client.get_planning_detail(request_id)
        assert detail_before["source_type"] == "plan_revision"
        assert detail_before["source_ref"] == str(captured["command_id"])
        assert await client.get_materialization(request_id) == {}

        context_summary = _run_control_plane_once(
            "sidera_control_plane.context_package_worker",
            config_values["database_url"],
        )
        planning_summary = _run_control_plane_once(
            "sidera_control_plane.planning_worker",
            config_values["database_url"],
        )
        assert int(context_summary["failed"]) == 0
        assert int(planning_summary["failed"]) == 0

        async with httpx.AsyncClient(trust_env=False) as web:
            detail_response = await web.get(
                f"{config_values['control_plane_url']}/api/v1/workspaces/"
                f"{workspace_id}/planning-requests/{request_id}"
            )
            diff_response = await web.get(
                f"{config_values['control_plane_url']}/api/v1/workspaces/"
                f"{workspace_id}/planning-requests/{request_id}/diff"
            )
            detail_response.raise_for_status()
            diff_response.raise_for_status()
            web_detail = detail_response.json()
            web_diff = diff_response.json()

        with tool_execution_context_scope(
            ToolExecutionContext(
                origin_channel="telegram",
                origin_chat_id="r6-1-akashic-gate",
                origin_session_key="telegram:r6-1-akashic-gate",
            )
        ):
            revision_read = _decode(
                await bridge.sidera_get_plan_revision(None, str(request_id))
            )
        assert revision_read["outcome"] == "needs_user"
        assert revision_read["resource_ref"] == f"revision:{request_id}"
        revision_data = revision_read["data"]
        assert web_detail["id"] == str(request_id)
        assert revision_data["root_task_id"] == str(root_task_id)
        assert revision_data["base"]["plan_snapshot_id"] == web_diff["base"]["plan_snapshot_id"]
        assert revision_data["base"]["plan_generation"] == web_diff["base"]["plan_generation"]
        assert revision_data["diff"]["diff_hash"] == web_diff["diff_hash"]
        assert revision_data["diff"]["proposal_content_hash"] == web_diff["proposal_content_hash"]
        assert web_detail["status"] in {
            "proposal_ready",
            "awaiting_confirmation",
            "needs_confirmation",
        }
        assert await client.get_materialization(request_id) == {}
    finally:
        await bridge.terminate()


def _decode(value: str) -> dict[str, Any]:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise TypeError("Akashic tool returned a non-object envelope")
    return cast(dict[str, Any], payload)
