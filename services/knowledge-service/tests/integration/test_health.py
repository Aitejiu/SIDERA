from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sidera_knowledge.api.app import create_app
from sidera_knowledge.config import KnowledgeSettings

pytestmark = pytest.mark.integration


def test_live_does_not_require_database(database_url: str) -> None:
    settings = KnowledgeSettings(database_url=database_url)
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "sidera-knowledge",
    }


def test_ready_checks_database_and_migration_head(database_url: str) -> None:
    settings = KnowledgeSettings(database_url=database_url)
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "sidera-knowledge",
        "schema_head": "0001_knowledge_foundation",
    }


def test_ready_failure_has_safe_error_code_without_database_url() -> None:
    database_url = "postgresql+psycopg://user:password@127.0.0.1:1/secret_db"
    settings = KnowledgeSettings(database_url=database_url)
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "error_code": "database_unavailable",
    }
    assert database_url not in response.text
