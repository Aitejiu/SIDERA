from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import SecretStr

from sidera_knowledge.config import KnowledgeSettings


def test_knowledge_package_does_not_import_control_plane() -> None:
    import sidera_knowledge

    assert sidera_knowledge.__version__ == "0.1.0"
    assert "sidera_control_plane" not in sidera_knowledge.__dict__


def test_runtime_dependency_set_is_explicit() -> None:
    project_path = Path(__file__).parents[2] / "pyproject.toml"
    project = tomllib.loads(project_path.read_text(encoding="utf-8"))
    dependencies = set(project["project"]["dependencies"])

    for required in (
        "alembic",
        "fastapi",
        "httpx",
        "psycopg[binary]",
        "pydantic",
        "pydantic-settings",
        "sqlalchemy",
        "uvicorn",
    ):
        assert any(item.startswith(required) for item in dependencies), required

    assert not any(
        "sidera-control-plane" in item or "sidera_control_plane" in item
        for item in dependencies
    )


def test_knowledge_settings_use_knowledge_prefix_and_mask_secrets(monkeypatch) -> None:
    monkeypatch.setenv(
        "SIDERA_KNOWLEDGE_DATABASE_URL",
        "postgresql+psycopg://sidera@127.0.0.1:55432/sidera_knowledge_test",
    )
    monkeypatch.setenv("SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN", "main-secret")
    settings = KnowledgeSettings()  # pyright: ignore[reportCallIssue]

    assert settings.host == "127.0.0.1"
    assert settings.port == 8020
    assert settings.alembic_head == "0001_knowledge_foundation"
    assert settings.main_agent_token == SecretStr("main-secret")
    assert "main-secret" not in repr(settings)
    assert "main-secret" not in settings.model_dump_json()
    assert "main-secret" not in str(settings.model_dump())


def test_knowledge_settings_require_database_and_positive_limits(monkeypatch) -> None:
    monkeypatch.delenv("SIDERA_KNOWLEDGE_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="database_url"):
        KnowledgeSettings()  # pyright: ignore[reportCallIssue]

    with pytest.raises(ValueError):
        KnowledgeSettings(
            database_url="postgresql+psycopg://test",
            request_max_bytes=0,
        )
