"""Write the private, identity-bound maintenance inventory for one run."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

INVENTORY_SCHEMA = 1


def write_inventory(
    path: Path,
    *,
    run_id: str,
    workspace_id: str,
    code_commit: str,
    implementation_fingerprint: str,
    runtime_fingerprint: str,
    components: list[dict[str, Any]],
    unsupported_component_ids: list[str],
    source_identities: dict[str, str] | None = None,
    writer_status: str = "unknown",
    active_component_ids: list[str] | None = None,
) -> Path:
    """Atomically write an inventory without claiming unsupported coverage."""

    component_ids = [str(component["component_id"]) for component in components]
    payload: dict[str, Any] = {
        "schema_version": INVENTORY_SCHEMA,
        "run_id": run_id,
        "workspace_id": workspace_id,
        "code_commit": code_commit,
        "implementation_fingerprint": implementation_fingerprint,
        "runtime_fingerprint": runtime_fingerprint,
        "writers": {
            "status": writer_status,
            "active_component_ids": list(active_component_ids or []),
            "observed_at": datetime.now(UTC).isoformat(),
        },
        "persistence": {
            "status": "complete" if not unsupported_component_ids else "incomplete",
            "component_ids": component_ids,
            "unsupported_component_ids": list(unsupported_component_ids),
        },
        "components": components,
    }
    if source_identities is not None:
        payload["source_identities"] = dict(source_identities)

    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("inventory_path_must_be_absolute")
    path.parent.mkdir(parents=True, exist_ok=True, mode=stat.S_IRWXU)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return path


def update_writer_state(
    path: Path,
    *,
    status: str,
    active_component_ids: list[str],
) -> Path:
    """Record a supervisor-owned writer transition without changing identity."""

    path = path.expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("inventory_path_invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("inventory_invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != INVENTORY_SCHEMA:
        raise ValueError("inventory_invalid")
    payload["writers"] = {
        "status": status,
        "active_component_ids": list(active_component_ids),
        "observed_at": datetime.now(UTC).isoformat(),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return path


__all__ = ["INVENTORY_SCHEMA", "update_writer_state", "write_inventory"]
