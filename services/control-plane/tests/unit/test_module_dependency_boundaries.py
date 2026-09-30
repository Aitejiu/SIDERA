from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

FORBIDDEN = (
    "sidera_control_plane.adapters",
    "sidera_control_plane.composition",
    "sidera_control_plane.gateway_worker",
    "sidera_control_plane.planning_worker",
    "sidera_main_agent_gateway",
    "sidera_task_bridge",
    "akashic",
    "akashic_sdk",
)


def _imported_modules_with_lines(source: str, package: str) -> tuple[tuple[int, str], ...]:
    entries = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            entries.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            raw = "." * node.level + (node.module or "")
            base = resolve_name(raw, package) if node.level else raw
            entries.append((node.lineno, base))
            entries.extend((node.lineno, f"{base}.{alias.name}") for alias in node.names)
    return tuple(entries)


def imported_modules(source: str, package: str) -> tuple[str, ...]:
    return tuple(
        name for _line, name in _imported_modules_with_lines(source, package)
    )


def forbidden_imports(source: str, package: str) -> tuple[str, ...]:
    return tuple(
        name
        for name in imported_modules(source, package)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN)
    )


def _forbidden_imports_with_lines(source: str, package: str) -> tuple[tuple[int, str], ...]:
    return tuple(
        (line, name)
        for line, name in _imported_modules_with_lines(source, package)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN)
    )


def test_knowledge_error_is_an_application_port_type() -> None:
    from sidera_control_plane.adapters.knowledge_http import (
        KnowledgeAdapterError as CompatibilityError,
    )
    from sidera_control_plane.application.knowledge_errors import (
        KnowledgeAdapterError as PortError,
    )

    assert PortError is CompatibilityError
    error = PortError("knowledge_unavailable", "safe message", temporary=True)
    assert (error.code, error.temporary, error.status_code) == (
        "knowledge_unavailable",
        True,
        None,
    )
    assert str(error) == "safe message"


def test_guard_recognizes_relative_and_type_checking_imports() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    def load_adapter():\n"
        "        from ..adapters.knowledge_http import KnowledgeAdapterError\n"
    )
    assert forbidden_imports(source, "sidera_control_plane.application")
    assert forbidden_imports(
        "from .. import adapters", "sidera_control_plane.application"
    )
    assert not forbidden_imports(
        "from .ports import KnowledgeCatalogPort", "sidera_control_plane.application"
    )


def test_guard_recognizes_absolute_import_and_import_from_forms() -> None:
    source = (
        "import sidera_control_plane.adapters.knowledge_http\n"
        "from sidera_control_plane import adapters\n"
    )
    assert forbidden_imports(source, "sidera_control_plane.application") == (
        "sidera_control_plane.adapters.knowledge_http",
        "sidera_control_plane.adapters",
    )


def test_core_layers_do_not_import_installed_implementations() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "sidera_control_plane"
    violations = []
    for layer in ("domain", "application"):
        for path in sorted((root / layer).rglob("*.py")):
            parts = path.relative_to(root).with_suffix("").parts
            package_parts = parts[:-1]
            package = ".".join(("sidera_control_plane", *package_parts))
            source = path.read_text(encoding="utf-8")
            violations.extend(
                (str(path.relative_to(root)), line, name)
                for line, name in _forbidden_imports_with_lines(source, package)
            )
    assert not violations, "\n".join(
        f"{path}:{line}: {name}" for path, line, name in violations
    )
