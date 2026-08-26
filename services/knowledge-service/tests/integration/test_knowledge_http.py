from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from sidera_knowledge.api.app import create_app
from sidera_knowledge.config import KnowledgeSettings
from sidera_knowledge.domain.pages import (
    KnowledgePage,
    KnowledgeRevision,
    KnowledgeSectionSnapshot,
    KnowledgeSource,
)
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


def _settings(database_url: str) -> KnowledgeSettings:
    return KnowledgeSettings(
        database_url=database_url,
        local_owner_token=SecretStr("owner-token"),
        main_agent_token=SecretStr("main-agent-token"),
        control_plane_token=SecretStr("control-plane-token"),
    )


def _seed_page(database: Database) -> tuple[KnowledgePage, KnowledgeSource, KnowledgeRevision]:
    workspace_id = uuid4()
    page = KnowledgePage(
        page_id=uuid4(),
        workspace_id=workspace_id,
        canonical_title="HTTP page",
        slug=f"http-{uuid4().hex}",
    )
    source = KnowledgeSource(
        source_id=uuid4(),
        workspace_id=workspace_id,
        source_type="manual",
        label="http.md",
        metadata={"origin": "http-test"},
    )
    revision = KnowledgeRevision(
        revision_id=uuid4(),
        page_id=page.page_id,
        workspace_id=workspace_id,
        revision_number=1,
        title=page.canonical_title,
        summary="HTTP summary",
        tags=("http",),
        sections=(
            KnowledgeSectionSnapshot(
                "overview", "Overview", "", "HTTP body marker\n", 0
            ),
        ),
        relations=(),
        source_refs=(source.source_id,),
        change_reason="test",
        created_by="test",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_page(page)
        uow.sources.add_source(source)
        uow.commit()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.pages.add_revision_and_advance_page(revision, expected_page_version=1)
        uow.commit()
    return page, source, revision


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _command_body(workspace_id, actor_type: str, actor_id: str, payload: dict[str, object]):
    command_id = uuid4()
    return command_id, {
        "command_id": str(command_id),
        "workspace_id": str(workspace_id),
        "command_type": "SubmitKnowledgeCandidate",
        "target_type": "knowledge_candidate",
        "actor": {"type": actor_type, "id": actor_id},
        "target_id": None,
        "expected_version": None,
        "idempotency_key": f"http-{command_id}",
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def _candidate_payload(candidate_id) -> dict[str, object]:
    return {
        "candidate_schema_version": 1,
        "candidate_id": str(candidate_id),
        "operation": "create_page",
        "title": "HTTP candidate",
        "slug": f"candidate-{candidate_id.hex}",
        "summary": "Candidate summary",
        "tags": ["http"],
        "sections": [
            {
                "section_key": "overview",
                "heading": "Overview",
                "summary": "",
                "body_markdown": "Candidate body\n",
                "ordinal": 0,
            }
        ],
        "relations": [],
        "sources": [
            {
                "source_type": "manual",
                "label": "candidate.md",
                "metadata": {"origin": "http"},
            }
        ],
        "provenance": {"producer_type": "main_agent", "producer_id": "agent-http"},
    }


def test_http_reads_search_and_capabilities_are_scoped(
    database_url: str,
    database: Database,
) -> None:
    page, _, revision = _seed_page(database)
    settings = _settings(database_url)
    with TestClient(create_app(settings)) as client:
        assert client.get(
            f"/api/v1/workspaces/{page.workspace_id}/knowledge/pages"
        ).status_code == 403
        assert client.get(
            f"/api/v1/workspaces/{page.workspace_id}/knowledge/pages",
            headers=_headers("wrong-token"),
        ).status_code == 403

        pages = client.get(
            f"/api/v1/workspaces/{page.workspace_id}/knowledge/pages",
            headers=_headers("owner-token"),
        )
        assert pages.status_code == 200
        assert pages.json()["items"][0]["page_id"] == str(page.page_id)

        detail = client.get(
            f"/api/v1/workspaces/{page.workspace_id}/knowledge/pages/{page.page_id}",
            headers=_headers("owner-token"),
        )
        assert detail.status_code == 200
        assert detail.json()["current_revision"]["sections"][0]["body_markdown"]

        search = client.get(
            f"/api/v1/workspaces/{page.workspace_id}/knowledge/search",
            params={"q": "HTTP", "limit": 20},
            headers=_headers("control-plane-token"),
        )
        assert search.status_code == 200
        assert search.json()["items"][0]["page_id"] == str(page.page_id)
        assert "body_markdown" not in search.json()["items"][0]

        exact_revision = client.get(
            f"/api/v1/workspaces/{page.workspace_id}/knowledge/pages/"
            f"{page.page_id}/revisions/{revision.revision_id}",
            headers=_headers("control-plane-token"),
        )
        assert exact_revision.status_code == 200
        assert exact_revision.json()["revision_id"] == str(revision.revision_id)

        forbidden = client.get(
            f"/api/v1/workspaces/{page.workspace_id}/knowledge/pages",
            headers=_headers("control-plane-token"),
        )
        assert forbidden.status_code == 403


def test_http_command_and_status_are_safe_and_main_agent_cannot_review(
    database_url: str,
    database: Database,
) -> None:
    page, _, _ = _seed_page(database)
    settings = _settings(database_url)
    candidate_id = uuid4()
    command_id, body = _command_body(
        page.workspace_id,
        "main_agent",
        "agent-http",
        _candidate_payload(candidate_id),
    )
    with TestClient(create_app(settings)) as client:
        submitted = client.post(
            "/api/v1/commands",
            json=body,
            headers=_headers("main-agent-token"),
        )
        assert submitted.status_code == 200
        assert submitted.json()["status"] == "applied"
        assert submitted.json()["result"]["candidate_id"] == str(candidate_id)
        assert "Candidate body" not in submitted.text

        status = client.get(
            f"/api/v1/workspaces/{page.workspace_id}/commands/{command_id}",
            headers=_headers("main-agent-token"),
        )
        assert status.status_code == 200
        assert status.json()["status"] == "applied"
        assert "Candidate body" not in status.text

        pending = client.get(
            f"/api/v1/workspaces/{page.workspace_id}/commands/{uuid4()}",
            headers=_headers("main-agent-token"),
        )
        assert pending.status_code == 200
        assert pending.json()["status"] == "pending"
        assert "payload" not in pending.json()

        invalid = client.post(
            "/api/v1/commands",
            json={**body, "unexpected": True},
            headers=_headers("main-agent-token"),
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "validation_error"

        mismatched = {
            **body,
            "command_id": str(uuid4()),
            "payload": {
                "candidate_id": str(candidate_id),
                "decision": "accept",
                "reason": "wrong variant",
                "observed_candidate_hash": "a" * 64,
                "expected_candidate_version": 1,
            },
        }
        rejected_shape = client.post(
            "/api/v1/commands",
            json=mismatched,
            headers=_headers("main-agent-token"),
        )
        assert rejected_shape.status_code == 422
        assert rejected_shape.json()["error"]["code"] == "validation_error"

        review_id = uuid4()
        review_body = {
            **body,
            "command_id": str(review_id),
            "command_type": "ReviewKnowledgeCandidate",
            "actor": {"type": "main_agent", "id": "agent-http"},
            "target_type": "knowledge_candidate",
            "target_id": str(candidate_id),
            "idempotency_key": f"http-review-{review_id}",
            "payload": {
                "candidate_id": str(candidate_id),
                "decision": "accept",
                "reason": "not allowed",
                "observed_candidate_hash": "a" * 64,
                "expected_candidate_version": 1,
            },
        }
        forbidden = client.post(
            "/api/v1/commands",
            json=review_body,
            headers=_headers("main-agent-token"),
        )
        assert forbidden.status_code == 403

        rejected_status = client.get(
            f"/api/v1/workspaces/{page.workspace_id}/commands/{review_id}",
            headers=_headers("main-agent-token"),
        )
        assert rejected_status.status_code == 200
        assert rejected_status.json()["status"] == "rejected"
        assert rejected_status.json()["error_code"] == "permission_denied"
        assert "payload" not in rejected_status.json()

        control_id, control_body = _command_body(
            page.workspace_id,
            "control_plane",
            "cp-http",
            _candidate_payload(uuid4()),
        )
        control_submit = client.post(
            "/api/v1/commands",
            json=control_body,
            headers=_headers("control-plane-token"),
        )
        assert control_submit.status_code == 403
        assert control_submit.json()["error"]["code"] == "permission_denied"
        control_status = client.get(
            f"/api/v1/workspaces/{page.workspace_id}/commands/{control_id}",
            headers=_headers("control-plane-token"),
        )
        assert control_status.status_code == 200
        assert control_status.json()["status"] == "rejected"
        assert "Candidate body" not in control_status.text


def test_http_internal_materialization_requires_control_plane(
    database_url: str,
    database: Database,
) -> None:
    page, source, revision = _seed_page(database)
    section = revision.sections[0]
    body = {
        "sections": [
            {
                "ordinal": 4,
                "page_id": str(page.page_id),
                "revision_id": str(revision.revision_id),
                "revision_hash": revision.content_hash,
                "section_key": section.section_key,
                "section_hash": section.section_hash,
                "source_ids": [str(source.source_id)],
            }
        ]
    }
    settings = _settings(database_url)
    with TestClient(create_app(settings)) as client:
        owner = client.post(
            f"/api/v1/internal/workspaces/{page.workspace_id}/materialize",
            json=body,
            headers=_headers("owner-token"),
        )
        assert owner.status_code == 403

        control_plane = client.post(
            f"/api/v1/internal/workspaces/{page.workspace_id}/materialize",
            json=body,
            headers=_headers("control-plane-token"),
        )
        assert control_plane.status_code == 200
        assert control_plane.json()["sections"][0]["ordinal"] == 4
        assert control_plane.json()["sections"][0]["body_markdown"] == "HTTP body marker\n"
