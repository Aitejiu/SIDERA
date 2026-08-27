from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

import httpx
import pytest

REQUIRED_ENV = (
    "SIDERA_MAIN_AGENT_GATEWAY_URL",
    "SIDERA_CONTROL_PLANE_URL",
    "SIDERA_WORKSPACE_ID",
    "SIDERA_PLANNING_PROFILE_ID",
    "SIDERA_PLANNING_PROFILE_VERSION",
    "SIDERA_REAL_MAIN_AGENT_PROVIDER",
)

pytestmark = pytest.mark.integration


def _require_real_environment() -> dict[str, str]:
    if os.environ.get("SIDERA_RUN_REAL_MAIN_AGENT") != "1":
        pytest.skip("SIDERA_RUN_REAL_MAIN_AGENT is not enabled")
    missing = [key for key in REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        pytest.fail(f"real Main Agent gate configuration is missing: {', '.join(missing)}")
    return {key: os.environ[key] for key in REQUIRED_ENV}


def _json(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise TypeError("Gateway SSE data must be an object")
    return parsed


async def _turn_events(
    client: httpx.AsyncClient,
    gateway_url: str,
    thread_id: str,
    turn_id: str,
    *,
    timeout_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    terminal: dict[str, Any] = {}
    deadline = time.monotonic() + timeout_seconds
    try:
        async with asyncio.timeout(timeout_seconds):
            async with client.stream(
                "GET",
                f"{gateway_url}/main-agent/v1/threads/{thread_id}/turns/{turn_id}/events",
                timeout=httpx.Timeout(10.0, read=None),
            ) as response:
                response.raise_for_status()
                event_name = "message"
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if time.monotonic() > deadline:
                        raise AssertionError("real turn stream timed out")
                    if line.startswith("event: "):
                        event_name = line.removeprefix("event: ")
                    elif line.startswith("data: "):
                        data_lines.append(line.removeprefix("data: "))
                    elif line == "" and data_lines:
                        payload = _json("\n".join(data_lines))
                        payload["_event"] = event_name
                        events.append(payload)
                        data_lines = []
                        if event_name in {"completed", "failed", "interrupted", "cancelled"}:
                            terminal = payload
                            break
    except TimeoutError as exc:
        raise AssertionError("real turn stream timed out") from exc
    if not terminal:
        raise AssertionError("real turn stream ended without a terminal event")
    return events, terminal


def _tool_names(events: Sequence[Mapping[str, Any]]) -> set[str]:
    names: set[str] = set()
    for event in events:
        item = event.get("item")
        if not isinstance(item, Mapping):
            continue
        data = item.get("data")
        if not isinstance(data, Mapping):
            continue
        for key in ("name", "tool_name", "toolName"):
            name = data.get(key)
            if isinstance(name, str):
                names.add(name)
    return names


def _event_text(events: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(events, ensure_ascii=False, separators=(",", ":"))


_PLANNING_RESOURCE_RE = re.compile(
    r"planning:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


def _planning_request_id_from_events(
    events: Sequence[Mapping[str, Any]],
) -> str:
    match = _PLANNING_RESOURCE_RE.search(_event_text(events))
    if match is None:
        raise AssertionError(
            "sidera_submit_brief did not return a planning resource reference"
        )
    return match.group(1)


async def _start_turn(
    client: httpx.AsyncClient,
    gateway_url: str,
    thread_id: str,
    input_text: str,
    *,
    timeout_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    response = await client.post(
        f"{gateway_url}/main-agent/v1/threads/{thread_id}/turns",
        json={"input": input_text},
        timeout=20.0,
    )
    response.raise_for_status()
    turn = response.json()
    if not isinstance(turn, Mapping) or not isinstance(turn.get("id"), str):
        raise TypeError("Gateway did not return a structured turn")
    return await _turn_events(
        client,
        gateway_url,
        thread_id,
        str(turn["id"]),
        timeout_seconds=timeout_seconds,
    )


async def _read_planning_detail(
    client: httpx.AsyncClient,
    control_plane_url: str,
    workspace_id: str,
    request_id: str,
) -> dict[str, Any]:
    response = await client.get(
        f"{control_plane_url}/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}",
        timeout=20.0,
    )
    response.raise_for_status()
    detail = response.json()
    if not isinstance(detail, dict):
        raise TypeError("Planning detail is not an object")
    return detail


async def _wait_for_status(
    client: httpx.AsyncClient,
    control_plane_url: str,
    workspace_id: str,
    request_id: str,
    statuses: set[str],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        detail = await _read_planning_detail(
            client, control_plane_url, workspace_id, request_id
        )
        if detail.get("status") in statuses:
            return detail
        if detail.get("status") in {"failed", "blocked", "cancelled", "rejected"}:
            raise AssertionError(
                f"PlanningRequest reached unexpected terminal status: {detail.get('status')}"
            )
        await asyncio.sleep(2.0)
    raise AssertionError(f"PlanningRequest did not reach {sorted(statuses)}")


async def test_real_main_agent_web_to_task_vertical_loop() -> None:
    config = _require_real_environment()
    gateway_url = config["SIDERA_MAIN_AGENT_GATEWAY_URL"].rstrip("/")
    control_plane_url = config["SIDERA_CONTROL_PLANE_URL"].rstrip("/")
    workspace_id = config["SIDERA_WORKSPACE_ID"]
    marker = f"SIDERA-R5-2-{uuid4().hex[:12]}"
    timeout_seconds = float(os.environ.get("SIDERA_REAL_MAIN_AGENT_TIMEOUT_SECONDS", "300"))

    async with httpx.AsyncClient(trust_env=False) as client:
        health = await client.get(f"{gateway_url}/main-agent/v1/health", timeout=20.0)
        health.raise_for_status()
        assert health.json().get("provider") == "available"

        created = await client.post(
            f"{gateway_url}/main-agent/v1/threads",
            json={"title": marker, "context_refs": [f"release:{marker}"]},
            timeout=20.0,
        )
        created.raise_for_status()
        thread = created.json()
        thread_id = thread.get("id")
        assert isinstance(thread_id, str)
        assert "akashic" not in created.text.lower()

        initial_events, initial_terminal = await _start_turn(
            client,
            gateway_url,
            thread_id,
            (
                f"请创建一个真实的 SIDERA 任务，唯一标记是 {marker}。目标是在当前工作区新增 "
                "scripts/sidera-r5-release-check.sh：当环境变量 SIDERA_R5_RELEASE=1 时输出 "
                "SIDERA_R5_RELEASE_OK，否则以非零状态退出；同时提供一个 shell 测试和回滚说明。"
                "约束是只使用当前工作区和一个最小 WorkerItem，不引入额外依赖或外部服务；"
                "验收标准是脚本行为、测试和回滚步骤都明确；"
                "输出要求是先形成 Planning Proposal，获得确认后再物化 Task。信息已经足够，"
                "不要提出澄清问题，直接生成 Proposal 并等待确认。"
            ),
            timeout_seconds=timeout_seconds,
        )
        assert initial_terminal.get("_event") == "completed"
        assert "sidera_submit_brief" in _tool_names(initial_events)
        request_id = _planning_request_id_from_events(initial_events)
        detail = await _read_planning_detail(
            client, control_plane_url, workspace_id, request_id
        )
        assert detail.get("source_type") == "sidera_web_chat"
        assert detail.get("planning_profile_id") == config["SIDERA_PLANNING_PROFILE_ID"]

        if detail.get("status") in {"needs_clarification", "clarification_required"}:
            await _start_turn(
                client,
                gateway_url,
                thread_id,
                "请采用最小风险默认值回答所有阻塞澄清，并继续生成方案。",
                timeout_seconds=timeout_seconds,
            )
        proposal = await _wait_for_status(
            client,
            control_plane_url,
            workspace_id,
            request_id,
            {"proposal_ready", "awaiting_confirmation", "needs_confirmation", "materialized"},
            timeout_seconds=timeout_seconds,
        )
        assert isinstance(proposal.get("version"), int)
        assert isinstance(proposal.get("current_proposal_revision"), int) or proposal.get("status") == "materialized"

        if proposal.get("status") != "materialized":
            await _start_turn(
                client,
                gateway_url,
                thread_id,
                "请先做一个保持目标不变的小幅方案修订，补充测试与回滚验收，然后重新生成最新 Proposal。",
                timeout_seconds=timeout_seconds,
            )
            revised = await _wait_for_status(
                client,
                control_plane_url,
                workspace_id,
                request_id,
                {"proposal_ready", "awaiting_confirmation", "needs_confirmation", "materialized"},
                timeout_seconds=timeout_seconds,
            )
            assert revised.get("version", 0) >= proposal.get("version", 0)
            await _start_turn(
                client,
                gateway_url,
                thread_id,
                "我确认当前最新版本 Proposal，请按最新版本执行并物化 Task。",
                timeout_seconds=timeout_seconds,
            )

        materialized = await _wait_for_status(
            client,
            control_plane_url,
            workspace_id,
            request_id,
            {"materialized"},
            timeout_seconds=timeout_seconds,
        )
        materialization = await client.get(
            f"{control_plane_url}/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}/materialization",
            timeout=20.0,
        )
        materialization.raise_for_status()
        materialization_body = materialization.json()
        root_task_id = materialization_body.get("root_task_id")
        assert isinstance(root_task_id, str)
        assert materialized.get("status") == "materialized"

        overview = await client.get(
            f"{control_plane_url}/api/v1/tasks/{root_task_id}/overview",
            params={"workspace_id": workspace_id},
            timeout=20.0,
        )
        overview.raise_for_status()
        overview_body = overview.json()
        assert overview_body.get("root_task_id") == root_task_id
        assert overview_body.get("subtask_count", 0) >= 1

        graph = await client.get(
            f"{control_plane_url}/api/v1/tasks/{root_task_id}/graph",
            params={"workspace_id": workspace_id},
            timeout=20.0,
        )
        graph.raise_for_status()
        graph_body = graph.json()
        assert isinstance(graph_body, Mapping)
        assert graph_body.get("root_task_id", root_task_id) == root_task_id

        status_events, status_terminal = await _start_turn(
            client,
            gateway_url,
            thread_id,
            "请读取刚刚创建的任务当前状态，并返回任务 ID 和下一步。",
            timeout_seconds=timeout_seconds,
        )
        assert status_terminal.get("_event") == "completed"
        assert "sidera_get_task_status" in _tool_names(status_events)
        assert root_task_id in _event_text(status_events)


async def test_real_main_agent_stale_confirm_and_control_plane_offline_contract() -> None:
    config = _require_real_environment()
    helper = os.environ.get("SIDERA_REAL_GATE_HELPER")
    if not helper:
        pytest.fail("SIDERA_REAL_GATE_HELPER is required when the real gate is enabled")
    helper_path = os.path.realpath(helper)
    if not os.path.isfile(helper_path) or not os.access(helper_path, os.X_OK):
        pytest.fail("SIDERA_REAL_GATE_HELPER must be an executable file")

    gateway_url = config["SIDERA_MAIN_AGENT_GATEWAY_URL"].rstrip("/")
    control_plane_url = config["SIDERA_CONTROL_PLANE_URL"].rstrip("/")
    workspace_id = config["SIDERA_WORKSPACE_ID"]
    marker = f"SIDERA-R5-2-CONFLICT-{uuid4().hex[:12]}"
    timeout_seconds = float(os.environ.get("SIDERA_REAL_MAIN_AGENT_TIMEOUT_SECONDS", "300"))

    def run_helper(action: str) -> None:
        subprocess.run(
            [helper_path, action],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

    async with httpx.AsyncClient(trust_env=False) as client:
        created = await client.post(
            f"{gateway_url}/main-agent/v1/threads",
            json={"title": marker, "context_refs": [f"release:{marker}"]},
            timeout=20.0,
        )
        created.raise_for_status()
        thread_id = created.json().get("id")
        assert isinstance(thread_id, str)
        initial_events, initial_terminal = await _start_turn(
            client,
            gateway_url,
            thread_id,
            (
                f"请为 {marker} 创建一个最小可执行 SIDERA 任务。目标是在当前工作区新增 "
                "scripts/sidera-r5-conflict-check.sh：当环境变量 SIDERA_R5_RELEASE=1 时输出 "
                "SIDERA_R5_RELEASE_OK，否则以非零状态退出，并附带一个 shell 测试。"
                "约束是单个 WorkerItem、不引入额外依赖或外部服务，验收标准是测试、结果和回滚说明。"
                "信息已经足够，"
                "不要提出澄清问题，直接生成 Proposal 并等待确认。"
            ),
            timeout_seconds=timeout_seconds,
        )
        assert initial_terminal.get("_event") == "completed"
        assert "sidera_submit_brief" in _tool_names(initial_events)
        request_id = _planning_request_id_from_events(initial_events)
        proposal = await _read_planning_detail(
            client, control_plane_url, workspace_id, request_id
        )
        proposal = await _wait_for_status(
            client,
            control_plane_url,
            workspace_id,
            request_id,
            {"proposal_ready", "awaiting_confirmation", "needs_confirmation", "materialized"},
            timeout_seconds=timeout_seconds,
        )
        stale_request_version = int(proposal["version"])
        stale_proposal_revision = int(proposal["current_proposal_revision"])
        stale_proposal_hash = str(proposal["current_proposal_content_hash"])

        await _start_turn(
            client,
            gateway_url,
            thread_id,
            "请修改上一版方案，增加一个回滚验收步骤，并重新生成一个新版本 Proposal。"
            "信息已经足够，不要提出澄清问题，直接生成新版本并等待确认。",
            timeout_seconds=timeout_seconds,
        )
        revised = await _wait_for_status(
            client,
            control_plane_url,
            workspace_id,
            request_id,
            {"proposal_ready", "awaiting_confirmation", "needs_confirmation", "materialized"},
            timeout_seconds=timeout_seconds,
        )
        assert revised.get("version", 0) >= proposal.get("version", 0)
        stale_events, _ = await _start_turn(
            client,
            gateway_url,
            thread_id,
            (
                "这是一个故意验证 SIDERA 乐观并发冲突的测试。工具调用本身是测试动作，"
                "控制面应拒绝过期参数并返回 conflict；请立即调用 sidera_confirm_proposal 工具，"
                "不要先调用状态查询工具，不要自行拒绝、询问或改写参数。"
                f"对 planning_request_id={request_id} 使用旧的 request_version="
                f"{stale_request_version}、proposal_revision={stale_proposal_revision}、"
                f"proposal_content_hash={stale_proposal_hash}，"
                "explicit_user_confirmation=true。这个旧版本已经过期，"
                "不要确认刚刚生成的最新版本。"
            ),
            timeout_seconds=timeout_seconds,
        )
        stale_text = _event_text(stale_events).lower()
        assert "sidera_confirm_proposal" in _tool_names(stale_events)
        assert any(token in stale_text for token in ("conflict", "version", "stale"))

        await _start_turn(
            client,
            gateway_url,
            thread_id,
            "请确认当前最新版本 Proposal 并物化任务。",
            timeout_seconds=timeout_seconds,
        )
        materialized = await _wait_for_status(
            client,
            control_plane_url,
            workspace_id,
            request_id,
            {"materialized"},
            timeout_seconds=timeout_seconds,
        )
        materialization = await client.get(
            f"{control_plane_url}/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}/materialization",
            timeout=20.0,
        )
        materialization.raise_for_status()
        root_task_id = materialization.json().get("root_task_id")
        assert isinstance(root_task_id, str)
        assert materialized.get("status") == "materialized"

        before_offline, before_terminal = await _start_turn(
            client,
            gateway_url,
            thread_id,
            "只回答一句普通问候，不要调用任何 SIDERA 任务工具。",
            timeout_seconds=timeout_seconds,
        )
        assert before_terminal.get("_event") == "completed"
        assert not {"sidera_get_task_status", "sidera_get_planning_status"}.intersection(
            _tool_names(before_offline)
        )

        run_helper("pause-control-plane")
        try:
            offline_chat, offline_chat_terminal = await _start_turn(
                client,
                gateway_url,
                thread_id,
                "只回答一句普通问候，不要调用任何 SIDERA 任务工具。",
                timeout_seconds=timeout_seconds,
            )
            assert offline_chat_terminal.get("_event") == "completed"
            assert not {"sidera_get_task_status", "sidera_get_planning_status"}.intersection(
                _tool_names(offline_chat)
            )
            offline_task, _ = await _start_turn(
                client,
                gateway_url,
                thread_id,
                f"请查询任务 {root_task_id} 的状态。",
                timeout_seconds=timeout_seconds,
            )
            offline_text = _event_text(offline_task).lower()
            assert "sidera_get_task_status" in _tool_names(offline_task)
            assert "unavailable" in offline_text
        finally:
            run_helper("resume-control-plane")
