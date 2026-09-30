from __future__ import annotations

import json
import os
import re
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from .acceptance_batch import read_current_batch
from .contracts import DemoManifest
from .evidence import (
    OVERALL_REQUIRED_IDS,
    CheckResult,
    evaluate_readiness,
    evidence_path_is_valid,
)
from .manifest import load_manifest

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_CATALOG_PATH = _PROJECT_ROOT / "apps" / "web" / "tests" / "interview" / "action-catalog.json"
_ACTION_ID = re.compile(r"^F(?:0[1-9]|1[0-8])-\d{2}$")
_SOURCE_FILES = {
    "control_plane_query": Path("reports/control-plane-query.json"),
    "artifact_hash": Path("reports/artifact-hash.json"),
    "local_check_receipt": Path("reports/local-check-receipt.json"),
    "safety_log": Path("logs/safety-audit.log"),
}


def action_catalog_ids(path: Path = _CATALOG_PATH) -> tuple[str, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        values = raw["actions"]
        ids = tuple(str(item["id"]) for item in values if isinstance(item, dict))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ()
    return tuple(dict.fromkeys(ids))


def action_catalog_fingerprint(path: Path = _CATALOG_PATH) -> str | None:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def validate_action_catalog(path: Path = _CATALOG_PATH) -> tuple[str, ...]:
    """Return stable validation codes for the browser action catalog."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ("catalog_unreadable",)
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return ("catalog_schema_invalid",)
    actions = raw.get("actions")
    if not isinstance(actions, list):
        return ("catalog_actions_invalid",)
    errors: list[str] = []
    ids: list[str] = []
    for item in actions:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            errors.append("action_entry_invalid")
            continue
        action_id = item["id"]
        ids.append(action_id)
        if _ACTION_ID.fullmatch(action_id) is None:
            errors.append(f"action_id_invalid:{action_id}")
    for action_id in sorted({item for item in ids if ids.count(item) > 1}):
        errors.append(f"action_id_duplicate:{action_id}")
    expected_groups = {f"F{number:02d}" for number in range(1, 19)}
    observed_groups = {action_id.split("-", 1)[0] for action_id in ids if "-" in action_id}
    errors.extend(
        f"action_group_missing:{group}"
        for group in sorted(expected_groups - observed_groups)
    )
    return tuple(errors)


def required_check_ids(path: Path = _CATALOG_PATH) -> tuple[str, ...]:
    return (*OVERALL_REQUIRED_IDS, *action_catalog_ids(path))


def build_report(
    manifest: DemoManifest,
    *,
    catalog_path: Path = _CATALOG_PATH,
    batch_id: UUID | None = None,
) -> dict[str, object]:
    """Read persisted evidence for one run and return a safe readiness report."""
    report_root = manifest.run_root / "reports"
    selected_batch_id = batch_id or read_current_batch(report_root / "current-batch.json")
    roots, root_diagnostics = _registered_evidence_roots(manifest)
    catalog_diagnostics = validate_action_catalog(catalog_path)
    expected_catalog_fingerprint = action_catalog_fingerprint(catalog_path)
    raw_actions, action_diagnostics, action_metadata = _collect_report_sources(
        manifest,
        roots,
        filename="action-report.json",
        collection_key="actions",
        diagnostic_prefix="action",
        catalog_fingerprint=expected_catalog_fingerprint,
        batch_id=selected_batch_id,
    )
    raw_overall, overall_diagnostics, _overall_metadata = _collect_report_sources(
        manifest,
        roots,
        filename="overall-checks.json",
        collection_key="checks",
        diagnostic_prefix="overall",
        catalog_fingerprint=None,
        batch_id=selected_batch_id,
    )
    source_paths, source_diagnostics = _collect_source_evidence(manifest, roots)
    diagnostics = [
        *catalog_diagnostics,
        *action_diagnostics,
        *overall_diagnostics,
        *root_diagnostics,
        *source_diagnostics,
    ]
    if manifest.schema_version == 2 and selected_batch_id is None:
        diagnostics.append("batch_identity_missing")
    catalog_ids = action_catalog_ids(catalog_path)
    required_ids = required_check_ids(catalog_path)
    catalog_mismatch = bool(catalog_diagnostics)
    observed_catalog_ids = tuple(
        item for item in required_ids if item.startswith("F")
    )
    if observed_catalog_ids != catalog_ids:
        catalog_mismatch = True
        diagnostics.append("catalog_required_ids_mismatch")
    if action_metadata.get("catalog_fingerprint") != expected_catalog_fingerprint:
        catalog_mismatch = True
        diagnostics.append("catalog_fingerprint_mismatch")
    if catalog_mismatch and "catalog_mismatch" not in diagnostics:
        diagnostics.append("catalog_mismatch")

    results: list[CheckResult] = []
    for check_id in required_ids:
        source = raw_overall if check_id in OVERALL_REQUIRED_IDS else raw_actions
        raw = source.get(check_id)
        if raw is None:
            results.append(
                CheckResult(
                    id=check_id,
                    status="NOT_RUN",
                    evidence_kind="simulated_interview",
                    failure_code=(
                        "overall_receipt_missing"
                        if check_id in OVERALL_REQUIRED_IDS
                        else "action_not_observed"
                    ),
                )
            )
            continue
        results.append(
            _result_from_report(
                check_id,
                raw,
                roots,
                expected_kind=(
                    "simulated_interview"
                    if check_id.startswith("F")
                    else None
                ),
            )
        )

    readiness = evaluate_readiness(tuple(results), required_ids)
    if diagnostics:
        readiness = "NOT_READY"
    report = {
        "schema_version": 2 if manifest.schema_version == 2 else 1,
        "run_id": str(manifest.run_id),
        "workspace_id": str(manifest.workspace_id),
        "scenario": manifest.scenario,
        "code_commit": manifest.code_commit,
        "implementation_fingerprint": manifest.implementation_fingerprint,
        "fingerprint_version": manifest.fingerprint_version,
        "batch_id": None if selected_batch_id is None else str(selected_batch_id),
        "readiness": readiness,
        "catalog_mismatch": catalog_mismatch,
        "catalog_fingerprint": expected_catalog_fingerprint,
        "evidence_roots": [str(root) for root in roots],
        "sources": source_paths,
        "diagnostics": list(dict.fromkeys(diagnostics)),
        "checks": [result.to_mapping() for result in results],
        "source": "persisted_local_evidence",
    }
    report_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(report_root, 0o700)
    path = report_root / "interview-report.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    return report


def _collect_source_evidence(
    manifest: DemoManifest,
    roots: tuple[Path, ...],
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Validate the fixed local receipts that support a publishable mainline."""
    diagnostics: list[str] = []
    candidates: list[tuple[DemoManifest, dict[str, Path]]] = []
    for root in roots:
        source_manifest = manifest
        if root != manifest.run_root:
            try:
                source_manifest = load_manifest(root / "manifest.json")
            except (OSError, ValueError):
                continue
        paths = {name: root / relative for name, relative in _SOURCE_FILES.items()}
        if all(
            path.is_file() and not path.is_symlink()
            for path in paths.values()
        ):
            candidates.append((source_manifest, paths))
    if not candidates:
        return {}, tuple(
            f"source_missing:{name}" for name in _SOURCE_FILES
        )

    source_manifest, paths = candidates[0]
    payloads: dict[str, dict[str, Any]] = {}
    for name in ("control_plane_query", "artifact_hash", "local_check_receipt"):
        try:
            value = json.loads(paths[name].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            diagnostics.append(f"source_invalid:{name}")
            continue
        if not isinstance(value, dict):
            diagnostics.append(f"source_invalid:{name}")
            continue
        payloads[name] = value
        if value.get("run_id") != str(source_manifest.run_id):
            diagnostics.append(f"source_run_id_mismatch:{name}")
        if value.get("code_commit") != source_manifest.code_commit:
            diagnostics.append(f"source_code_commit_mismatch:{name}")

    artifact = payloads.get("artifact_hash")
    if artifact is not None:
        content_path = artifact.get("content_path")
        expected_hash = artifact.get("content_sha256")
        if not isinstance(content_path, str) or not isinstance(expected_hash, str):
            diagnostics.append("artifact_hash_receipt_invalid")
        elif not evidence_path_is_valid(content_path, roots):
            diagnostics.append("artifact_content_outside_run")
        else:
            content = Path(content_path)
            if sha256(content.read_bytes()).hexdigest() != expected_hash:
                diagnostics.append("artifact_content_hash_mismatch")
            if artifact.get("artifact_hash") not in {None, expected_hash}:
                diagnostics.append("artifact_hash_mismatch")

    local_check = payloads.get("local_check_receipt")
    if local_check is not None and (
        local_check.get("status") != "passed"
        or local_check.get("exit_code") != 0
    ):
        diagnostics.append("local_check_not_passed")

    candidate_hashes = {
        value.get("candidate_hash")
        for value in payloads.values()
        if isinstance(value.get("candidate_hash"), str)
    }
    if len(candidate_hashes) > 1:
        diagnostics.append("candidate_hash_mismatch")

    try:
        safety_log = paths["safety_log"].read_text(encoding="utf-8")
    except OSError:
        diagnostics.append("source_invalid:safety_log")
    else:
        if (
            str(source_manifest.run_id) not in safety_log
            or "status=passed" not in safety_log
        ):
            diagnostics.append("safety_log_not_passed")

    return (
        {name: str(path) for name, path in paths.items()},
        tuple(dict.fromkeys(diagnostics)),
    )


def register_child_run(
    parent_manifest: DemoManifest,
    child_manifest: DemoManifest,
    *,
    suite: str,
    status: str,
) -> None:
    """Register one same-version test run as an allowed evidence source."""
    if not suite.strip() or status not in {"passed", "failed", "incomplete"}:
        raise ValueError("child_run_registry_entry_invalid")
    expected_root = parent_manifest.run_root.parent / str(child_manifest.run_id)
    child_root = child_manifest.run_root.expanduser()
    if (
        child_root != expected_root
        or child_root.is_symlink()
        or not _child_scenario_allowed(parent_manifest, child_manifest)
        or parent_manifest.code_commit != child_manifest.code_commit
    ):
        raise ValueError("child_run_registry_identity_mismatch")
    report_root = parent_manifest.run_root / "reports"
    report_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(report_root, 0o700)
    path = report_root / "child-runs.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {
            "schema_version": 1,
            "run_id": str(parent_manifest.run_id),
            "children": [],
        }
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("child_run_registry_invalid") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != 1
        or raw.get("run_id") != str(parent_manifest.run_id)
        or not isinstance(raw.get("children"), list)
    ):
        raise ValueError("child_run_registry_invalid")
    children = [item for item in raw["children"] if isinstance(item, dict)]
    entry = {
        "run_id": str(child_manifest.run_id),
        "run_root": str(child_root),
        "suite": suite,
        "status": status,
    }
    children = [item for item in children if item.get("run_id") != entry["run_id"]]
    children.append(entry)
    document = {
        "schema_version": 1,
        "run_id": str(parent_manifest.run_id),
        "children": sorted(children, key=lambda item: str(item.get("run_id", ""))),
    }
    _write_private_json(path, document)


def _collect_report_sources(
    manifest: DemoManifest,
    roots: tuple[Path, ...],
    *,
    filename: str,
    collection_key: str,
    diagnostic_prefix: str,
    catalog_fingerprint: str | None,
    batch_id: UUID | None,
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...], dict[str, Any]]:
    """Merge the latest observed result from this run and its registered children."""
    sources: list[
        tuple[
            Path,
            Path,
            DemoManifest,
            dict[str, dict[str, Any]],
            tuple[str, ...],
            dict[str, Any],
        ]
    ] = []
    diagnostics: list[str] = []
    own_path = manifest.run_root / "reports" / filename
    own_values, own_diagnostics, own_metadata = _read_evidence_report(
        own_path, collection_key, diagnostic_prefix
    )
    if own_values or own_metadata or "report_missing" not in " ".join(own_diagnostics):
        sources.append(
            (
                own_path,
                manifest.run_root,
                manifest,
                own_values,
                own_diagnostics,
                own_metadata,
            )
        )

    for root in roots[1:]:
        try:
            child_manifest = load_manifest(root / "manifest.json")
        except (OSError, ValueError):
            diagnostics.append(f"child_{diagnostic_prefix}_manifest_invalid:{root.name}")
            continue
        source_path = root / "reports" / filename
        values, source_diagnostics, metadata = _read_evidence_report(
            source_path, collection_key, diagnostic_prefix
        )
        if not values and not metadata and any(
            item == f"{diagnostic_prefix}_report_missing" for item in source_diagnostics
        ):
            continue
        sources.append(
            (
                source_path,
                root,
                child_manifest,
                values,
                source_diagnostics,
                metadata,
            )
        )

    if not sources:
        diagnostics.extend(own_diagnostics)
        return {}, tuple(dict.fromkeys(diagnostics)), {}

    merged: dict[str, dict[str, Any]] = {}
    selected_order: dict[str, tuple[int, str]] = {}
    selected_metadata: dict[str, Any] = {}
    ordered_sources = (
        list(sources)
        if batch_id is not None
        else sorted(sources, key=lambda item: _report_source_order(item[0]))
    )
    for source_index, (
        source_path,
        root,
        source_manifest,
        values,
        source_diagnostics,
        metadata,
    ) in enumerate(ordered_sources):
        diagnostics.extend(source_diagnostics)
        diagnostics.extend(
            _identity_diagnostics(
                metadata,
                source_manifest,
                label=f"{diagnostic_prefix}_report",
                catalog_fingerprint=catalog_fingerprint,
                batch_id=batch_id,
            )
        )
        if batch_id is not None and metadata.get("batch_id") != str(batch_id):
            diagnostics.append(f"{diagnostic_prefix}_report_batch_mismatch")
            continue
        if not selected_metadata and metadata:
            selected_metadata = metadata
        for item_id, item in values.items():
            if item.get("status") == "NOT_RUN":
                continue
            order = (
                (source_index, str(source_path))
                if batch_id is not None
                else _report_source_order(source_path)
            )
            existing = merged.get(item_id)
            if existing is None or order >= selected_order[item_id]:
                merged[item_id] = item
                selected_order[item_id] = order
            elif existing is not None and batch_id is not None:
                diagnostics.append(f"{diagnostic_prefix}_batch_observation_duplicate:{item_id}")
    return merged, tuple(dict.fromkeys(diagnostics)), selected_metadata


def _read_evidence_report(
    path: Path,
    collection_key: str,
    diagnostic_prefix: str,
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...], dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, (f"{diagnostic_prefix}_report_missing",), {}
    if not isinstance(raw, dict):
        return {}, (f"{diagnostic_prefix}_report_invalid",), {}
    if raw.get("schema_version") not in {1, 2}:
        return {}, (f"{diagnostic_prefix}_report_schema_invalid",), raw
    values = raw.get(collection_key, [])
    if not isinstance(values, list):
        return {}, (f"{diagnostic_prefix}_report_entries_invalid",), raw
    diagnostics: list[str] = []
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            diagnostics.append(f"{diagnostic_prefix}_entry_invalid")
            continue
        item_id = item["id"]
        if item_id in result:
            diagnostics.append(f"{diagnostic_prefix}_id_duplicate:{item_id}")
            if _status_rank(item.get("status")) > _status_rank(result[item_id].get("status")):
                result[item_id] = item
            continue
        result[item_id] = item
    return result, tuple(dict.fromkeys(diagnostics)), raw


def _status_rank(status: object) -> int:
    ranks: dict[object, int] = {
        "FAIL": 4,
        "INCOMPLETE": 3,
        "PASS": 2,
        "NOT_RUN": 1,
    }
    return ranks.get(status, 0)


def _report_source_order(path: Path) -> tuple[int, str]:
    """Use persisted report time to let a later rerun supersede an old result."""
    try:
        modified_at = path.stat().st_mtime_ns
    except OSError:
        modified_at = 0
    return modified_at, str(path)


def _registered_evidence_roots(
    manifest: DemoManifest,
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """Return the run root plus explicitly registered, same-version child runs."""
    roots = [manifest.run_root]
    path = manifest.run_root / "reports" / "child-runs.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return tuple(roots), ()
    except (OSError, json.JSONDecodeError):
        return tuple(roots), ("child_runs_registry_invalid",)
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return tuple(roots), ("child_runs_registry_invalid",)
    children = raw.get("children")
    if not isinstance(children, list):
        return tuple(roots), ("child_runs_registry_invalid",)

    diagnostics: list[str] = []
    storage_root = manifest.run_root.parent.resolve(strict=False)
    for item in children:
        if not isinstance(item, dict):
            diagnostics.append("child_run_entry_invalid")
            continue
        run_id = item.get("run_id")
        raw_root = item.get("run_root")
        if not isinstance(run_id, str) or not isinstance(raw_root, str):
            diagnostics.append("child_run_entry_invalid")
            continue
        child_root = Path(raw_root).expanduser()
        expected_root = storage_root / run_id
        if (
            not child_root.is_absolute()
            or child_root != expected_root
            or child_root.is_symlink()
            or not child_root.is_dir()
        ):
            diagnostics.append(f"child_run_root_invalid:{run_id}")
            continue
        try:
            child_manifest = load_manifest(child_root / "manifest.json")
        except (OSError, ValueError):
            diagnostics.append(f"child_run_manifest_invalid:{run_id}")
            continue
        if (
            str(child_manifest.run_id) != run_id
            or not _child_scenario_allowed(manifest, child_manifest)
            or child_manifest.code_commit != manifest.code_commit
        ):
            diagnostics.append(f"child_run_identity_mismatch:{run_id}")
            continue
        roots.append(child_root)
    return tuple(roots), tuple(dict.fromkeys(diagnostics))


def _child_scenario_allowed(
    parent_manifest: DemoManifest,
    child_manifest: DemoManifest,
) -> bool:
    """Allow mainline to aggregate explicitly registered extension scenarios."""
    return (
        parent_manifest.scenario == "mainline"
        or parent_manifest.scenario == child_manifest.scenario
    )


def _identity_diagnostics(
    metadata: dict[str, Any],
    manifest: DemoManifest,
    *,
    label: str,
    catalog_fingerprint: str | None = None,
    batch_id: UUID | None = None,
) -> tuple[str, ...]:
    if not metadata:
        return ()
    diagnostics: list[str] = []
    expected = {
        "run_id": str(manifest.run_id),
        "scenario": manifest.scenario,
        "code_commit": manifest.code_commit,
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            diagnostics.append(f"{label}_{key}_mismatch")
    if manifest.implementation_fingerprint is not None:
        if metadata.get("implementation_fingerprint") != manifest.implementation_fingerprint:
            diagnostics.append(f"{label}_implementation_fingerprint_mismatch")
        if metadata.get("fingerprint_version") != manifest.fingerprint_version:
            diagnostics.append(f"{label}_fingerprint_version_mismatch")
    if catalog_fingerprint is not None and metadata.get("catalog_fingerprint") != catalog_fingerprint:
        diagnostics.append(f"{label}_catalog_fingerprint_mismatch")
    if batch_id is not None and metadata.get("batch_id") != str(batch_id):
        diagnostics.append(f"{label}_batch_id_mismatch")
    return tuple(diagnostics)


def _result_from_report(
    check_id: str,
    raw: dict[str, Any],
    roots: tuple[Path, ...],
    *,
    expected_kind: str | None = None,
) -> CheckResult:
    status = raw.get("status")
    if status not in {"NOT_RUN", "PASS", "FAIL", "INCOMPLETE"}:
        status = "INCOMPLETE"
    kind = raw.get("evidence_kind", "simulated_interview")
    if kind not in {"simulated_interview", "real_local_verification"}:
        kind = "simulated_interview"
    raw_paths = raw.get("evidence_paths", [])
    paths = tuple(str(path) for path in raw_paths) if isinstance(raw_paths, list) else ()
    if status == "PASS" and expected_kind is not None and kind != expected_kind:
        status = "INCOMPLETE"
        failure_code = "action_evidence_kind_invalid"
    elif status == "PASS" and any(not evidence_path_is_valid(path, roots) for path in paths):
        status = "INCOMPLETE"
        failure_code = "evidence_path_outside_run"
    elif status == "PASS" and not paths:
        status = "INCOMPLETE"
        failure_code = "passing_check_requires_evidence"
    elif status == "PASS" and (
        isinstance(raw.get("candidate_hash"), str)
        and isinstance(raw.get("artifact_hash"), str)
        and raw["candidate_hash"] != raw["artifact_hash"]
    ):
        status = "INCOMPLETE"
        failure_code = "candidate_hash_mismatch"
    else:
        failure_code = raw.get("failure_code")
        if failure_code is not None and not isinstance(failure_code, str):
            failure_code = "invalid_failure_code"
    return CheckResult(
        id=check_id,
        status=status,
        evidence_kind=kind,
        evidence_paths=paths,
        failure_code=failure_code,
    )


def _write_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        json.dump(value, target, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
