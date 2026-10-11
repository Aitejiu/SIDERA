from __future__ import annotations

import ast
import importlib
import os
import tomllib
from collections.abc import Callable
from datetime import datetime
from importlib import metadata, resources
from pathlib import Path
from types import ModuleType
from typing import get_type_hints

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
PUBLIC_PACKAGES = (
    ("sidera-agents", "sidera_agents", "packages/agents"),
    (
        "sidera-control-plane",
        "sidera_control_plane",
        "services/control-plane",
    ),
    (
        "sidera-control-plane-client",
        "sidera_control_plane_client",
        "packages/control-plane-client",
    ),
    (
        "sidera-model-connections",
        "sidera_model_connections",
        "packages/model-connections",
    ),
    ("sidera-runtime-guard", "sidera_runtime_guard", "packages/runtime-guard"),
    ("sidera-product", "sidera_product", "apps/server"),
)
CLOCK_SIGNATURES = (
    (
        "AgentService.__init__",
        [
            "self",
            "store",
            "registry",
            "authorizer",
            "sources",
            "secrets",
            "clock",
            "tool_connections",
        ],
        0,
    ),
    (
        "create_agent_service",
        [
            "store",
            "registry",
            "authorizer",
            "sources",
            "secrets",
            "clock",
            "tool_connections",
        ],
        1,
    ),
)


@pytest.mark.parametrize(
    ("distribution_name", "module_name", "project_path"),
    PUBLIC_PACKAGES,
    ids=[package[0] for package in PUBLIC_PACKAGES],
)
def test_source_marker(
    distribution_name: str, module_name: str, project_path: str
) -> None:
    project = REPOSITORY / project_path
    marker = project / "src" / module_name / "py.typed"
    assert marker.is_file(), f"Missing typing marker: {marker}"
    assert not marker.is_symlink()
    assert marker.read_bytes() == b""
    with (project / "pyproject.toml").open("rb") as source:
        configuration = tomllib.load(source)
    assert configuration["project"]["name"] == distribution_name
    assert (
        f"src/{module_name}"
        in configuration["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    )


@pytest.mark.parametrize(
    ("signature", "expected_arguments", "expected_default_count"),
    CLOCK_SIGNATURES,
    ids=[signature[0] for signature in CLOCK_SIGNATURES],
)
def test_source_clock(
    signature: str,
    expected_arguments: list[str],
    expected_default_count: int,
) -> None:
    bootstrap = REPOSITORY / "packages/agents/src/sidera_agents/bootstrap.py"
    tree = ast.parse(bootstrap.read_text(encoding="utf-8"))
    if signature == "AgentService.__init__":
        service = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "AgentService"
        )
        node = next(
            node
            for node in service.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
    else:
        node = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "create_agent_service"
        )
    clock_argument = next(arg for arg in node.args.args if arg.arg == "clock")
    assert clock_argument.annotation is not None
    assert ast.unparse(clock_argument.annotation) == "Callable[[], datetime]"
    assert [arg.arg for arg in node.args.args] == expected_arguments
    assert len(node.args.defaults) == expected_default_count
    assert node.args.posonlyargs == []
    assert node.args.kwonlyargs == []
    assert node.args.vararg is None
    assert node.args.kwarg is None
    if expected_default_count:
        assert isinstance(node.args.defaults[0], ast.Constant)
        assert node.args.defaults[0].value is None


@pytest.fixture
def installed_prefix() -> Path:
    value = os.environ.get("SIDERA_NATIVE_INSTALLED_PREFIX")
    assert value, "SIDERA_NATIVE_INSTALLED_PREFIX is required for installed tests"
    prefix = Path(value).resolve()
    assert prefix.is_dir()
    assert not prefix.is_relative_to(REPOSITORY)
    return prefix


def _installed_module(module_name: str, installed_prefix: Path) -> ModuleType:
    module = importlib.import_module(module_name)
    assert module.__file__ is not None
    module_path = Path(module.__file__).resolve()
    assert module_path.is_relative_to(installed_prefix)
    assert "site-packages" in module_path.relative_to(installed_prefix).parts
    assert not module_path.is_relative_to(REPOSITORY)
    return module


@pytest.mark.parametrize(
    ("distribution_name", "module_name"),
    [package[:2] for package in PUBLIC_PACKAGES],
    ids=[package[0] for package in PUBLIC_PACKAGES],
)
def test_installed_marker_and_provenance(
    distribution_name: str, module_name: str, installed_prefix: Path
) -> None:
    module = _installed_module(module_name, installed_prefix)
    distribution = metadata.distribution(distribution_name)
    assert distribution.version == "0.1.0"
    assert distribution.read_text("RECORD") is not None
    files = distribution.files
    assert files is not None
    marker_name = f"{module_name}/py.typed"
    assert marker_name in {str(file) for file in files}
    marker = Path(distribution.locate_file(marker_name))
    assert not marker.is_symlink()
    assert marker.resolve().is_relative_to(installed_prefix)
    assert module.__file__ is not None
    assert marker.resolve() == Path(module.__file__).resolve().with_name("py.typed")
    assert resources.files(module).joinpath("py.typed").read_bytes() == b""


@pytest.mark.parametrize("signature", [signature[0] for signature in CLOCK_SIGNATURES])
def test_installed_clock(signature: str, installed_prefix: Path) -> None:
    bootstrap = _installed_module("sidera_agents.bootstrap", installed_prefix)
    function = (
        bootstrap.AgentService.__init__
        if signature == "AgentService.__init__"
        else bootstrap.create_agent_service
    )
    assert get_type_hints(function)["clock"] == Callable[[], datetime]
