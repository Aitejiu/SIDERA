from __future__ import annotations

import tomllib
from pathlib import Path


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
