from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from .contracts import DemoEndpoints, DemoManifest

_DATABASE_NAME = re.compile(r"^sidera_interview_test_([0-9a-f]{32})$")
_KNOWLEDGE_DATABASE_NAME = re.compile(
    r"^sidera_interview_knowledge_([0-9a-f]{32})$"
)
_ENDPOINT_NAMES = ("control_plane", "knowledge", "main_agent", "web")


class DemoConfigurationError(ValueError):
    """Raised when an interview demo manifest cannot be trusted locally."""


def stable_id(run_id: UUID, key: str) -> UUID:
    if not key or key.strip() != key:
        raise ValueError("invalid_resource_key")
    return uuid5(run_id, key)


def load_manifest(path: Path) -> DemoManifest:
    if not path.is_absolute():
        raise DemoConfigurationError("manifest_path_must_be_absolute")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DemoConfigurationError("manifest_unreadable") from exc
    return DemoManifest.model_validate(payload)


def validate_manifest(manifest: DemoManifest, sidera_root: Path) -> None:
    _validate_endpoints(manifest.endpoints)
    _validate_database_names(manifest)
    _validate_paths(manifest, sidera_root)


def _validate_endpoints(endpoints: DemoEndpoints) -> None:
    ports: list[int] = []
    for name in _ENDPOINT_NAMES:
        value = getattr(endpoints, name)
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise DemoConfigurationError(f"invalid_endpoint:{name}") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or port is None
            or not 1024 <= port <= 65535
            or parsed.netloc != f"127.0.0.1:{port}"
        ):
            raise DemoConfigurationError(f"invalid_endpoint:{name}")
        ports.append(port)
    if len(set(ports)) != len(ports):
        raise DemoConfigurationError("duplicate_endpoint_port")


def _validate_database_names(manifest: DemoManifest) -> None:
    database_match = _DATABASE_NAME.fullmatch(manifest.database_name)
    knowledge_match = _KNOWLEDGE_DATABASE_NAME.fullmatch(
        manifest.knowledge_database_name
    )
    if database_match is None or database_match.group(1) != manifest.run_id.hex:
        raise DemoConfigurationError("database_name_does_not_match_run")
    if knowledge_match is None or knowledge_match.group(1) != manifest.run_id.hex:
        raise DemoConfigurationError("knowledge_database_name_does_not_match_run")


def _validate_paths(manifest: DemoManifest, sidera_root: Path) -> None:
    root = sidera_root.expanduser().resolve(strict=False)
    run_root = manifest.run_root.expanduser().resolve(strict=False)
    repository = manifest.repository.expanduser().resolve(strict=False)
    storage_root = (root / ".sidera" / "interview-demo").resolve(strict=False)

    if manifest.restore_mode == "read_only":
        _validate_restore_paths(root, run_root, repository, manifest.runtime_root)
        return

    if manifest.runtime_root is not None:
        raise DemoConfigurationError("runtime_root_requires_restore_mode")

    if not _is_within(storage_root, root):
        raise DemoConfigurationError("path_inside_sidera")
    if run_root != storage_root / str(manifest.run_id):
        raise DemoConfigurationError("invalid_demo_run_root")
    if _is_within(repository, root) or _is_within(root, repository):
        raise DemoConfigurationError("path_inside_sidera")
    if _is_within(repository, run_root) or _is_within(run_root, repository):
        raise DemoConfigurationError("repository_overlaps_run_root")


def _validate_restore_paths(
    sidera_root: Path,
    run_root: Path,
    repository: Path,
    runtime_root: Path | None,
) -> None:
    resolved_runtime = (runtime_root or run_root).expanduser().resolve(strict=False)
    home = Path.home().resolve(strict=False)
    if (
        _is_within(run_root, sidera_root)
        or _is_within(sidera_root, run_root)
        or _is_within(resolved_runtime, sidera_root)
        or _is_within(sidera_root, resolved_runtime)
        or run_root == home
        or resolved_runtime == home
    ):
        raise DemoConfigurationError("restore_path_inside_sidera")
    if resolved_runtime == run_root or _is_within(resolved_runtime, run_root) or _is_within(
        run_root, resolved_runtime
    ):
        raise DemoConfigurationError("restore_runtime_must_be_separate")
    if _is_within(repository, sidera_root) or _is_within(sidera_root, repository):
        raise DemoConfigurationError("path_inside_sidera")
    if repository == run_root or _is_within(run_root, repository):
        raise DemoConfigurationError("repository_overlaps_restore_root")


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents
