from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Literal
from uuid import UUID

from .contracts import DemoManifest
from .environment import runtime_root_for
from .faults import DemoProcessCrash, consume_fault_once
from .http_client import DemoClient, DemoClientError, DemoClientNotFound
from .state import DemoStateStore, OperationRecord

_MARKER_SCHEMA_VERSION = 1
_SHA256_LENGTH = 64
RecoveryMarkerKind = Literal["file"]


def write_recovery_marker(
    manifest: DemoManifest,
    operation_id: UUID,
    *,
    kind: RecoveryMarkerKind,
    path: Path,
    content_hash: str,
    result: dict[str, object],
) -> None:
    """Persist an effect marker only after the external file is durable."""

    if not isinstance(operation_id, UUID):
        raise TypeError("recovery marker operation_id must be a UUID")
    if kind != "file":
        raise ValueError("unsupported_recovery_marker_kind")
    target = _safe_run_path(manifest, path, must_exist=True)
    if not _is_sha256(content_hash):
        raise ValueError("recovery marker content hash is invalid")
    if _sha256_file(target) != content_hash:
        raise ValueError("recovery marker content hash does not match file")
    if not isinstance(result, dict):
        raise TypeError("recovery marker result must be an object")

    marker_root = runtime_root_for(manifest) / "recovery" / "markers"
    marker_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(marker_root, 0o700)
    marker_path = marker_root / f"{operation_id}.json"
    document = {
        "schema_version": _MARKER_SCHEMA_VERSION,
        "run_id": str(manifest.run_id),
        "operation_id": str(operation_id),
        "kind": kind,
        "path": str(target),
        "content_hash": content_hash,
        "result": dict(result),
    }
    _write_json(marker_path, document, marker_root)


def reconcile_demo_operations(
    manifest: DemoManifest,
) -> tuple[OperationRecord, ...]:
    """Reconcile only operations backed by an authoritative durable fact.

    A missing response is not treated as success. HTTP commands are resolved
    through their public command-status endpoint; local input and file effects
    require their matching persisted event/output or a verified marker.
    """

    recovered: list[OperationRecord] = []
    for state in _state_stores(manifest):
        for key, record in state.list_operations():
            if record.status not in {"pending", "unknown"}:
                recovered.append(record)
                continue
            settled = _reconcile_operation(manifest, state, key, record)
            recovered.append(settled)
    return tuple(
        sorted(
            recovered,
            key=lambda item: (str(item.operation_id), item.status),
        )
    )


def crash_after_candidate_publish(
    manifest: DemoManifest,
    candidate_id: UUID,
) -> None:
    """Crash once after a candidate identity is durably published."""

    if not isinstance(candidate_id, UUID):
        raise TypeError("candidate_id must be a UUID")
    if consume_fault_once(manifest, "after-candidate-publish", candidate_id):
        raise DemoProcessCrash("after-candidate-publish")


def _reconcile_operation(
    manifest: DemoManifest,
    state: DemoStateStore,
    key: str,
    record: OperationRecord,
) -> OperationRecord:
    if key.startswith("heartbeat:"):
        return _reconcile_command(manifest, state, key, record)
    if key.startswith("input/"):
        return _reconcile_input(state, key, record)
    if key.startswith("file-effect/"):
        return _reconcile_file_effect(manifest, state, key, record)
    if key.startswith("candidate-effect/"):
        return _settle_unknown(
            state,
            key,
            record,
            "candidate_identity_requires_authoritative_query",
        )
    return _settle_unknown(state, key, record, "no_recovery_evidence")


def _reconcile_command(
    manifest: DemoManifest,
    state: DemoStateStore,
    key: str,
    record: OperationRecord,
) -> OperationRecord:
    client = DemoClient(manifest)
    try:
        status = client.get(
            f"/api/v1/workspaces/{manifest.workspace_id}/commands/{record.operation_id}"
        )
    except (DemoClientError, DemoClientNotFound):
        return _settle_unknown(state, key, record, "command_status_unavailable")
    finally:
        client.close()
    if not isinstance(status, dict) or status.get("command_id") != str(
        record.operation_id
    ):
        return _settle_unknown(state, key, record, "command_status_identity_mismatch")
    command_status = status.get("status")
    if command_status == "applied":
        result = status.get("result")
        if not isinstance(result, dict) or result.get("command_id") != str(
            record.operation_id
        ):
            return _settle_unknown(state, key, record, "command_result_unavailable")
        return state.settle(key, record.operation_id, "applied", dict(result))
    if command_status == "rejected":
        return state.settle(
            key,
            record.operation_id,
            "rejected",
            {"status": "rejected"},
        )
    return _settle_unknown(state, key, record, "command_not_terminal")


def _reconcile_input(
    state: DemoStateStore,
    key: str,
    record: OperationRecord,
) -> OperationRecord:
    parsed = key.split("/")
    if len(parsed) != 3:
        return _settle_unknown(state, key, record, "input_key_invalid")
    try:
        session_id = UUID(parsed[1])
        operation_id = UUID(parsed[2])
    except ValueError:
        return _settle_unknown(state, key, record, "input_key_invalid")
    if operation_id != record.operation_id:
        return _settle_unknown(state, key, record, "input_operation_identity_mismatch")

    accepted_sequence: int | None = None
    for event in state.events_after(session_id, 0):
        if event.event_type != "input.accepted":
            continue
        if event.payload.get("input_operation_id") == str(operation_id):
            accepted_sequence = event.sequence
            break
    if accepted_sequence is None:
        return _settle_unknown(state, key, record, "input_effect_not_observed")

    output = state.read(f"worker-output/{session_id}/{operation_id}")
    if output is None:
        return _settle_unknown(
            state, key, record, "input_persisted_without_output"
        )
    output_record = OperationRecord.from_dict(output)
    if output_record.status != "applied":
        return _settle_unknown(
            state, key, record, "input_persisted_without_output"
        )
    content_hash = output_record.result.get("content_hash")
    if not isinstance(content_hash, str) or not _is_sha256(content_hash):
        return _settle_unknown(state, key, record, "input_output_identity_invalid")
    return state.settle(
        key,
        record.operation_id,
        "applied",
        {
            "outcome": "delivered",
            "accepted_sequence": accepted_sequence,
            "content_hash": content_hash,
        },
    )


def _reconcile_file_effect(
    manifest: DemoManifest,
    state: DemoStateStore,
    key: str,
    record: OperationRecord,
) -> OperationRecord:
    expected_operation_id = key.removeprefix("file-effect/")
    if expected_operation_id != str(record.operation_id):
        return _settle_unknown(state, key, record, "file_operation_identity_mismatch")
    marker = _read_marker(manifest, record.operation_id)
    if marker is None:
        return _settle_unknown(state, key, record, "file_marker_missing")
    if marker.get("kind") != "file":
        return _settle_unknown(state, key, record, "file_marker_kind_invalid")
    raw_path = marker.get("path")
    raw_hash = marker.get("content_hash")
    result = marker.get("result")
    if not isinstance(raw_path, str) or not isinstance(raw_hash, str) or not isinstance(
        result, dict
    ):
        return _settle_unknown(state, key, record, "file_marker_invalid")
    try:
        path = _safe_run_path(manifest, Path(raw_path), must_exist=True)
    except (OSError, ValueError):
        return _settle_unknown(state, key, record, "file_marker_path_invalid")
    if not _is_sha256(raw_hash) or _sha256_file(path) != raw_hash:
        return _settle_unknown(state, key, record, "file_marker_hash_mismatch")
    marker_result_hash = result.get("content_hash")
    if marker_result_hash is not None and marker_result_hash != raw_hash:
        return _settle_unknown(state, key, record, "file_marker_result_mismatch")
    return state.settle(key, record.operation_id, "applied", dict(result))


def _settle_unknown(
    state: DemoStateStore,
    key: str,
    record: OperationRecord,
    reason: str,
) -> OperationRecord:
    return state.settle(
        key,
        record.operation_id,
        "unknown",
        {"reason": reason},
    )


def _state_stores(manifest: DemoManifest) -> tuple[DemoStateStore, ...]:
    roots: list[Path] = []
    root = runtime_root_for(manifest) / "state"
    if (root / "operations.json").is_file():
        roots.append(root)
    worker_root = runtime_root_for(manifest) / "worker-state"
    if worker_root.is_dir():
        roots.extend(
            path
            for path in sorted(worker_root.iterdir())
            if path.is_dir() and (path / "operations.json").is_file()
        )
    return tuple(DemoStateStore(path, manifest.run_id) for path in roots)


def _read_marker(manifest: DemoManifest, operation_id: UUID) -> dict[str, object] | None:
    path = runtime_root_for(manifest) / "recovery" / "markers" / f"{operation_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if (
        value.get("schema_version") != _MARKER_SCHEMA_VERSION
        or value.get("run_id") != str(manifest.run_id)
        or value.get("operation_id") != str(operation_id)
    ):
        return None
    return value


def _safe_run_path(
    manifest: DemoManifest,
    path: Path,
    *,
    must_exist: bool,
) -> Path:
    raw_run_root = manifest.run_root.expanduser()
    if raw_run_root.is_symlink():
        raise ValueError("recovery run root cannot be a symlink")
    run_root = raw_run_root.resolve(strict=True)
    raw_path = path.expanduser()
    if not raw_path.is_absolute():
        raise ValueError("recovery path must be absolute")
    if raw_path.is_symlink():
        raise ValueError("recovery path cannot be a symlink")
    resolved = raw_path.resolve(strict=must_exist)
    try:
        resolved.relative_to(run_root)
    except ValueError as exc:
        raise ValueError("recovery path is outside run root") from exc
    if must_exist and (not resolved.is_file() or resolved.is_symlink()):
        raise ValueError("recovery path must be a regular file")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _write_json(path: Path, value: dict[str, object], directory: Path) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(value, temporary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


__all__ = [
    "RecoveryMarkerKind",
    "crash_after_candidate_publish",
    "reconcile_demo_operations",
    "write_recovery_marker",
]
