"""Immutable acceptance-batch identities for the interview demo."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

BATCH_SCHEMA_VERSION = 2
_BATCH_KEYS = frozenset(
    {
        "schema_version",
        "batch_id",
        "suite",
        "scenario",
        "run_id",
        "workspace_id",
        "implementation_fingerprint",
        "fingerprint_version",
        "code_commit",
        "catalog_fingerprint",
        "created_at",
        "expected",
    }
)
_ENTRY_KEYS = frozenset(
    {
        "schema_version",
        "batch_id",
        "suite",
        "scenario",
        "stable_test_key",
        "project",
        "viewport",
        "check_id",
        "status",
        "attempt",
        "evidence_kind",
    }
)
_STATUSES = {"NOT_RUN", "PASS", "FAIL", "INCOMPLETE"}
_EVIDENCE_KINDS = {"simulated_interview", "real_local_verification"}


class AcceptanceBatchError(ValueError):
    """An acceptance batch is malformed or has an ambiguous identity."""


def build_entry(
    *,
    batch_id: UUID,
    suite: str,
    scenario: str,
    stable_test_key: str,
    project: str,
    viewport: Mapping[str, int] | None,
    check_id: str,
    status: str = "NOT_RUN",
    attempt: int = 1,
    evidence_kind: str = "simulated_interview",
) -> dict[str, object]:
    if not suite.strip() or not scenario.strip() or not stable_test_key.strip():
        raise AcceptanceBatchError("acceptance_entry_identity_required")
    if project != "none" and not project.strip():
        raise AcceptanceBatchError("acceptance_entry_project_invalid")
    if status not in _STATUSES:
        raise AcceptanceBatchError("acceptance_entry_status_invalid")
    if evidence_kind not in _EVIDENCE_KINDS:
        raise AcceptanceBatchError("acceptance_entry_evidence_kind_invalid")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise AcceptanceBatchError("acceptance_entry_attempt_invalid")
    normalized_viewport: dict[str, int] | None
    if viewport is None:
        normalized_viewport = None
    else:
        if set(viewport) != {"width", "height"} or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in viewport.values()
        ):
            raise AcceptanceBatchError("acceptance_entry_viewport_invalid")
        normalized_viewport = {
            "width": int(viewport["width"]),
            "height": int(viewport["height"]),
        }
    if project == "none" and normalized_viewport is not None:
        raise AcceptanceBatchError("backend_entry_cannot_have_viewport")
    if project != "none" and normalized_viewport is None:
        raise AcceptanceBatchError("browser_entry_requires_viewport")
    return {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": str(batch_id),
        "suite": suite,
        "scenario": scenario,
        "stable_test_key": stable_test_key,
        "project": project,
        "viewport": normalized_viewport,
        "check_id": check_id,
        "status": status,
        "attempt": attempt,
        "evidence_kind": evidence_kind,
    }


def build_batch(
    *,
    suite: str,
    scenario: str,
    run_id: UUID | None,
    workspace_id: UUID | None,
    implementation_fingerprint: str | None,
    fingerprint_version: int | None,
    code_commit: str | None,
    catalog_fingerprint: str | None,
    expected: list[dict[str, object]],
    batch_id: UUID | None = None,
) -> dict[str, object]:
    batch_id = batch_id or uuid4()
    if not suite.strip() or not scenario.strip():
        raise AcceptanceBatchError("acceptance_batch_identity_required")
    if len({str(item.get("stable_test_key")) for item in expected}) != len(expected):
        raise AcceptanceBatchError("acceptance_batch_duplicate_test")
    for item in expected:
        validate_entry(item)
        if item["batch_id"] != str(batch_id):
            raise AcceptanceBatchError("acceptance_entry_batch_mismatch")
    return {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": str(batch_id),
        "suite": suite,
        "scenario": scenario,
        "run_id": None if run_id is None else str(run_id),
        "workspace_id": None if workspace_id is None else str(workspace_id),
        "implementation_fingerprint": implementation_fingerprint,
        "fingerprint_version": fingerprint_version,
        "code_commit": code_commit,
        "catalog_fingerprint": catalog_fingerprint,
        "created_at": datetime.now(UTC).isoformat(),
        "expected": expected,
    }


def validate_entry(value: Mapping[str, object]) -> None:
    if set(value) != _ENTRY_KEYS:
        raise AcceptanceBatchError("acceptance_entry_fields_invalid")
    if value["schema_version"] != BATCH_SCHEMA_VERSION:
        raise AcceptanceBatchError("acceptance_entry_schema_invalid")
    try:
        UUID(str(value["batch_id"]))
    except (ValueError, TypeError) as exc:
        raise AcceptanceBatchError("acceptance_entry_batch_id_invalid") from exc
    if not isinstance(value["project"], str):
        raise AcceptanceBatchError("acceptance_entry_project_invalid")
    viewport = value["viewport"]
    if viewport is not None and not isinstance(viewport, Mapping):
        raise AcceptanceBatchError("acceptance_entry_viewport_invalid")
    # Reuse the constructor's cross-field rules after validating the raw shape.
    build_entry(
        batch_id=UUID(str(value["batch_id"])),
        suite=str(value["suite"]),
        scenario=str(value["scenario"]),
        stable_test_key=str(value["stable_test_key"]),
        project=str(value["project"]),
        viewport=viewport if isinstance(viewport, Mapping) else None,
        check_id=str(value["check_id"]),
        status=str(value["status"]),
        attempt=value["attempt"],  # type: ignore[arg-type]
        evidence_kind=str(value["evidence_kind"]),
    )


def validate_batch(value: Mapping[str, object]) -> None:
    if set(value) != _BATCH_KEYS:
        raise AcceptanceBatchError("acceptance_batch_fields_invalid")
    if value["schema_version"] != BATCH_SCHEMA_VERSION:
        raise AcceptanceBatchError("acceptance_batch_schema_invalid")
    try:
        batch_id = UUID(str(value["batch_id"]))
    except (ValueError, TypeError) as exc:
        raise AcceptanceBatchError("acceptance_batch_id_invalid") from exc
    expected = value["expected"]
    if not isinstance(expected, list) or not expected:
        raise AcceptanceBatchError("acceptance_batch_expected_invalid")
    seen: set[str] = set()
    for item in expected:
        if not isinstance(item, Mapping):
            raise AcceptanceBatchError("acceptance_entry_invalid")
        validate_entry(item)
        if item["batch_id"] != str(batch_id):
            raise AcceptanceBatchError("acceptance_entry_batch_mismatch")
        key = str(item["stable_test_key"])
        if key in seen:
            raise AcceptanceBatchError("acceptance_batch_duplicate_test")
        seen.add(key)


def write_batch(path: Path, batch: Mapping[str, object]) -> None:
    validate_batch(batch)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(batch, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def read_batch(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceBatchError("acceptance_batch_unreadable") from exc
    if not isinstance(value, Mapping):
        raise AcceptanceBatchError("acceptance_batch_invalid")
    validate_batch(value)
    return dict(value)


def write_current_batch(path: Path, batch_id: UUID) -> None:
    """Persist the controller-selected batch without using filesystem mtime."""

    payload = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": str(batch_id),
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def read_current_batch(path: Path) -> UUID | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("schema_version") != BATCH_SCHEMA_VERSION:
            return None
        return UUID(str(value["batch_id"]))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
