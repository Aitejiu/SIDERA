from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import DemoManifest
from .environment import runtime_root_for
from .manifest import DemoConfigurationError, validate_manifest

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_ENDPOINT_NAMES = ("control_plane", "knowledge", "main_agent", "web")
_FORBIDDEN_ENVIRONMENT_KEYS = frozenset(
    {
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    }
)


def assert_demo_endpoint(url: str, manifest: DemoManifest) -> None:
    """Allow only an exact, registered local origin from one demo manifest."""
    if not isinstance(url, str):
        raise TypeError("demo_endpoint_must_be_text")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("demo_endpoint_invalid") from exc
    registered = {
        getattr(manifest.endpoints, name) for name in _ENDPOINT_NAMES
    }
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port is None
        or parsed.netloc != f"127.0.0.1:{port}"
        or url not in registered
    ):
        raise ValueError("demo_endpoint_not_registered")


def audit_demo_environment(environment: Mapping[str, str]) -> tuple[str, ...]:
    """Return inherited credential/proxy keys that must never enter a run."""
    return tuple(
        f"forbidden_environment:{key}"
        for key in sorted(_FORBIDDEN_ENVIRONMENT_KEYS.intersection(environment))
    )


def audit_demo_configuration(manifest: DemoManifest) -> tuple[str, ...]:
    """Return fail-closed configuration violations for an interview run."""
    violations: list[str] = []
    try:
        validate_manifest(manifest, _PROJECT_ROOT)
    except (DemoConfigurationError, ValueError):
        violations.append("manifest_invalid")
        return tuple(violations)

    for name in _ENDPOINT_NAMES:
        try:
            assert_demo_endpoint(getattr(manifest.endpoints, name), manifest)
        except ValueError:
            violations.append(f"endpoint_invalid:{name}")

    run_root = manifest.run_root.expanduser()
    runtime_root = runtime_root_for(manifest).expanduser()
    if run_root.is_symlink():
        violations.append("run_root_symlink")
    if runtime_root.is_symlink():
        violations.append("runtime_root_symlink")
    if run_root.exists() and (run_root.stat().st_mode & 0o077):
        violations.append("run_root_permissions_invalid")
    if runtime_root.exists() and (runtime_root.stat().st_mode & 0o077):
        violations.append("runtime_root_permissions_invalid")
    token_path = runtime_root / "tokens.json"
    if token_path.exists() and (
        token_path.is_symlink()
        or not token_path.is_file()
        or token_path.stat().st_mode & 0o077
    ):
        violations.append("token_file_permissions_invalid")
    if manifest.pg_host != "127.0.0.1":
        violations.append("database_host_not_loopback")
    return tuple(dict.fromkeys(violations))


def assert_safe_child_environment(environment: Mapping[str, str]) -> None:
    violations = audit_demo_environment(environment)
    if violations:
        raise ValueError(";".join(violations))


def is_current_process(pid: int) -> bool:
    """Small platform-neutral probe used by safety tests and diagnostics."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True
