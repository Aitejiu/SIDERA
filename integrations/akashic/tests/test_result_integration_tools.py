from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

from test_tools import FakeClient, decode, tools

from sidera_task_bridge.notifications import _result_fact

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
TASK_ID = UUID("00000000-0000-0000-0000-000000000020")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000021")


class ResultFakeClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.results: dict[str, Any] = {}
        self.calls: list[UUID] = []

    async def get_task_results(self, task_id: UUID) -> dict[str, Any]:
        self.calls.append(task_id)
        return self.results


def result_view() -> dict[str, Any]:
    return {
        "workspace_id": str(WORKSPACE_ID),
        "root_task_id": str(TASK_ID),
        "task_title": "结果摘要",
        "task_status": "running",
        "task_version": 3,
        "result_version": 4,
        "result_stage": "awaiting_acceptance",
        "current_candidate": {
            "candidate_id": str(CANDIDATE_ID),
            "candidate_hash": "a" * 64,
            "runtime_payload": {"provider_key": "must-not-leak"},
        },
        "criteria": [],
        "blocking_reasons": [],
        "available_actions": [
            {
                "action": "accept_result",
                "command_type": "CompleteTask",
                "target_id": str(TASK_ID),
                "expected_version": 3,
                "expected_result_version": 4,
            }
        ],
        "acceptance": None,
        "active_operation": None,
        "candidate_history": {"items": [], "next_cursor": None},
        "last_change_cursor": 42,
        "as_of": "2026-09-08T00:00:00Z",
    }


def test_task_results_tool_is_read_only_bounded_and_workspace_scoped() -> None:
    async def scenario() -> None:
        client = ResultFakeClient()
        client.results = result_view()
        bridge, _, _ = tools(client=client)

        result = decode(await bridge.sidera_get_task_results(None, str(TASK_ID)))

        assert result["outcome"] == "ok"
        assert result["resource_ref"] == f"task:{TASK_ID}:results"
        assert result["web_url"].endswith(
            f"/tasks/{TASK_ID}?view=results&candidate={CANDIDATE_ID}"
        )
        assert client.calls == [TASK_ID]
        assert result["data"]["root_task_id"] == str(TASK_ID)
        assert result["data"]["result_stage"] == "awaiting_acceptance"
        encoded = json.dumps(result)
        assert "actor" not in encoded
        assert "runtime_payload" not in encoded
        assert "must-not-leak" not in encoded
        assert "sidera_accept_task" not in encoded

    asyncio.run(scenario())


def test_task_results_tool_rejects_invalid_id_without_client_call() -> None:
    async def scenario() -> None:
        client = ResultFakeClient()
        bridge, _, _ = tools(client=client)

        result = decode(await bridge.sidera_get_task_results(None, "not-a-uuid"))

        assert result["outcome"] == "failed"
        assert result["error_code"] == "invalid_resource_id"
        assert client.calls == []

    asyncio.run(scenario())


def test_result_fact_distinguishes_review_attention_and_post_acceptance() -> None:
    ready = _result_fact(
        {
            **result_view(),
            "task_title": "发布任务",
            "result_stage": "awaiting_acceptance",
        },
        task_id=TASK_ID,
        version=4,
        web_url="/tasks/results",
    )
    assert ready.kind == "result_candidate_ready"
    assert ready.snapshot.status == "result_candidate_ready"
    assert "逐条审阅" in (ready.fact_text or "")

    attention = _result_fact(
        {
            **result_view(),
            "result_stage": "attention",
            "blocking_reasons": ["verification_failed"],
        },
        task_id=TASK_ID,
        version=5,
        web_url="/tasks/results",
    )
    assert attention.kind == "result_attention"
    assert attention.subtitle == "verification_failed"

    accepted = _result_fact(
        {
            **result_view(),
            "result_stage": "finalizing",
            "acceptance": {"decision": "accepted"},
        },
        task_id=TASK_ID,
        version=6,
        web_url="/tasks/results",
    )
    assert accepted.kind == "result_acceptance_recorded"
    assert accepted.snapshot.status == "result_acceptance_recorded"
    assert "仍等待真实收尾结果" in (accepted.fact_text or "")
    assert accepted.kind != "task_completed"
