from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

PROJECTS = {
    "sidera-agents": "packages/agents",
    "sidera-runtime-guard": "packages/runtime-guard",
    "sidera-control-plane-client": "packages/control-plane-client",
    "sidera-control-plane": "services/control-plane",
    "sidera-akashic-task-bridge": "integrations/akashic",
    "sidera-main-agent-gateway": "services/main-agent-gateway",
}
REQUIREMENTS = {
    "agents-core": "sidera-agents==0.1.0",
    "control-plane-core": "sidera-control-plane==0.1.0",
    "control-plane-agents": "sidera-control-plane==0.1.0",
    "public-client": "sidera-control-plane-client==0.1.0",
    "gateway-core": "sidera-main-agent-gateway==0.1.0",
    "bridge-client": "sidera-akashic-task-bridge==0.1.0",
    "gateway-demo": "sidera-main-agent-gateway[demo]==0.1.0",
}
COMBINATION_PROJECTS = {
    "agents-core": ("sidera-agents",),
    "control-plane-core": (
        "sidera-control-plane",
        "sidera-agents",
        "sidera-runtime-guard",
    ),
    "control-plane-agents": (
        "sidera-control-plane",
        "sidera-agents",
        "sidera-runtime-guard",
    ),
    "public-client": ("sidera-control-plane-client",),
    "gateway-core": (
        "sidera-main-agent-gateway",
        "sidera-runtime-guard",
    ),
    "bridge-client": (
        "sidera-akashic-task-bridge",
        "sidera-control-plane-client",
    ),
    "gateway-demo": (
        "sidera-main-agent-gateway",
        "sidera-akashic-task-bridge",
        "sidera-control-plane-client",
        "sidera-runtime-guard",
    ),
}
SOURCE_ROOTS = {
    "sidera-agents": "packages/agents/src",
    "sidera-runtime-guard": "packages/runtime-guard/src",
    "sidera-control-plane-client": "packages/control-plane-client/src",
    "sidera-control-plane": "services/control-plane/src",
    "sidera-akashic-task-bridge": "integrations/akashic/sidera_task_bridge",
    "sidera-main-agent-gateway": "services/main-agent-gateway/src",
}
ABSENT_MODULES = {
    "agents-core": (
        "sidera_control_plane",
        "sidera_control_plane_client",
        "sidera_product",
        "sidera_main_agent_gateway",
        "sidera_task_bridge",
        "sidera_runtime_guard",
        "akashic_sdk",
        "sqlalchemy",
        "alembic",
        "psycopg",
        "openai",
        "anthropic",
    ),
    "control-plane-core": (
        "akashic_sdk",
        "sidera_task_bridge",
        "sidera_main_agent_gateway",
        "sidera_control_plane_client",
    ),
    "control-plane-agents": (
        "akashic_sdk",
        "sidera_task_bridge",
        "sidera_main_agent_gateway",
        "sidera_product",
        "sidera_control_plane_client",
    ),
    "public-client": (
        "sidera_control_plane",
        "sqlalchemy",
        "alembic",
        "psycopg",
        "akashic_sdk",
        "sidera_task_bridge",
        "sidera_main_agent_gateway",
    ),
    "gateway-core": (
        "akashic_sdk",
        "sidera_task_bridge",
        "sidera_control_plane",
    ),
    "bridge-client": (
        "akashic_sdk",
        "sidera_control_plane",
        "sidera_main_agent_gateway",
    ),
    "gateway-demo": ("akashic_sdk", "sidera_control_plane"),
}
ABSENT_DISTRIBUTIONS = {
    "agents-core": (
        "sidera-control-plane",
        "sidera-control-plane-client",
        "sidera-product",
        "sidera-main-agent-gateway",
        "sidera-akashic-task-bridge",
        "sidera-runtime-guard",
        "akashic-agent-sdk",
        "sqlalchemy",
        "alembic",
        "psycopg",
        "psycopg-binary",
        "openai",
        "anthropic",
    ),
    "control-plane-core": (
        "akashic-agent-sdk",
        "sidera-akashic-task-bridge",
        "sidera-main-agent-gateway",
        "sidera-control-plane-client",
    ),
    "control-plane-agents": (
        "akashic-agent-sdk",
        "sidera-akashic-task-bridge",
        "sidera-main-agent-gateway",
        "sidera-product",
        "sidera-control-plane-client",
    ),
    "public-client": (
        "sidera-control-plane",
        "sqlalchemy",
        "alembic",
        "psycopg",
        "psycopg-binary",
        "akashic-agent-sdk",
        "sidera-akashic-task-bridge",
        "sidera-main-agent-gateway",
    ),
    "gateway-core": (
        "akashic-agent-sdk",
        "sidera-akashic-task-bridge",
        "sidera-control-plane",
    ),
    "bridge-client": (
        "akashic-agent-sdk",
        "sidera-control-plane",
        "sidera-main-agent-gateway",
    ),
    "gateway-demo": ("akashic-agent-sdk", "sidera-control-plane"),
}


PROVENANCE_HELPER_SOURCE = r"""
import base64
import csv
import hashlib
import io
import zipfile
from pathlib import Path, PurePosixPath

def wheel_record_matches(wheel_path, installed_root):
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            record_names = [
                name for name in archive.namelist()
                if name.endswith(".dist-info/RECORD")
            ]
            if len(record_names) != 1:
                return False
            record_text = archive.read(record_names[0]).decode("utf-8")
            rows = csv.reader(io.StringIO(record_text, newline=""))
            has_python = False
            has_metadata = False
            checked = 0
            for row in rows:
                if len(row) != 3:
                    return False
                filename, encoded_hash, _size = row
                if not encoded_hash:
                    continue
                algorithm, separator, value = encoded_hash.partition("=")
                relative = PurePosixPath(filename)
                if (
                    algorithm != "sha256"
                    or not separator
                    or relative.is_absolute()
                    or ".." in relative.parts
                ):
                    return False
                wheel_bytes = archive.read(filename)
                wheel_hash = base64.urlsafe_b64encode(
                    hashlib.sha256(wheel_bytes).digest()
                ).rstrip(b"=").decode("ascii")
                if value != wheel_hash:
                    return False
                installed_path = Path(installed_root).joinpath(*relative.parts)
                if not installed_path.is_file():
                    return False
                installed_hash = base64.urlsafe_b64encode(
                    hashlib.sha256(installed_path.read_bytes()).digest()
                ).rstrip(b"=").decode("ascii")
                if value != installed_hash:
                    return False
                checked += 1
                has_python = has_python or (
                    filename.endswith(".py") and ".dist-info/" not in filename
                )
                has_metadata = has_metadata or filename.endswith(".dist-info/METADATA")
            return checked > 0 and has_python and has_metadata
    except (OSError, ValueError, zipfile.BadZipFile, csv.Error):
        return False
"""


PROBE_SOURCE = (
    PROVENANCE_HELPER_SOURCE
    + r"""
import asyncio
from configparser import ConfigParser
import importlib
import importlib.metadata as metadata
import importlib.util
from importlib.resources import files
import json
import re
import sys
from pathlib import Path

PROJECTS = {
    "agents-core": ("sidera-agents",),
    "control-plane-core": ("sidera-control-plane", "sidera-agents", "sidera-runtime-guard"),
    "control-plane-agents": ("sidera-control-plane", "sidera-agents", "sidera-runtime-guard"),
    "public-client": ("sidera-control-plane-client",),
    "gateway-core": ("sidera-main-agent-gateway", "sidera-runtime-guard"),
    "bridge-client": ("sidera-akashic-task-bridge", "sidera-control-plane-client"),
    "gateway-demo": (
        "sidera-main-agent-gateway",
        "sidera-akashic-task-bridge",
        "sidera-control-plane-client",
        "sidera-runtime-guard",
    ),
}
ABSENT_MODULES = {
    "agents-core": (
        "sidera_control_plane", "sidera_control_plane_client", "sidera_product",
        "sidera_main_agent_gateway", "sidera_task_bridge", "sidera_runtime_guard",
        "akashic_sdk", "sqlalchemy", "alembic", "psycopg", "openai", "anthropic",
    ),
    "control-plane-core": (
        "akashic_sdk", "sidera_task_bridge", "sidera_main_agent_gateway",
        "sidera_control_plane_client",
    ),
    "control-plane-agents": (
        "akashic_sdk", "sidera_task_bridge", "sidera_main_agent_gateway",
        "sidera_product", "sidera_control_plane_client",
    ),
    "public-client": (
        "sidera_control_plane", "sqlalchemy", "alembic", "psycopg",
        "akashic_sdk", "sidera_task_bridge", "sidera_main_agent_gateway",
    ),
    "gateway-core": (
        "akashic_sdk", "sidera_task_bridge", "sidera_control_plane",
    ),
    "bridge-client": (
        "akashic_sdk", "sidera_control_plane", "sidera_main_agent_gateway",
    ),
    "gateway-demo": ("akashic_sdk", "sidera_control_plane"),
}
ABSENT_DISTRIBUTIONS = {
    "agents-core": (
        "sidera-control-plane", "sidera-control-plane-client", "sidera-product",
        "sidera-main-agent-gateway", "sidera-akashic-task-bridge", "sidera-runtime-guard",
        "akashic-agent-sdk", "sqlalchemy", "alembic", "psycopg", "psycopg-binary",
        "openai", "anthropic",
    ),
    "control-plane-core": (
        "akashic-agent-sdk", "sidera-akashic-task-bridge",
        "sidera-main-agent-gateway", "sidera-control-plane-client",
    ),
    "control-plane-agents": (
        "akashic-agent-sdk", "sidera-akashic-task-bridge", "sidera-product",
        "sidera-main-agent-gateway", "sidera-control-plane-client",
    ),
    "public-client": (
        "sidera-control-plane", "sqlalchemy", "alembic", "psycopg",
        "psycopg-binary", "akashic-agent-sdk", "sidera-akashic-task-bridge",
        "sidera-main-agent-gateway",
    ),
    "gateway-core": (
        "akashic-agent-sdk", "sidera-akashic-task-bridge", "sidera-control-plane",
    ),
    "bridge-client": (
        "akashic-agent-sdk", "sidera-control-plane", "sidera-main-agent-gateway",
    ),
    "gateway-demo": ("akashic-agent-sdk", "sidera-control-plane"),
}

class ProbeFailure(Exception):
    def __init__(self, code, packages=None):
        self.code = code
        self.packages = packages or {}

def _module_available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError as exc:
        missing = exc.name
        if missing and (name == missing or name.startswith(missing + ".")):
            return False
        raise

def _normalized_distribution(name):
    return re.sub(r"[-_.]+", "-", name).lower()

def _probe_agents_core():
    forbidden = (
        "sidera_agents.postgres", "sidera_agents.harnesses.opencode",
        "sidera_agents.harnesses.opencode_config", "sidera_agents.harnesses.opencode_descriptor",
        "sidera_agents.harnesses.acp_transport",
    )
    if any(
        module == prefix or module.startswith(prefix + ".")
        for module in sys.modules for prefix in forbidden
    ):
        raise ProbeFailure("dependency_boundary_failed")
    installed = Path(metadata.distribution("sidera-agents").locate_file("")).resolve()
    if not installed.is_relative_to(Path(sys.prefix).resolve()):
        raise ProbeFailure("dependency_boundary_failed")
    for name in (
        "sidera_agents", "sidera_agents.api", "sidera_agents.bootstrap",
        "sidera_agents.contracts", "sidera_agents.errors", "sidera_agents.ports",
    ):
        module = importlib.import_module(name)
        origin = getattr(module, "__file__", None)
        if not origin or not Path(origin).resolve().is_relative_to(installed):
            raise ProbeFailure("dependency_boundary_failed")
    package = Path(str(files("sidera_agents"))).resolve()
    if not package.is_relative_to(installed):
        raise ProbeFailure("dependency_boundary_failed")
    config = ConfigParser(defaults={"here": str(package)})
    config.read_string((package / "alembic.ini").read_text(encoding="utf-8"))
    migrations = Path(config.get("alembic", "script_location")).resolve()
    if not migrations.is_relative_to(package) or any(
        not (migrations / relative).is_file() for relative in (
            "env.py", "versions/0001_agent_catalog.py", "versions/0002_runtime_catalog.py",
            "versions/0003_agent_runs_operations.py", "versions/0004_inputs_events.py",
        )
    ):
        raise ProbeFailure("dependency_boundary_failed")
    consumer = {"__name__": "installed_agents_consumer"}
    exec(compile(sys.argv[3], "standalone-public-consumer", "exec"), consumer)
    if consumer["main"]() != {
        "harness_id": "fixture-harness", "state": "completed",
        "messages": ["Fixture response."], "closed": True,
        "profile_status": {"enabled": False, "version": 2},
        "resource_status": {"enabled": False, "version": 2},
    }:
        raise ProbeFailure("probe_failed")
    if any(
        module == prefix or module.startswith(prefix + ".")
        for module in sys.modules for prefix in forbidden
    ):
        raise ProbeFailure("dependency_boundary_failed")

def _probe_control_plane_core():
    from fastapi.testclient import TestClient
    from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
    from sidera_control_plane.api.app import create_app
    from sidera_control_plane.application.worker_runtime import WorkerRuntimeResolver
    from sidera_control_plane.config import Settings
    from sidera_control_plane.gateway_worker import GatewayRuntime
    from sidera_control_plane.planning_worker import PlanningWorkerRuntime
    importlib.import_module("sidera_control_plane.application.ports")
    app = create_app(Settings(database_url="postgresql+psycopg://unused/unused"))
    paths = app.openapi().get("paths", {})
    if not isinstance(paths, dict) or not {
        "/healthz",
        "/api/v1/tasks",
        "/api/v1/commands",
    } <= paths.keys():
        raise ProbeFailure("probe_failed")

def _probe_control_plane_agents():
    from fastapi.testclient import TestClient
    from sidera_control_plane.composition.native_runtime import create_control_plane_runtime
    from sidera_control_plane.config import Settings

    forbidden = (
        "sidera_agents.postgres", "sidera_agents.harnesses.opencode",
        "sidera_agents.harnesses.acp_transport", "sidera_agents.harnesses.opencode_config",
        "sidera_agents.harnesses.opencode_descriptor",
        "sidera_control_plane.adapters.opencode_acp", "sidera_control_plane.adapters.opencode_planner",
        "sidera_control_plane.adapters.dsh_acp", "sidera_control_plane.adapters.acp_process",
    )
    if any(module == prefix or module.startswith(prefix + ".") for module in sys.modules for prefix in forbidden):
        raise ProbeFailure("dependency_boundary_failed")
    for distribution, modules in (
        ("sidera-control-plane", ("sidera_control_plane", "sidera_control_plane.composition.native_runtime")),
        ("sidera-agents", ("sidera_agents", "sidera_agents.api", "sidera_agents.contracts", "sidera_agents.ports")),
    ):
        installed = Path(metadata.distribution(distribution).locate_file("")).resolve()
        if not installed.is_relative_to(Path(sys.prefix).resolve()):
            raise ProbeFailure("dependency_boundary_failed")
        for name in modules:
            origin = getattr(importlib.import_module(name), "__file__", None)
            if not origin or not Path(origin).resolve().is_relative_to(installed):
                raise ProbeFailure("dependency_boundary_failed")
    package = Path(str(files("sidera_control_plane"))).resolve()
    installed = Path(metadata.distribution("sidera-control-plane").locate_file("")).resolve()
    if not package.is_relative_to(installed):
        raise ProbeFailure("dependency_boundary_failed")
    config = ConfigParser(defaults={"here": str(package)})
    config.read_string((package / "alembic.ini").read_text(encoding="utf-8"))
    migrations = Path(config.get("alembic", "script_location")).resolve()
    if not migrations.is_relative_to(package) or not (migrations / "env.py").is_file() or not tuple((migrations / "versions").glob("*.py")):
        raise ProbeFailure("dependency_boundary_failed")
    class UnusedPublicPorts:
        def __getattr__(self, name):
            raise ProbeFailure("probe_failed")
    ports = UnusedPublicPorts()
    runtime = create_control_plane_runtime(
        Settings(database_url="postgresql+psycopg://unused@/unused?host=/private/tmp/unused"),
        ports, ports, ports, ports,
    )
    try:
        reader = runtime.context_source_reader
        if reader is ports or reader is not runtime.context_source_reader:
            raise ProbeFailure("probe_failed")
        if not all(callable(getattr(runtime, name)) for name in ("start_background", "close", "migrate", "readiness", "configure_workspace")):
            raise ProbeFailure("probe_failed")
        with TestClient(runtime.app) as client:
            if client.get("/healthz").status_code != 200:
                raise ProbeFailure("probe_failed")
            if client.get("/api/v1/tasks").status_code != 401:
                raise ProbeFailure("probe_failed")
    finally:
        if runtime.close(grace_seconds=0) is not True:
            raise ProbeFailure("probe_failed")
    if any(module == prefix or module.startswith(prefix + ".") for module in sys.modules for prefix in forbidden):
        raise ProbeFailure("dependency_boundary_failed")

async def _probe_public_client():
    import httpx
    from uuid import UUID
    from sidera_control_plane_client import (
        ActorRef,
        CommandEnvelope,
        CommandResultUnknown,
        ControlPlaneClient,
        ControlPlaneClientConfig,
    )

    workspace_id = UUID("10000000-0000-0000-0000-000000000002")
    task_id = UUID("10000000-0000-0000-0000-000000000005")
    command_id = UUID("10000000-0000-0000-0000-000000000001")
    seen = []

    async def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params)))
        if request.method == "GET":
            if request.url.path.endswith("/active-plan"):
                return httpx.Response(200, json={"active_generation": 2})
            return httpx.Response(200, json={"id": str(task_id), "version": 4})
        raise httpx.ReadTimeout("mocked unknown result", request=request)

    config = ControlPlaneClientConfig(
        control_plane_url="http://127.0.0.1:60800",
        workspace_id=workspace_id,
        actor=ActorRef(type="integration", id="wheel-probe"),
    )
    client = ControlPlaneClient(config, transport=httpx.MockTransport(handler))
    envelope = CommandEnvelope(
        command_id=command_id,
        command_type="SubmitPlanningRequest",
        target_type="workspace",
        target_id=None,
        expected_version=None,
        idempotency_key="cfg1:wheel-probe",
        correlation_id=command_id,
        payload={},
    )
    try:
        task = await client.get_task(task_id)
        active_plan = await client.get_active_plan(task_id)
        try:
            await client.execute_command(envelope)
        except CommandResultUnknown as exc:
            if exc.command_id != command_id:
                raise ProbeFailure("probe_failed") from None
        else:
            raise ProbeFailure("probe_failed")
    finally:
        await client.close()
    if task.get("version") != 4 or active_plan.get("active_generation") != 2:
        raise ProbeFailure("probe_failed")
    expected_requests = [
        (
            "GET",
            f"/api/v1/tasks/{task_id}",
            {"workspace_id": str(workspace_id)},
        ),
        (
            "GET",
            f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}/active-plan",
            {},
        ),
        ("POST", "/api/v1/commands", {}),
    ]
    if seen != expected_requests:
        raise ProbeFailure("probe_failed")

def _probe_gateway_core():
    from fastapi.testclient import TestClient
    from uuid import UUID
    from sidera_main_agent_gateway.api.app import create_app
    from sidera_main_agent_gateway.config import Settings

    settings = Settings(
        adapter_kind="unconfigured",
        sidera_workspace_id=UUID("20000000-0000-0000-0000-000000000001"),
    )
    with TestClient(create_app(settings)) as client:
        health = client.get("/main-agent/v1/health")
        threads = client.get("/main-agent/v1/threads")
    if health.status_code != 200 or health.json().get("provider") != "unavailable":
        raise ProbeFailure("probe_failed")
    if threads.status_code != 503:
        raise ProbeFailure("probe_failed")

def _probe_bridge_client():
    import asyncio
    from uuid import UUID
    from sidera_control_plane_client import (
        CommandEnvelope as PublicCommandEnvelope,
        ControlPlaneClient as PublicControlPlaneClient,
    )
    from sidera_task_bridge.client import (
        CommandDispatcher,
        CommandEnvelope,
        ControlPlaneClient,
    )
    from sidera_task_bridge.config import TaskBridgeConfig

    if CommandEnvelope is not PublicCommandEnvelope:
        raise ProbeFailure("probe_failed")
    if not issubclass(ControlPlaneClient, PublicControlPlaneClient):
        raise ProbeFailure("probe_failed")
    config = TaskBridgeConfig(
        workspace_id=UUID("30000000-0000-0000-0000-000000000001"),
        planning_profile_id=UUID("30000000-0000-0000-0000-000000000002"),
        planning_profile_version=1,
    )
    client = ControlPlaneClient(config)
    asyncio.run(client.close())
    if not callable(CommandDispatcher):
        raise ProbeFailure("probe_failed")

def _probe_gateway_demo():
    from sidera_main_agent_gateway.adapters.demo.adapter import DemoMainAgentAdapter
    from sidera_main_agent_gateway.composition import build_main_agent_adapter
    from sidera_task_bridge.client import CommandDispatcher

    if not callable(DemoMainAgentAdapter) or not callable(build_main_agent_adapter):
        raise ProbeFailure("probe_failed")
    if not callable(CommandDispatcher):
        raise ProbeFailure("probe_failed")

def _run_probe():
    name = sys.argv[1]
    wheels = json.loads(sys.argv[2])
    if name not in PROJECTS or not isinstance(wheels, dict):
        raise ProbeFailure("probe_failed")
    for module in ABSENT_MODULES[name]:
        if _module_available(module):
            raise ProbeFailure("probe_failed")
    installed = {
        _normalized_distribution(dist.metadata.get("Name", ""))
        for dist in metadata.distributions()
        if dist.metadata.get("Name")
    }
    if any(_normalized_distribution(item) in installed for item in ABSENT_DISTRIBUTIONS[name]):
        raise ProbeFailure("probe_failed")
    packages = {}
    for package in PROJECTS[name]:
        dist = metadata.distribution(package)
        wheel_path = Path(wheels[package])
        record_match = wheel_record_matches(
            str(wheel_path), str(Path(dist.locate_file("")))
        )
        wheel_hash = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
        version = dist.version
        safe_version = (
            version
            if re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,8}){1,3}", version)
            else "invalid"
        )
        packages[package] = {
            "version": safe_version,
            "wheel_sha256": wheel_hash,
            "record_match": record_match,
        }
        if dist.version != "0.1.0" or not record_match:
            raise ProbeFailure("dependency_boundary_failed", packages)
    try:
        if name == "agents-core":
            _probe_agents_core()
        elif name == "control-plane-core":
            _probe_control_plane_core()
        elif name == "control-plane-agents":
            _probe_control_plane_agents()
        elif name == "public-client":
            asyncio.run(_probe_public_client())
        elif name == "gateway-core":
            _probe_gateway_core()
        elif name == "bridge-client":
            _probe_bridge_client()
        elif name == "gateway-demo":
            _probe_gateway_demo()
        else:
            raise ProbeFailure("probe_failed")
    except ProbeFailure as failure:
        raise ProbeFailure(failure.code, packages) from None
    return packages

try:
    _packages = _run_probe()
except ProbeFailure as _failure:
    print(json.dumps({"status": "FAIL", "code": _failure.code, "packages": _failure.packages}, separators=(",", ":")))
    sys.exit(1)
except Exception:
    print(json.dumps({"status": "FAIL", "code": "probe_failed", "packages": locals().get("packages", {})}, separators=(",", ":")))
    sys.exit(1)
print(json.dumps({"status": "PASS", "packages": _packages}, separators=(",", ":")))
"""
)


class CheckFailure(ValueError):
    def __init__(
        self,
        code: str,
        combination: str | None = None,
        packages: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.combination = combination
        self.packages = dict(packages or {})
        message = code if combination is None else f"{code}:{combination}"
        super().__init__(message)


def check_repository(repository: Path, projects: Sequence[str] | None = None) -> Path:
    resolved = Path(repository).resolve()
    selected = tuple(PROJECTS) if projects is None else projects
    if not resolved.is_dir() or any(
        not (resolved / PROJECTS[package] / "pyproject.toml").is_file()
        for package in selected
    ):
        raise CheckFailure("invalid_repository")
    return resolved


def check_output_root(output_root: Path, repository: Path) -> Path:
    requested = Path(output_root)
    resolved_repository = Path(repository).resolve()
    if requested.is_symlink():
        raise CheckFailure("output_root_refused")
    resolved = requested.resolve()
    if resolved == resolved_repository or resolved_repository in resolved.parents:
        raise CheckFailure("output_root_refused")
    if requested.exists() and (
        not requested.is_dir() or next(requested.iterdir(), None) is not None
    ):
        raise CheckFailure("output_root_refused")
    return resolved


def build_argv(repository: Path, project: str, output_root: Path) -> list[str]:
    if project not in PROJECTS:
        raise ValueError("unknown project")
    result = [
        "uv",
        "build",
        str(Path(repository) / PROJECTS[project]),
        "--wheel",
        "--out-dir",
        str(Path(output_root) / "wheels"),
        "--no-create-gitignore",
    ]
    if project == "sidera-agents":
        result.append("--offline")
    return result


def venv_argv(name: str, output_root: Path) -> list[str]:
    if name not in REQUIREMENTS:
        raise ValueError("unknown combination")
    return [
        "uv",
        "venv",
        str(Path(output_root) / f"{name}-venv"),
        "--python",
        "3.12",
    ]


def install_argv(name: str, output_root: Path) -> list[str]:
    if name not in REQUIREMENTS:
        raise ValueError("unknown combination")
    python = Path(output_root) / f"{name}-venv" / "bin" / "python"
    result = [
        "uv",
        "pip",
        "install",
        "--python",
        str(python),
        "--find-links",
        str(Path(output_root) / "wheels"),
    ]
    if name == "agents-core":
        result.append("--offline")
    result.append(REQUIREMENTS[name])
    if name in {"control-plane-core", "control-plane-agents", "gateway-core"}:
        result.append("httpx>=0.28,<1")
    return result


def subprocess_environment(output_root: Path) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "TMPDIR", "LANG", "LC_ALL")
        if key in os.environ
    }
    environment.setdefault("PATH", "/usr/bin:/bin")
    environment["UV_CACHE_DIR"] = str(Path(output_root) / "uv-cache")
    environment["UV_NO_CONFIG"] = "1"
    return environment


def _run(
    argv: Sequence[str],
    output_root: Path,
    timeout: int,
    code: str,
    combination: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = subprocess_environment(output_root)
    if combination == "agents-core":
        environment.pop("UV_CACHE_DIR", None)
    try:
        return subprocess.run(
            list(argv),
            cwd=output_root,
            env=environment,
            timeout=timeout,
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CheckFailure(code, combination) from None


def _check_returncode(
    result: subprocess.CompletedProcess[str],
    code: str,
    combination: str | None = None,
) -> None:
    if result.returncode != 0:
        raise CheckFailure(code, combination)


def _wheel_name_prefix(package: str) -> str:
    return re.sub(r"[-_.]+", "_", package).lower() + "-"


def build_wheels(
    repository: Path, output_root: Path, projects: Sequence[str] | None = None
) -> dict[str, Path]:
    wheel_dir = Path(output_root) / "wheels"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    built = {}
    selected = tuple(PROJECTS) if projects is None else projects
    for package in selected:
        combination = "agents-core" if package == "sidera-agents" else None
        result = _run(
            build_argv(repository, package, output_root),
            output_root,
            300,
            "build_failed",
            combination,
        )
        _check_returncode(result, "build_failed")
        matches = sorted(
            path
            for path in wheel_dir.glob("*.whl")
            if path.name.lower().startswith(_wheel_name_prefix(package))
        )
        if len(matches) != 1:
            raise CheckFailure("build_failed")
        built[package] = matches[0]
    return built


def source_fingerprints(
    repository: Path, projects: Sequence[str] | None = None
) -> dict[str, str]:
    root = Path(repository)
    fingerprints = {}
    selected = tuple(PROJECTS) if projects is None else projects
    for package in selected:
        project = root / PROJECTS[package]
        digest = hashlib.sha256()
        digest.update(b"pyproject.toml\0")
        digest.update((project / "pyproject.toml").read_bytes())
        source_root = root / SOURCE_ROOTS[package]
        if source_root.is_dir():
            for path in sorted(source_root.rglob("*.py")):
                if (
                    path.is_symlink()
                    or "__pycache__" in path.parts
                    or not path.is_file()
                ):
                    continue
                relative_source = path.relative_to(source_root).as_posix()
                digest.update(b"\0")
                digest.update(relative_source.encode("utf-8"))
                digest.update(b"\0")
                digest.update(path.read_bytes())
        fingerprints[package] = digest.hexdigest()
    return fingerprints


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wheel_version(path: Path, package: str) -> str:
    prefix = _wheel_name_prefix(package)
    if not path.name.lower().startswith(prefix):
        raise CheckFailure("build_failed")
    version = path.name[len(prefix) :].split("-", 1)[0]
    if version != "0.1.0":
        raise CheckFailure("build_failed")
    return version


def _probe_argv(
    name: str,
    wheels: Mapping[str, Path],
    output_root: Path,
    repository: Path | None = None,
) -> list[str]:
    python = Path(output_root) / f"{name}-venv" / "bin" / "python"
    selected = {package: str(wheels[package]) for package in COMBINATION_PROJECTS[name]}
    result = [
        str(python),
        "-I",
        "-c",
        PROBE_SOURCE,
        name,
        json.dumps(selected, sort_keys=True, separators=(",", ":")),
    ]
    if name == "agents-core":
        if repository is None:
            raise CheckFailure("invalid_repository", name)
        example = repository / PROJECTS["sidera-agents"] / "examples" / "standalone.py"
        result.append(example.read_text(encoding="utf-8"))
    return result


def _safe_probe_packages(
    name: str, wheels: Mapping[str, Path], payload: object
) -> dict[str, dict[str, object]]:
    if not isinstance(payload, dict):
        return {}
    reported = payload.get("packages")
    if not isinstance(reported, dict):
        return {}
    safe = {}
    for package in COMBINATION_PROJECTS[name]:
        item = reported.get(package)
        if not isinstance(item, dict):
            continue
        version = item.get("version")
        safe_version = (
            version
            if isinstance(version, str)
            and re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,8}){1,3}", version)
            else "invalid"
        )
        record_match = item.get("record_match")
        safe[package] = {
            "version": safe_version,
            "wheel_sha256": _file_sha256(wheels[package]),
            "record_match": record_match if isinstance(record_match, bool) else None,
        }
    return safe


def check_combination(
    name: str,
    wheels: Mapping[str, Path],
    output_root: Path,
    repository: Path | None = None,
) -> dict[str, object]:
    if name not in REQUIREMENTS:
        raise ValueError("unknown combination")
    for argv in (venv_argv(name, output_root), install_argv(name, output_root)):
        result = _run(argv, output_root, 300, "install_failed", name)
        _check_returncode(result, "install_failed", name)
    result = _run(
        _probe_argv(name, wheels, output_root, repository),
        output_root,
        30,
        "probe_failed",
        name,
    )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise CheckFailure("probe_failed", name) from None
    safe_packages = _safe_probe_packages(name, wheels, payload)
    if not isinstance(payload, dict):
        raise CheckFailure("probe_failed", name)
    code = payload.get("code")
    if result.returncode != 0 or payload.get("status") != "PASS":
        failure_code = (
            "dependency_boundary_failed"
            if code == "dependency_boundary_failed"
            else "probe_failed"
        )
        raise CheckFailure(failure_code, name, safe_packages)
    if set(safe_packages) != set(COMBINATION_PROJECTS[name]) or any(
        item.get("version") != "0.1.0" or item.get("record_match") is not True
        for item in safe_packages.values()
    ):
        raise CheckFailure("dependency_boundary_failed", name, safe_packages)
    return {"status": "PASS", "packages": safe_packages}


def _initial_report(output_root: str) -> dict[str, object]:
    return {
        "status": "FAIL",
        "combinations": {name: {"status": "NOT_RUN"} for name in REQUIREMENTS},
        "provider_calls": 0,
        "output_root": output_root,
    }


def _write_evidence(output_root: Path | None, report: dict[str, object]) -> bool:
    if output_root is None:
        return True
    try:
        (output_root / "evidence.json").write_text(
            json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="ascii",
        )
    except OSError:
        report["status"] = "FAIL"
        report["code"] = "output_root_refused"
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--combination", choices=tuple(REQUIREMENTS))
    args = parser.parse_args(argv)
    requested_output = str(args.output_root)
    report = _initial_report(requested_output)
    output_root: Path | None = None
    selected = (args.combination,) if args.combination else tuple(REQUIREMENTS)
    selected_projects = tuple(
        dict.fromkeys(
            package for name in selected for package in COMBINATION_PROJECTS[name]
        )
    )
    try:
        repository = check_repository(Path(args.repository), selected_projects)
        output_root = check_output_root(Path(args.output_root), repository)
        output_root.mkdir(parents=True, exist_ok=True)
        report["output_root"] = str(output_root)
        try:
            fingerprints = source_fingerprints(repository, selected_projects)
        except (OSError, UnicodeError):
            raise CheckFailure("invalid_repository") from None
        combinations = report["combinations"]
        assert isinstance(combinations, dict)
        for name in selected:
            packages = COMBINATION_PROJECTS[name]
            combinations[name]["source_fingerprint"] = {
                package: fingerprints[package] for package in packages
            }
        wheels = build_wheels(repository, output_root, selected_projects)
        for name in selected:
            packages = COMBINATION_PROJECTS[name]
            combinations[name]["packages"] = {
                package: {
                    "version": _wheel_version(wheels[package], package),
                    "wheel_sha256": _file_sha256(wheels[package]),
                    "record_match": None,
                }
                for package in packages
            }
        first_failure = None
        for name in selected:
            try:
                result = check_combination(name, wheels, output_root, repository)
            except CheckFailure as failure:
                combinations[name]["status"] = "FAIL"
                combinations[name]["code"] = failure.code
                if failure.packages:
                    combinations[name]["packages"] = failure.packages
                if first_failure is None:
                    first_failure = failure.code
            except (OSError, ValueError):
                combinations[name]["status"] = "FAIL"
                combinations[name]["code"] = "probe_failed"
                if first_failure is None:
                    first_failure = "probe_failed"
            else:
                combinations[name].update(result)
        if first_failure is not None:
            report["status"] = "FAIL"
            report["code"] = first_failure
            exit_code = 1
        elif all(combinations[name]["status"] == "PASS" for name in selected):
            report["status"] = "PASS"
            exit_code = 0
        else:
            report["status"] = "PARTIAL"
            exit_code = 0
    except CheckFailure as failure:
        report["status"] = "FAIL"
        report["code"] = failure.code
        if failure.combination:
            combinations = report["combinations"]
            assert isinstance(combinations, dict)
            combinations[failure.combination]["status"] = "FAIL"
            combinations[failure.combination]["code"] = failure.code
        exit_code = 1
    except OSError:
        report["status"] = "FAIL"
        report["code"] = "output_root_refused" if output_root else "invalid_repository"
        exit_code = 1
    if not _write_evidence(output_root, report):
        exit_code = 1
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
