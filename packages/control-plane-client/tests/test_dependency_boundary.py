import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

FORBIDDEN = (
    "akashic",
    "akashic_sdk",
    "sidera_task_bridge",
    "sidera_main_agent_gateway",
    "sidera_control_plane",
    "sqlalchemy",
    "sqlmodel",
)


def _package_for_source(relative_path: Path) -> str:
    containing_directory = relative_path.with_suffix("").parts[:-1]
    return ".".join(("sidera_control_plane_client", *containing_directory))


def _imported_modules(source: str, package: str) -> tuple[str, ...]:
    names = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            raw = "." * node.level + (node.module or "")
            base = resolve_name(raw, package) if node.level else raw
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names)
    return tuple(names)


@pytest.mark.parametrize(
    ("relative_path", "expected"),
    [
        (Path("__init__.py"), "sidera_control_plane_client"),
        (Path("config.py"), "sidera_control_plane_client"),
        (Path("subpackage/__init__.py"), "sidera_control_plane_client.subpackage"),
        (Path("subpackage/client.py"), "sidera_control_plane_client.subpackage"),
    ],
)
def test_source_package_is_its_containing_directory(relative_path, expected):
    assert _package_for_source(relative_path) == expected


def test_absolute_import_node_is_reported_independently():
    imports = _imported_modules(
        "import sidera_task_bridge.client", "sidera_control_plane_client"
    )

    assert imports == ("sidera_task_bridge.client",)
    assert any(
        name == prefix or name.startswith(prefix + ".")
        for name in imports
        for prefix in FORBIDDEN
    )


def test_nested_type_checking_import_from_is_reported_independently():
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    def host_only():\n"
        "        from sidera_main_agent_gateway.client import HostClient\n"
    )
    imports = _imported_modules(source, "sidera_control_plane_client")

    assert imports == (
        "typing",
        "typing.TYPE_CHECKING",
        "sidera_main_agent_gateway.client",
        "sidera_main_agent_gateway.client.HostClient",
    )
    assert any(
        name == prefix or name.startswith(prefix + ".")
        for name in imports
        for prefix in FORBIDDEN
    )


def test_allowed_relative_imports_resolve_to_hand_derived_names():
    package = _package_for_source(Path("subpackage/consumer.py"))
    imports = _imported_modules(
        "from . import config\nfrom .contracts import CommandEnvelope\n", package
    )

    assert package == "sidera_control_plane_client.subpackage"
    assert imports == (
        "sidera_control_plane_client.subpackage",
        "sidera_control_plane_client.subpackage.config",
        "sidera_control_plane_client.subpackage.contracts",
        "sidera_control_plane_client.subpackage.contracts.CommandEnvelope",
    )


def test_neutral_client_package_has_no_host_or_orm_imports():
    package_root = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "sidera_control_plane_client"
    )
    files = tuple(sorted(package_root.rglob("*.py")))
    assert files, "the neutral client package has no source modules"
    violations = []
    for path in files:
        source = path.read_text(encoding="utf-8")
        package = _package_for_source(path.relative_to(package_root))
        for name in _imported_modules(source, package):
            if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN):
                violations.append((path.name, name))
    assert violations == []
