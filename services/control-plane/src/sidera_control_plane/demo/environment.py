from __future__ import annotations

import json
import os
import secrets
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit

from sidera_control_plane.adapters.git_workspace import opaque_repository_ref

from .contracts import DemoManifest
from .database import database_url_for

_INHERITED_ENVIRONMENT = frozenset({"PATH", "LANG", "LC_ALL", "TMPDIR"})
_TOKEN_KEYS = (
    "knowledge_local_owner",
    "knowledge_main_agent",
    "knowledge_control_plane",
    "web_knowledge",
)


class DemoTokens(TypedDict):
    knowledge_local_owner: str
    knowledge_main_agent: str
    knowledge_control_plane: str
    web_knowledge: str


def child_environment(
    inherited: dict[str, str], explicit: dict[str, str]
) -> dict[str, str]:
    result = {
        key: value for key, value in inherited.items() if key in _INHERITED_ENVIRONMENT
    }
    result.update(explicit)
    return result


def allocate_local_ports(count: int = 4) -> tuple[int, ...]:
    if count < 1 or count > 32:
        raise ValueError("port_count_out_of_range")
    listeners: list[socket.socket] = []
    try:
        for _ in range(count):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listeners.append(listener)
            listener.bind(("127.0.0.1", 0))
        return tuple(int(listener.getsockname()[1]) for listener in listeners)
    finally:
        for listener in listeners:
            listener.close()


def component_environment(
    manifest: DemoManifest,
    component: str,
    tokens: Mapping[str, str] | DemoTokens,
    inherited: Mapping[str, str],
    *,
    pgpassfile: Path | None = None,
) -> dict[str, str]:
    required_tokens = {
        "knowledge_local_owner",
        "knowledge_main_agent",
        "knowledge_control_plane",
        "web_knowledge",
    }
    if set(tokens) != required_tokens:
        raise ValueError("demo_tokens_invalid")
    run_root = str(manifest.run_root)
    manifest_path = runtime_root_for(manifest) / "manifest.json"
    control_environment = {
        "SIDERA_DATABASE_URL": database_url_for(manifest),
        "SIDERA_INTERVIEW_DEMO_MANIFEST": str(manifest_path),
        "SIDERA_WEB_ORIGIN": manifest.endpoints.web,
        "SIDERA_KNOWLEDGE_BASE_URL": manifest.endpoints.knowledge,
        "SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN": tokens[
            "knowledge_control_plane"
        ],
        "SIDERA_CONTEXT_PACKAGE_CUTOVER_AT": "2026-08-27T00:00:00+08:00",
        "SIDERA_COLLABORATION_GOVERNANCE_ENABLED": "true",
        "SIDERA_COLLABORATION_WORKSPACE_ID": str(manifest.workspace_id),
        "SIDERA_RESULT_INPUTS_ENABLED": "true",
        "SIDERA_RESULT_INTEGRATION_ENABLED": "true",
        "SIDERA_NOTIFICATION_ENABLED": "true",
        "SIDERA_NOTIFICATION_BASELINE_REQUIRED": "true",
        "SIDERA_RESULT_APPLY_ON_ACCEPT": "false",
        "SIDERA_RESULT_REPOSITORY_REF": opaque_repository_ref(manifest.repository),
        "SIDERA_RESULT_REPOSITORY_PATH": str(manifest.repository),
        "SIDERA_RESULT_BASE_COMMIT": manifest.base_commit,
        "SIDERA_WORKSPACE_ROOT": f"{run_root}/workspaces",
        "SIDERA_ARTIFACT_ROOT": f"{run_root}/artifacts",
        "SIDERA_RESULT_STORE_ROOT": f"{run_root}/results",
        "SIDERA_PLANNING_TEMP_ROOT": f"{run_root}/planning",
        "SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR": f"{run_root}/candidate-spool",
        "SIDERA_MAIN_AGENT_ENTRY_URI": (
            f"{manifest.endpoints.web}/workspaces/{manifest.workspace_id}/chat"
        ),
        "SIDERA_LOCAL_ACTOR_TYPE": "local",
        "SIDERA_LOCAL_ACTOR_ID": "aitejiu",
        "SIDERA_API_BIND_HOST": "127.0.0.1",
        "SIDERA_RESTORE_READ_ONLY": (
            "true" if manifest.restore_mode == "read_only" else "false"
        ),
        "SIDERA_RESTORE_SCHEMA_VALIDATION": (
            "true" if manifest.restore_mode == "read_only" else "false"
        ),
    }
    if component in {
        "control-plane",
        "demo-worker-heartbeat",
        "planning-worker",
        "context-package-worker",
        "knowledge-candidate-worker",
        "demo-notification-sink",
        "activation-worker",
        "scheduler-worker",
        "gateway-worker",
        "input-dispatcher",
        "plan-transition-worker",
        "handoff-worker",
        "collaboration-worker",
        "notification-worker",
        "result-worker",
    }:
        explicit = control_environment
    elif component == "knowledge-service":
        explicit = {
            "SIDERA_KNOWLEDGE_DATABASE_URL": database_url_for(
                manifest, knowledge=True
            ),
            "SIDERA_KNOWLEDGE_WORKSPACE_ID": str(manifest.workspace_id),
            "SIDERA_KNOWLEDGE_HOST": "127.0.0.1",
            "SIDERA_KNOWLEDGE_PORT": str(_port(manifest.endpoints.knowledge)),
            "SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN": tokens[
                "knowledge_local_owner"
            ],
            "SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN": tokens["knowledge_main_agent"],
            "SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN": tokens[
                "knowledge_control_plane"
            ],
            "SIDERA_KNOWLEDGE_SPOOL_ROOT": f"{run_root}/knowledge-spool",
            "SIDERA_KNOWLEDGE_RESTORE_READ_ONLY": (
                "true" if manifest.restore_mode == "read_only" else "false"
            ),
            "SIDERA_KNOWLEDGE_RESTORE_SCHEMA_VALIDATION": (
                "true" if manifest.restore_mode == "read_only" else "false"
            ),
        }
    elif component == "main-agent-gateway":
        explicit = {
            "SIDERA_MAIN_AGENT_ADAPTER_KIND": "interview-demo",
            "SIDERA_MAIN_AGENT_DEMO_MANIFEST": str(manifest_path),
            "SIDERA_MAIN_AGENT_SIDERA_WORKSPACE_ID": str(manifest.workspace_id),
            "SIDERA_MAIN_AGENT_WEB_ORIGIN": manifest.endpoints.web,
            "SIDERA_MAIN_AGENT_HOST": "127.0.0.1",
            "SIDERA_MAIN_AGENT_PORT": str(_port(manifest.endpoints.main_agent)),
            "SIDERA_MAIN_AGENT_RESTORE_READ_ONLY": (
                "true" if manifest.restore_mode == "read_only" else "false"
            ),
        }
    elif component == "web":
        explicit = {
            "SIDERA_WEB_CONTROL_PLANE_PORT": str(
                _port(manifest.endpoints.control_plane)
            ),
            "SIDERA_WEB_KNOWLEDGE_SERVICE_PORT": str(
                _port(manifest.endpoints.knowledge)
            ),
            "SIDERA_WEB_MAIN_AGENT_PORT": str(
                _port(manifest.endpoints.main_agent)
            ),
            "SIDERA_WEB_PORT": str(_port(manifest.endpoints.web)),
            # The knowledge service exposes the local-owner capability to the
            # browser proxy; keep the proxy credential within that contract.
            "SIDERA_WEB_KNOWLEDGE_TOKEN": tokens["knowledge_local_owner"],
        }
    else:
        raise ValueError("unknown_demo_component")

    resolved = dict(explicit)
    if pgpassfile is not None and component in {
        "control-plane",
        "knowledge-service",
        "demo-worker-heartbeat",
        "planning-worker",
        "context-package-worker",
        "knowledge-candidate-worker",
        "demo-notification-sink",
        "activation-worker",
        "scheduler-worker",
        "gateway-worker",
        "input-dispatcher",
        "plan-transition-worker",
        "handoff-worker",
        "collaboration-worker",
        "notification-worker",
        "result-worker",
    }:
        pgpassfile = pgpassfile.expanduser()
        if not pgpassfile.is_absolute() or not pgpassfile.is_file():
            raise ValueError("selected_pgpassfile_must_be_an_existing_absolute_file")
        resolved["PGPASSFILE"] = str(pgpassfile)
    return child_environment(dict(inherited), resolved)


def runtime_root_for(manifest: DemoManifest) -> Path:
    return manifest.runtime_root or manifest.run_root


def create_demo_tokens(run_root: Path) -> DemoTokens:
    if not run_root.is_absolute():
        raise ValueError("run_root_must_be_absolute")
    run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(run_root, 0o700)
    path = run_root / "tokens.json"
    tokens = DemoTokens(
        knowledge_local_owner=secrets.token_urlsafe(32),
        knowledge_main_agent=secrets.token_urlsafe(32),
        knowledge_control_plane=secrets.token_urlsafe(32),
        web_knowledge=secrets.token_urlsafe(32),
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        json.dump(tokens, target, sort_keys=True, separators=(",", ":"))
        target.flush()
        os.fsync(target.fileno())
    directory_fd = os.open(run_root, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return tokens


def load_demo_tokens(run_root: Path) -> DemoTokens:
    path = run_root / "tokens.json"
    try:
        if path.stat().st_mode & 0o077:
            raise ValueError("demo_tokens_permissions_invalid")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("demo_tokens_unreadable") from exc
    if not isinstance(raw, dict) or set(raw) != set(_TOKEN_KEYS):
        raise ValueError("demo_tokens_invalid")
    if any(not isinstance(raw[key], str) or len(raw[key]) < 32 for key in _TOKEN_KEYS):
        raise ValueError("demo_tokens_invalid")
    return DemoTokens(**raw)


def _port(endpoint: str) -> int:
    port = urlsplit(endpoint).port
    if port is None:
        raise ValueError("manifest_endpoint_port_missing")
    return port
