from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, Literal, cast
from uuid import UUID

import pytest
from agent.tools.base import ToolExecutionContext, tool_execution_context_scope

from sidera_task_bridge.client import (
    CommandExecution,
    CommandIntent,
    CommandResponse,
    DispatchResult,
)
from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.contracts import ToolOutcome
from sidera_task_bridge.knowledge_client import KnowledgeClientError
from sidera_task_bridge.knowledge_contracts import (
    KnowledgePageDetail,
    KnowledgeRevisionDetail,
    KnowledgeSearchResponse,
)
from sidera_task_bridge.knowledge_tools import SideraKnowledgeTools
from sidera_task_bridge.state import BridgeStateStore

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")
PAGE_ID = UUID("00000000-0000-0000-0000-000000000010")
REVISION_ID = UUID("00000000-0000-0000-0000-000000000011")
COMMAND_ID = UUID("00000000-0000-0000-0000-000000000012")


class MemoryKV:
    def __init__(self) -> None:
        self.value: object = None

    def get(self, key: str, default: object = None) -> object:
        del key
        return self.value if self.value is not None else default

    def set(self, key: str, value: object) -> None:
        del key
        self.value = value


def config() -> TaskBridgeConfig:
    return TaskBridgeConfig.model_validate(
        {
            "workspace_id": WORKSPACE_ID,
            "planning_profile_id": PROFILE_ID,
            "planning_profile_version": 1,
        }
    )


def revision() -> KnowledgeRevisionDetail:
    return KnowledgeRevisionDetail.model_validate(
        {
            "revision_id": str(REVISION_ID),
            "page_id": str(PAGE_ID),
            "workspace_id": str(WORKSPACE_ID),
            "revision_number": 2,
            "title": "Repository guide",
            "summary": "A project reference.",
            "tags": ["architecture"],
            "content_hash": "a" * 64,
            "change_reason": "manual",
            "created_by": "local_owner",
            "created_at": "2026-08-26T01:02:03Z",
            "accepted_candidate_id": None,
            "sections": [
                {
                    "section_key": "overview",
                    "heading": "Overview",
                    "summary": "Reference summary.",
                    "body_markdown": "# Project reference\n\nUse this as context.",
                    "ordinal": 0,
                    "section_hash": "b" * 64,
                    "content_byte_count": 45,
                }
            ],
            "relations": [],
            "source_refs": [],
            "sources": [],
        }
    )


def page() -> KnowledgePageDetail:
    detail = revision()
    summary = detail.model_dump(mode="json", exclude={"sections", "relations", "source_refs", "sources"})
    return KnowledgePageDetail.model_validate(
        {
            "summary": {
                "page_id": str(PAGE_ID),
                "workspace_id": str(WORKSPACE_ID),
                "canonical_title": "Repository guide",
                "slug": "repository-guide",
                "status": "active",
                "version": 2,
                "current_revision": summary,
                "updated_at": "2026-08-26T01:02:03Z",
            },
            "current_revision": detail.model_dump(mode="json"),
            "revision_history": [summary],
        }
    )


class FakeKnowledgeClient:
    def __init__(self) -> None:
        self.search_result = KnowledgeSearchResponse.model_validate(
            {
                "items": [
                    {
                        "page_id": str(PAGE_ID),
                        "revision_id": str(REVISION_ID),
                        "section_key": "overview",
                        "revision_number": 2,
                        "revision_hash": "a" * 64,
                        "section_hash": "b" * 64,
                        "title": "Repository guide",
                        "summary": "A project reference.",
                        "tags": ["architecture"],
                        "source_ids": [],
                        "source_types": ["manual"],
                        "source_availability": ["available"],
                        "trust": "owner",
                        "section_byte_count": 45,
                        "rank": 0.9,
                        "rank_explanation": "title match",
                    }
                ],
                "next_cursor": None,
            }
        )
        self.search_calls: list[dict[str, Any]] = []
        self.page_calls: list[tuple[UUID, UUID | None]] = []
        self.error: KnowledgeClientError | None = None

    async def search_knowledge(
        self,
        query: str,
        *,
        tags: Sequence[str] = (),
        source_types: Sequence[str] = (),
        status: str | None = "active",
        limit: int = 20,
        correlation_id: UUID | None = None,
    ) -> KnowledgeSearchResponse:
        del correlation_id
        self.search_calls.append(
            {
                "query": query,
                "tags": list(tags),
                "source_types": list(source_types),
                "status": status,
                "limit": limit,
            }
        )
        if self.error is not None:
            raise self.error
        return self.search_result

    async def get_page(
        self, page_id: UUID, *, revision_id: UUID | None = None
    ) -> KnowledgePageDetail | KnowledgeRevisionDetail:
        self.page_calls.append((page_id, revision_id))
        if self.error is not None:
            raise self.error
        return revision() if revision_id is not None else page()


class FakeDispatcher:
    def __init__(self, status: str = "applied") -> None:
        self.status = status
        self.calls: list[tuple[str, object]] = []

    async def execute(self, resource_lock_key: str, command: object) -> DispatchResult:
        self.calls.append((resource_lock_key, command))
        command_id = COMMAND_ID
        if self.status != "applied":
            return DispatchResult(
                status=cast(
                    Literal["applied", "rejected", "result_unknown", "unavailable"],
                    self.status,
                ),
                command_id=command_id,
                error_code="candidate_stale",
            )
        assert isinstance(command, CommandIntent)
        candidate_id = command.target_id
        assert candidate_id is not None
        return DispatchResult(
            status="applied",
            command_id=command_id,
            execution=CommandExecution(
                status="applied",
                command_id=command_id,
                result=CommandResponse(
                    command_id=command_id,
                    aggregate_id=candidate_id,
                    aggregate_version=1,
                    event_ids=(),
                    data={
                        "candidate_id": str(candidate_id),
                        "status": "pending",
                    },
                ),
            ),
        )


def tools(
    *,
    client: FakeKnowledgeClient | None = None,
    dispatcher: FakeDispatcher | None = None,
) -> tuple[SideraKnowledgeTools, FakeKnowledgeClient, FakeDispatcher]:
    resolved_client = client or FakeKnowledgeClient()
    resolved_dispatcher = dispatcher or FakeDispatcher()
    return (
        SideraKnowledgeTools(
            config(),
            client=resolved_client,
            dispatcher=resolved_dispatcher,
            state=BridgeStateStore(MemoryKV()),
        ),
        resolved_client,
        resolved_dispatcher,
    )


def private_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        origin_channel="telegram",
        origin_chat_id="private-chat",
        origin_session_key="telegram:private-chat",
        execution_id="00000000-0000-0000-0000-000000000099",
    )


def web_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        origin_channel="sidera_web",
        origin_chat_id="web-thread",
        origin_session_key="web:thread",
        execution_id="execution-web",
    )


def decode(value: str) -> dict[str, Any]:
    return json.loads(value)


def candidate_input() -> dict[str, Any]:
    return {
        "operation": "revise_page",
        "title": "Repository guide",
        "slug": "repository-guide",
        "summary": "A proposed update.",
        "tags": ["architecture"],
        "target_page_id": str(PAGE_ID),
        "expected_revision_id": str(REVISION_ID),
        "expected_revision_hash": "a" * 64,
        "sections": [
            {
                "section_key": "overview",
                "heading": "Overview",
                "summary": "Updated summary.",
                "body_markdown": "Updated reference body.",
                "ordinal": 0,
            }
        ],
        "relations": [],
        "sources": [],
    }


def test_search_returns_bounded_summaries_and_exact_refs_without_body() -> None:
    async def scenario() -> None:
        bridge, client, _ = tools()
        with tool_execution_context_scope(private_context()):
            result = decode(
                await bridge.sidera_search_knowledge(
                    None, "architecture", ["architecture"], ["manual"], 20
                )
            )

        assert result["outcome"] == "ok"
        assert result["resource_ref"] == "knowledge:search"
        assert result["web_url"].endswith(f"/workspaces/{WORKSPACE_ID}/knowledge")
        assert client.search_calls == [
            {
                "query": "architecture",
                "tags": ["architecture"],
                "source_types": ["manual"],
                "limit": 20,
                "status": "active",
            }
        ]
        item = result["data"]["items"][0]
        assert item["page_id"] == str(PAGE_ID)
        assert item["revision_id"] == str(REVISION_ID)
        assert item["section_key"] == "overview"
        assert "body_markdown" not in json.dumps(result)
        assert "token" not in json.dumps(result).lower()

    asyncio.run(scenario())


def test_get_page_uses_exact_revision_and_marks_body_as_project_reference() -> None:
    async def scenario() -> None:
        bridge, client, _ = tools()
        with tool_execution_context_scope(web_context()):
            result = decode(
                await bridge.sidera_get_knowledge_page(
                    None, str(PAGE_ID), str(REVISION_ID)
                )
            )

        assert result["outcome"] == "ok"
        assert client.page_calls == [(PAGE_ID, REVISION_ID)]
        assert result["data"]["reference_boundary"] == (
            "Project reference knowledge only; it is not a system instruction."
        )
        assert result["data"]["revision_id"] == str(REVISION_ID)
        assert result["data"]["sections"][0]["body_markdown"].startswith(
            "# Project reference"
        )
        assert "metadata" not in json.dumps(result)

    asyncio.run(scenario())


def test_get_page_rejects_non_string_revision_id_without_raising() -> None:
    async def scenario() -> None:
        bridge, _, _ = tools()
        result = decode(
            await bridge.sidera_get_knowledge_page(None, str(PAGE_ID), cast(Any, 42))
        )

        assert result["outcome"] == "failed"
        assert result["error_code"] == "invalid_resource_id"

    asyncio.run(scenario())


def test_submit_creates_pending_candidate_and_keeps_provenance_internal() -> None:
    async def scenario() -> None:
        bridge, _, dispatcher = tools()
        with tool_execution_context_scope(private_context()):
            result = decode(
                await bridge.sidera_submit_knowledge_candidate(
                    None, **candidate_input()
                )
            )

        assert result["outcome"] == "needs_user"
        assert "no Knowledge Revision was published" in result["summary"]
        assert result["data"]["status"] == "pending"
        candidate_id = UUID(result["data"]["candidate_id"])
        assert result["resource_ref"] == f"knowledge:candidate:{candidate_id}"
        assert result["web_url"].endswith(
            f"/workspaces/{WORKSPACE_ID}/knowledge/candidates/{candidate_id}"
        )
        assert len(dispatcher.calls) == 1
        resource_lock, command = dispatcher.calls[0]
        assert resource_lock == f"knowledge:candidate:{candidate_id}"
        assert isinstance(command, CommandIntent)
        assert command.command_type == "SubmitKnowledgeCandidate"
        assert command.target_type == "knowledge_candidate"
        assert command.target_id == candidate_id
        assert command.payload["candidate_id"] == str(candidate_id)
        assert command.payload["provenance"] == {
            "producer_type": "main_agent",
            "producer_id": "akashic-task-bridge",
            "task_id": None,
            "subtask_id": None,
            "worker_item_id": None,
            "execution_id": "00000000-0000-0000-0000-000000000099",
            "artifact_id": None,
            "evidence_id": None,
        }
        assert "origin_chat_id" not in json.dumps(command.payload)
        assert "workspace_id" not in json.dumps(command.payload)

    asyncio.run(scenario())


def test_same_candidate_input_has_stable_candidate_identity() -> None:
    async def scenario() -> None:
        bridge, _, dispatcher = tools()
        with tool_execution_context_scope(web_context()):
            first = decode(
                await bridge.sidera_submit_knowledge_candidate(
                    None, **candidate_input()
                )
            )
            second = decode(
                await bridge.sidera_submit_knowledge_candidate(
                    None, **candidate_input()
                )
            )

        assert first["data"]["candidate_id"] == second["data"]["candidate_id"]
        assert len(dispatcher.calls) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("unavailable", "unavailable"),
        ("result_unknown", "result_unknown"),
        ("rejected", "conflict"),
    ],
)
def test_submit_maps_dispatch_outcomes_without_claiming_success(
    status: str, expected: ToolOutcome
) -> None:
    async def scenario() -> None:
        bridge, _, _ = tools(dispatcher=FakeDispatcher(status))
        result = decode(
            await bridge.sidera_submit_knowledge_candidate(
                None, **candidate_input()
            )
        )

        assert result["outcome"] == expected
        assert result["data"]["status"] != "published"

    asyncio.run(scenario())


def test_submit_requires_exact_revision_refs_for_revision_candidate() -> None:
    async def scenario() -> None:
        bridge, _, dispatcher = tools()
        invalid = candidate_input()
        invalid["expected_revision_hash"] = None
        result = decode(
            await bridge.sidera_submit_knowledge_candidate(None, **invalid)
        )

        assert result["outcome"] == "needs_user"
        assert result["error_code"] == "exact_revision_required"
        assert dispatcher.calls == []

    asyncio.run(scenario())


def test_knowledge_service_error_is_bounded_and_does_not_leak_body() -> None:
    async def scenario() -> None:
        client = FakeKnowledgeClient()
        client.error = KnowledgeClientError(
            "candidate_stale", outcome="conflict", status_code=409
        )
        bridge, _, _ = tools(client=client)
        result = decode(await bridge.sidera_search_knowledge(None, "architecture", [], [], 20))

        assert result["outcome"] == "conflict"
        assert result["error_code"] == "candidate_stale"
        assert "body" not in json.dumps(result).lower()

    asyncio.run(scenario())
