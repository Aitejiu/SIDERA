from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from sidera_control_plane.adapters.knowledge_http import (
    KnowledgeAdapterError,
    KnowledgeHttpAdapter,
)
from sidera_control_plane.application.ports import ExactSectionRef
from sidera_control_plane.config import Settings

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
PAGE_ID = UUID("20000000-0000-0000-0000-000000000002")
REVISION_ID = UUID("30000000-0000-0000-0000-000000000003")
SOURCE_ID = UUID("40000000-0000-0000-0000-000000000004")
CORRELATION_ID = UUID("50000000-0000-0000-0000-000000000005")
CALLER_REQUEST_ID = UUID("60000000-0000-0000-0000-000000000006")
TOKEN = "knowledge-control-plane-secret"


def _settings(**overrides: Any) -> Settings:
    return Settings(
        database_url="postgresql+psycopg://sidera@127.0.0.1:55432/sidera_test",
        **overrides,
    )


def _source() -> dict[str, object]:
    return {
        "source_id": str(SOURCE_ID),
        "workspace_id": str(WORKSPACE_ID),
        "source_type": "markdown_file",
        "label": "context.md",
        "uri": None,
        "workspace_relative_path": "docs/context.md",
        "git_revision": "a" * 40,
        "digest": None,
        "metadata": {"origin": "test"},
        "availability": "available",
        "safe_reason_code": None,
        "last_checked_at": None,
        "version": 1,
    }


def _search_hit() -> dict[str, object]:
    return {
        "page_id": str(PAGE_ID),
        "revision_id": str(REVISION_ID),
        "section_key": "overview",
        "revision_number": 2,
        "revision_hash": "b" * 64,
        "section_hash": "c" * 64,
        "title": "Context",
        "summary": "Context summary",
        "tags": ["context"],
        "source_ids": [str(SOURCE_ID)],
        "source_types": ["markdown_file"],
        "source_availability": ["available"],
        "trust": "workspace_confirmed",
        "section_byte_count": 64,
        "rank": 0.98,
        "rank_explanation": "title match",
    }


def _revision() -> dict[str, object]:
    return {
        "revision_id": str(REVISION_ID),
        "page_id": str(PAGE_ID),
        "workspace_id": str(WORKSPACE_ID),
        "revision_number": 2,
        "title": "Context",
        "summary": "Context summary",
        "tags": ["context"],
        "content_hash": "b" * 64,
        "change_reason": "test",
        "created_by": "test",
        "created_at": datetime(2026, 8, 26, tzinfo=UTC).isoformat(),
        "accepted_candidate_id": None,
        "sections": [
            {
                "section_key": "overview",
                "heading": "Overview",
                "summary": "Overview summary",
                "body_markdown": "body must only be returned by materialize",
                "ordinal": 0,
                "section_hash": "c" * 64,
                "content_byte_count": 64,
            }
        ],
        "relations": [],
        "source_refs": [str(SOURCE_ID)],
        "sources": [_source()],
    }


def _materialized() -> dict[str, object]:
    return {
        "sections": [
            {
                "ordinal": 0,
                "page_id": str(PAGE_ID),
                "revision_id": str(REVISION_ID),
                "revision_hash": "b" * 64,
                "section_key": "overview",
                "section_hash": "c" * 64,
                "body_markdown": "exact body",
                "sources": [_source()],
                "trust": "workspace_confirmed",
                "section_byte_count": 11,
            }
        ],
        "total_byte_count": 11,
    }


def _adapter(
    handler,
    *,
    metadata_timeout_seconds: float = 5,
    materialization_timeout_seconds: float = 15,
) -> tuple[KnowledgeHttpAdapter, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        KnowledgeHttpAdapter(
            base_url="http://127.0.0.1:8020",
            control_plane_token=SecretStr(TOKEN),
            metadata_timeout_seconds=metadata_timeout_seconds,
            materialization_timeout_seconds=materialization_timeout_seconds,
            client=client,
        ),
        client,
    )


def _ref() -> ExactSectionRef:
    return ExactSectionRef(
        ordinal=0,
        page_id=PAGE_ID,
        revision_id=REVISION_ID,
        revision_hash="b" * 64,
        section_key="overview",
        section_hash="c" * 64,
        source_ids=(SOURCE_ID,),
    )


def test_settings_add_knowledge_runtime_contract_and_redact_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIDERA_KNOWLEDGE_BASE_URL", raising=False)
    settings = _settings(knowledge_control_plane_token=SecretStr(TOKEN))

    assert settings.knowledge_base_url == "http://127.0.0.1:8020"
    assert settings.knowledge_metadata_timeout_seconds == 5
    assert settings.knowledge_materialization_timeout_seconds == 15
    assert settings.context_assembly_max_attempts == 5
    assert settings.planning_context_ttl_seconds == 1800
    assert settings.execution_context_ttl_seconds == 7200
    assert str(settings.knowledge_control_plane_token) == "**********"
    assert TOKEN not in repr(settings)


def test_http_adapter_does_not_inherit_system_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.Client
    seen: dict[str, object] = {}

    def client_factory(*args: object, **kwargs: object) -> httpx.Client:
        seen.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_factory)
    adapter = KnowledgeHttpAdapter(base_url="http://127.0.0.1:8020")
    adapter.close()

    assert seen["trust_env"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"knowledge_base_url": "file:///tmp/knowledge"},
        {"knowledge_metadata_timeout_seconds": 11},
        {"knowledge_materialization_timeout_seconds": 31},
        {"context_assembly_max_attempts": 6},
        {"planning_context_ttl_seconds": 7_201},
        {"execution_context_ttl_seconds": 86_401},
        {"context_package_cutover_at": "2026-08-26T12:00:00"},
    ],
)
def test_settings_reject_unsafe_knowledge_limits(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _settings(**overrides)


def test_http_adapter_search_revision_and_materialize_forward_typed_requests() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/knowledge/search"):
            return httpx.Response(200, json={"items": [_search_hit()], "next_cursor": None})
        if request.url.path.endswith(f"/revisions/{REVISION_ID}"):
            return httpx.Response(200, json=_revision())
        if request.url.path.endswith("/internal/workspaces/" + str(WORKSPACE_ID) + "/materialize"):
            return httpx.Response(200, json=_materialized())
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no", "details": {}}})

    adapter, client = _adapter(handler)
    try:
        hits = adapter.search(
            WORKSPACE_ID,
            "context",
            tags=("context",),
            source_types=("markdown_file",),
            status="active",
            limit=5,
            correlation_id=CORRELATION_ID,
            caller_request_id=CALLER_REQUEST_ID,
        )
        manifest = adapter.get_revision_manifest(
            WORKSPACE_ID,
            PAGE_ID,
            REVISION_ID,
            correlation_id=CORRELATION_ID,
            caller_request_id=CALLER_REQUEST_ID,
        )
        bodies = adapter.materialize(
            WORKSPACE_ID,
            (_ref(),),
            correlation_id=CORRELATION_ID,
            caller_request_id=CALLER_REQUEST_ID,
        )
    finally:
        client.close()

    assert hits[0].page_id == PAGE_ID
    assert hits[0].section_hash == "c" * 64
    assert manifest.revision_id == REVISION_ID
    assert manifest.sections[0].section_key == "overview"
    assert bodies[0].body_markdown == "exact body"
    assert len(seen) == 3
    for request in seen:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.headers["X-Correlation-ID"] == str(CORRELATION_ID)
        assert request.headers["X-Caller-Request-ID"] == str(CALLER_REQUEST_ID)
        assert str(WORKSPACE_ID) in str(request.url)
    search_request = next(item for item in seen if item.url.path.endswith("/knowledge/search"))
    assert search_request.url.params["q"] == "context"
    assert search_request.url.params["tags"] == "context"
    assert search_request.url.params["source_types"] == "markdown_file"
    materialize_request = next(item for item in seen if item.url.path.endswith("/materialize"))
    assert "body_markdown" not in materialize_request.content.decode()


def test_http_adapter_submits_candidate_and_queries_deterministic_command_status() -> None:
    command_id = UUID("70000000-0000-0000-0000-000000000007")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "status": "applied",
                    "command_id": str(command_id),
                    "result": {"candidate_id": str(PAGE_ID), "status": "pending"},
                },
            )
        return httpx.Response(
            200,
            json={
                "command_id": str(command_id),
                "workspace_id": str(WORKSPACE_ID),
                "status": "pending",
                "result": None,
                "error_code": None,
            },
        )

    adapter, client = _adapter(handler)
    try:
        payload = {
            "candidate_schema_version": 1,
            "candidate_id": str(PAGE_ID),
            "operation": "create_page",
            "title": "Candidate",
            "slug": "candidate",
            "sections": [],
        }
        applied = adapter.submit_candidate(
            WORKSPACE_ID,
            command_id,
            "candidate-idempotency-key",
            payload,
            correlation_id=CORRELATION_ID,
            caller_request_id=CALLER_REQUEST_ID,
        )
        pending = adapter.get_command_status(
            WORKSPACE_ID,
            command_id,
            correlation_id=CORRELATION_ID,
            caller_request_id=CALLER_REQUEST_ID,
        )
    finally:
        client.close()

    assert applied.status == "applied"
    assert applied.result == {"candidate_id": str(PAGE_ID), "status": "pending"}
    assert pending.status == "pending"
    assert len(seen) == 2
    assert seen[0].url.path == "/api/v1/commands"
    assert seen[1].url.path.endswith(f"/commands/{command_id}")
    assert "candidate-idempotency-key" in seen[0].content.decode()


@pytest.mark.parametrize(
    "status,expected_code,temporary",
    [
        (404, "reference_not_found", False),
        (409, "permanent_mismatch", False),
        (403, "permission_denied", False),
        (429, "temporary_unavailable", True),
        (500, "temporary_unavailable", True),
    ],
)
def test_http_adapter_maps_typed_http_failures(
    status: int, expected_code: str, temporary: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": "stale_revision" if status == 409 else "private",
                    "message": "private response body marker",
                    "details": {},
                }
            },
        )

    adapter, client = _adapter(handler)
    try:
        with pytest.raises(KnowledgeAdapterError) as error:
            adapter.search(
                WORKSPACE_ID,
                "context",
                correlation_id=CORRELATION_ID,
                caller_request_id=CALLER_REQUEST_ID,
            )
    finally:
        client.close()

    assert error.value.code == expected_code
    assert error.value.temporary is temporary
    assert error.value.status_code == status
    assert TOKEN not in str(error.value)
    assert "private response body marker" not in str(error.value)


@pytest.mark.parametrize("exception", [httpx.ReadTimeout, httpx.ConnectError])
def test_http_adapter_maps_network_failures_to_temporary_unavailable(exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception("network failure", request=request)

    adapter, client = _adapter(handler)
    try:
        with pytest.raises(KnowledgeAdapterError) as error:
            adapter.search(
                WORKSPACE_ID,
                "context",
                correlation_id=CORRELATION_ID,
                caller_request_id=CALLER_REQUEST_ID,
            )
    finally:
        client.close()

    assert error.value.code == "temporary_unavailable"
    assert error.value.temporary is True
    assert error.value.status_code is None


def test_http_adapter_rejects_extra_response_fields_without_logging_body(caplog) -> None:
    body_marker = "response-body-must-not-be-logged"

    def handler(request: httpx.Request) -> httpx.Response:
        hit = _search_hit()
        hit["unexpected"] = body_marker
        return httpx.Response(200, json={"items": [hit], "next_cursor": None})

    adapter, client = _adapter(handler)
    try:
        with pytest.raises(KnowledgeAdapterError) as error:
            adapter.search(
                WORKSPACE_ID,
                "context",
                correlation_id=CORRELATION_ID,
                caller_request_id=CALLER_REQUEST_ID,
            )
    finally:
        client.close()

    assert error.value.code == "protocol_error"
    assert body_marker not in caplog.text
    assert TOKEN not in caplog.text
