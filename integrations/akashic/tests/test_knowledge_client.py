from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import httpx
import pytest

from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.knowledge_client import (
    KnowledgeClient,
    KnowledgeClientError,
    KnowledgeCommandResultUnknown,
)
from sidera_task_bridge.knowledge_contracts import (
    KnowledgePageDetail,
    KnowledgeRevisionDetail,
)

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")
PAGE_ID = UUID("00000000-0000-0000-0000-000000000010")
REVISION_ID = UUID("00000000-0000-0000-0000-000000000011")
COMMAND_ID = UUID("00000000-0000-0000-0000-000000000012")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000013")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000014")
TOKEN = "knowledge-main-agent-token"


@pytest.fixture(autouse=True)
def knowledge_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN", TOKEN)


def make_config(**overrides: Any) -> TaskBridgeConfig:
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "planning_profile_id": PROFILE_ID,
        "planning_profile_version": 1,
    }
    values.update(overrides)
    return TaskBridgeConfig.model_validate(values)


def revision_summary() -> dict[str, Any]:
    return {
        "revision_id": str(REVISION_ID),
        "page_id": str(PAGE_ID),
        "workspace_id": str(WORKSPACE_ID),
        "revision_number": 1,
        "title": "Repository guide",
        "summary": "A bounded project reference.",
        "tags": ["architecture"],
        "content_hash": "a" * 64,
        "change_reason": "manual",
        "created_by": "local_owner",
        "created_at": "2026-08-26T01:02:03Z",
        "accepted_candidate_id": None,
    }


def revision_detail() -> dict[str, Any]:
    return {
        **revision_summary(),
        "sections": [
            {
                "section_key": "overview",
                "heading": "Overview",
                "summary": "Safe reference text.",
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


def page_detail() -> dict[str, Any]:
    return {
        "summary": {
            "page_id": str(PAGE_ID),
            "workspace_id": str(WORKSPACE_ID),
            "canonical_title": "Repository guide",
            "slug": "repository-guide",
            "status": "active",
            "version": 1,
            "current_revision": revision_summary(),
            "updated_at": "2026-08-26T01:02:03Z",
        },
        "current_revision": revision_detail(),
        "revision_history": [revision_summary()],
    }


def search_response() -> dict[str, Any]:
    return {
        "items": [
            {
                "page_id": str(PAGE_ID),
                "revision_id": str(REVISION_ID),
                "section_key": "overview",
                "revision_number": 1,
                "revision_hash": "a" * 64,
                "section_hash": "b" * 64,
                "title": "Repository guide",
                "summary": "A bounded project reference.",
                "tags": ["architecture"],
                "source_ids": [],
                "source_types": ["manual"],
                "source_availability": ["available"],
                "trust": "owner",
                "section_byte_count": 45,
                "rank": 0.9,
                "rank_explanation": "title and section match",
            }
        ],
        "next_cursor": None,
    }


def candidate_payload() -> dict[str, Any]:
    return {
        "candidate_schema_version": 1,
        "candidate_id": str(CANDIDATE_ID),
        "operation": "create_page",
        "target_page_id": None,
        "expected_revision_id": None,
        "expected_revision_hash": None,
        "title": "A proposed page",
        "slug": "a-proposed-page",
        "summary": "A proposed knowledge page.",
        "tags": ["proposal"],
        "sections": [
            {
                "section_key": "proposal",
                "heading": "Proposal",
                "summary": "A candidate section.",
                "body_markdown": "Candidate body.",
                "ordinal": 0,
            }
        ],
        "relations": [],
        "sources": [],
        "provenance": {},
        "supersedes_candidate_id": None,
    }


def command_status(status: str) -> dict[str, Any]:
    return {
        "command_id": str(COMMAND_ID),
        "workspace_id": str(WORKSPACE_ID),
        "status": status,
        "result": (
            {"candidate_id": str(CANDIDATE_ID), "status": "pending"}
            if status == "applied"
            else None
        ),
        "error_code": "candidate_stale" if status == "rejected" else None,
    }


def test_search_sends_workspace_bearer_and_correlation_and_parses_strict_hit() -> None:
    async def scenario() -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["authorization"] = request.headers.get("Authorization")
            seen["correlation"] = request.headers.get("X-Correlation-ID")
            return httpx.Response(200, json=search_response())

        client = KnowledgeClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        result = await client.search_knowledge(
            "architecture",
            tags=("architecture",),
            source_types=("manual",),
            limit=20,
            correlation_id=CORRELATION_ID,
        )
        await client.close()

        assert (
            seen["url"]
            == f"http://127.0.0.1:8020/api/v1/workspaces/{WORKSPACE_ID}/knowledge/search"
            "?q=architecture&tags=architecture&source_types=manual&status=active&limit=20"
        )
        assert seen["authorization"] == f"Bearer {TOKEN}"
        assert seen["correlation"] == str(CORRELATION_ID)
        assert result.items[0].page_id == PAGE_ID
        assert result.items[0].summary == "A bounded project reference."

    asyncio.run(scenario())


def test_get_page_reads_current_or_exact_revision_without_latest_guessing() -> None:
    async def scenario() -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if "/revisions/" in request.url.path:
                return httpx.Response(200, json=revision_detail())
            return httpx.Response(200, json=page_detail())

        client = KnowledgeClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        current = await client.get_page(PAGE_ID)
        revision = await client.get_page(PAGE_ID, revision_id=REVISION_ID)
        await client.close()

        assert isinstance(current, KnowledgePageDetail)
        assert current.current_revision is not None
        assert current.current_revision.revision_id == REVISION_ID
        assert isinstance(revision, KnowledgeRevisionDetail)
        assert revision.revision_id == REVISION_ID
        assert paths == [
            f"/api/v1/workspaces/{WORKSPACE_ID}/knowledge/pages/{PAGE_ID}",
            f"/api/v1/workspaces/{WORKSPACE_ID}/knowledge/pages/{PAGE_ID}/revisions/{REVISION_ID}",
        ]

    asyncio.run(scenario())


def test_get_page_rejects_response_over_limit_without_returning_body() -> None:
    async def scenario() -> None:
        secret_body_marker = "private knowledge body marker"

        def handler(_request: httpx.Request) -> httpx.Response:
            content = (secret_body_marker + " ") * 40_000
            return httpx.Response(200, content=content.encode())

        client = KnowledgeClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        with pytest.raises(KnowledgeClientError) as error:
            await client.get_page(PAGE_ID)
        await client.close()

        assert error.value.code == "content_limit_exceeded"
        assert error.value.outcome == "failed"
        assert secret_body_marker not in str(error.value)

    asyncio.run(scenario())


def test_submit_candidate_builds_main_agent_command_envelope() -> None:
    async def scenario() -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = request.url.path
            seen["headers"] = dict(request.headers)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "status": "applied",
                    "command_id": str(COMMAND_ID),
                    "result": {
                        "candidate_id": str(CANDIDATE_ID),
                        "status": "pending",
                    },
                },
            )

        client = KnowledgeClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        result = await client.submit_candidate(
            COMMAND_ID,
            candidate_payload(),
            idempotency_key="sidera:SubmitKnowledgeCandidate:000000000012",
            correlation_id=CORRELATION_ID,
        )
        await client.close()

        body = seen["body"]
        assert seen["url"] == "/api/v1/commands"
        assert seen["headers"]["authorization"] == f"Bearer {TOKEN}"
        assert seen["headers"]["x-correlation-id"] == str(CORRELATION_ID)
        assert body["workspace_id"] == str(WORKSPACE_ID)
        assert body["actor"] == {"type": "main_agent", "id": "akashic-task-bridge"}
        assert body["command_type"] == "SubmitKnowledgeCandidate"
        assert body["target_type"] == "knowledge_candidate"
        assert body["target_id"] == str(CANDIDATE_ID)
        assert body["command_id"] == str(COMMAND_ID)
        assert body["idempotency_key"].startswith("sidera:")
        assert body["correlation_id"] == str(CORRELATION_ID)
        assert body["payload"] == candidate_payload()
        assert result.status == "applied"
        assert result.result is not None
        assert result.result.candidate_id == CANDIDATE_ID

    asyncio.run(scenario())


def test_command_status_preserves_pending_applied_and_rejected_states() -> None:
    async def scenario() -> None:
        statuses = iter(("pending", "applied", "rejected"))

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith(str(COMMAND_ID))
            return httpx.Response(200, json=command_status(next(statuses)))

        client = KnowledgeClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        pending = await client.get_command_status(COMMAND_ID)
        applied = await client.get_command_status(COMMAND_ID)
        rejected = await client.get_command_status(COMMAND_ID)
        await client.close()

        assert pending.status == "pending"
        assert applied.status == "applied"
        assert rejected.status == "rejected"
        assert rejected.error_code == "candidate_stale"

    asyncio.run(scenario())


def test_client_maps_conflict_unavailable_and_unknown_write_without_raw_details() -> None:
    async def scenario() -> None:
        secret_detail = "private server detail and token"

        def conflict_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "candidate_stale",
                        "message": secret_detail,
                        "details": {"current_revision": 4},
                    }
                },
            )

        conflict_client = KnowledgeClient(
            make_config(), transport=httpx.MockTransport(conflict_handler)
        )
        with pytest.raises(KnowledgeClientError) as conflict:
            await conflict_client.search_knowledge("architecture")
        await conflict_client.close()
        assert conflict.value.outcome == "conflict"
        assert conflict.value.code == "candidate_stale"
        assert secret_detail not in str(conflict.value)
        assert TOKEN not in str(conflict.value)

        def unavailable_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="raw unavailable body")

        unavailable_client = KnowledgeClient(
            make_config(), transport=httpx.MockTransport(unavailable_handler)
        )
        with pytest.raises(KnowledgeClientError) as unavailable:
            await unavailable_client.search_knowledge("architecture")
        await unavailable_client.close()
        assert unavailable.value.outcome == "unavailable"
        assert unavailable.value.code == "temporarily_unavailable"
        assert "raw unavailable body" not in str(unavailable.value)

        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("wire secret", request=request)

        write_client = KnowledgeClient(
            make_config(), transport=httpx.MockTransport(timeout_handler)
        )
        with pytest.raises(KnowledgeCommandResultUnknown) as unknown:
            await write_client.submit_candidate(
                COMMAND_ID,
                candidate_payload(),
                idempotency_key="sidera:SubmitKnowledgeCandidate:000000000012",
            )
        await write_client.close()
        assert unknown.value.command_id == COMMAND_ID
        assert unknown.value.outcome == "result_unknown"
        assert "wire secret" not in str(unknown.value)

    asyncio.run(scenario())


def test_command_status_does_not_turn_404_or_timeout_into_terminal_state() -> None:
    async def scenario() -> None:
        def not_found_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        not_found_client = KnowledgeClient(
            make_config(), transport=httpx.MockTransport(not_found_handler)
        )
        with pytest.raises(KnowledgeClientError) as not_found:
            await not_found_client.get_command_status(COMMAND_ID)
        await not_found_client.close()
        assert not_found.value.outcome == "failed"
        assert not_found.value.code == "not_found"

        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("status wire secret", request=request)

        timeout_client = KnowledgeClient(
            make_config(), transport=httpx.MockTransport(timeout_handler)
        )
        with pytest.raises(KnowledgeClientError) as unavailable:
            await timeout_client.get_command_status(COMMAND_ID)
        await timeout_client.close()
        assert unavailable.value.outcome == "unavailable"
        assert unavailable.value.code == "temporarily_unavailable"
        assert "status wire secret" not in str(unavailable.value)

    asyncio.run(scenario())


def test_unconfigured_knowledge_client_is_explicitly_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.delenv("SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN")
        client = KnowledgeClient(
            make_config(),
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(500, text="must not call")
            ),
        )
        with pytest.raises(KnowledgeClientError) as error:
            await client.search_knowledge("architecture")
        await client.close()

        assert error.value.code == "knowledge_not_configured"
        assert error.value.outcome == "unavailable"

    asyncio.run(scenario())
