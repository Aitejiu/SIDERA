import ast
import importlib
import sys
from pathlib import Path


def test_agents_never_import_task_product_or_legacy_internals() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "sidera_agents"
    assert root.is_dir()
    banned = (
        "sidera_control_plane",
        "sidera_product",
        "sidera_main_agent_gateway",
        "sidera_task_bridge",
        "akashic_sdk",
        "agent",
    )
    violations: list[str] = []

    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                continue
            for name in imported:
                if any(
                    name == prefix or name.startswith(prefix + ".") for prefix in banned
                ):
                    violations.append(f"{path}: {name}")

    assert not violations, "forbidden imports:\n" + "\n".join(violations)


def test_core_api_import_does_not_load_optional_storage_or_adapters() -> None:
    importlib.import_module("sidera_agents.api")

    forbidden = (
        "sidera_agents.postgres",
        "sidera_agents.harnesses",
        "sqlalchemy",
        "alembic",
        "psycopg",
    )
    loaded = [
        name
        for name in sys.modules
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ]
    assert not loaded
