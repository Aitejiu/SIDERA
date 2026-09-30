#!/usr/bin/env python3
"""Local, provider-free maintenance for an explicitly inventoried SIDERA run.

The command intentionally has no generic command execution path.  It only reads
validated manifests and the component paths named by their companion inventory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE_ROOT = PROJECT_ROOT / "services" / "control-plane"
if str(CONTROL_PLANE_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_PLANE_ROOT))

from sidera_control_plane.basic_acceptance_manifest import (
    load_manifest as load_basic_manifest,
)
from sidera_control_plane.basic_acceptance_manifest import (
    validate_manifest_isolation,
)
from sidera_control_plane.demo.manifest import (
    load_manifest as load_demo_manifest,
)
from sidera_control_plane.demo.manifest import (
    validate_manifest as validate_demo_manifest,
)
from sidera_control_plane.maintenance_lock import (
    MaintenanceLockBusy,
    maintenance_lock,
)

INVENTORY_SCHEMA = 1
BACKUP_SCHEMA = 1
ALLOWED_KINDS = {"postgres", "sqlite", "directory", "git", "metadata", "ledger"}
ALLOWED_SECRET_POLICIES = {"private_data", "metadata_only", "exclude"}
INVENTORY_REQUIRED_FIELDS = {
    "schema_version",
    "run_id",
    "workspace_id",
    "code_commit",
    "implementation_fingerprint",
    "runtime_fingerprint",
    "writers",
    "persistence",
    "components",
}
INVENTORY_OPTIONAL_FIELDS = {"source_identities"}
PERSISTENCE_FIELDS = {
    "status",
    "component_ids",
    "unsupported_component_ids",
}
WRITER_FIELDS = {"status", "active_component_ids", "observed_at"}
SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "credentials",
    "credentials.json",
    "auth.json",
    "token",
    "tokens",
    "keychain",
}


class MaintenanceError(ValueError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def doctor(manifest_path: Path) -> dict[str, object]:
    context = _load_context(manifest_path)
    inventory = _load_inventory(
        context,
        require_complete_coverage=False,
        require_stopped=False,
    )
    tools = {
        name: bool(shutil.which(name))
        for name in (
            "python3",
            "uv",
            "node",
            "npm",
            "git",
            "pg_isready",
            "psql",
            "pg_dump",
            "ps",
        )
    }
    tool_versions = {
        name: _tool_version(name) for name, available in tools.items() if available
    }
    issues: list[str] = []
    if not tools["python3"] or not tools["git"]:
        issues.append("required_local_tool_missing")
    persistence = inventory["persistence"]
    if persistence["status"] != "complete":
        issues.append("persistence_incomplete")
    for component_id in persistence["unsupported_component_ids"]:
        issues.append(f"persistence_unsupported:{component_id}")
    for component in inventory["components"]:
        try:
            _validate_component(component, context)
        except MaintenanceError as exc:
            component_id = (
                component.get("component_id", "unknown")
                if isinstance(component, dict)
                else "unknown"
            )
            issues.append(f"component:{component_id}:{exc.code}")
    writer_status = inventory["writers"]
    if writer_status["status"] != "stopped" or writer_status["active_component_ids"]:
        issues.append("writers_not_stopped")
    writer_facts, writer_issues = _inspect_writer_identity(context, writer_status)
    issues.extend(writer_issues)
    permissions, permission_issues = _inspect_permissions(context)
    issues.extend(permission_issues)
    database_facts, database_issues, database_credentials_used = _inspect_postgres_components(
        inventory["components"], context, persistence
    )
    issues.extend(database_issues)
    ports, port_issues = _inspect_manifest_ports(context)
    issues.extend(port_issues)
    run_root = context["run_root"]
    if not run_root.exists():
        issues.append("run_root_missing")
    elif run_root.is_symlink():
        issues.append("run_root_symlink")
    return {
        "schema_version": 1,
        "status": "PASS" if not issues else "INCOMPLETE",
        "manifest_kind": context["kind"],
        "run_id": context["run_id"],
        "workspace_id": context["workspace_id"],
        "run_root": str(run_root),
        "implementation_fingerprint": context["implementation_fingerprint"],
        "tools": tools,
        "tool_versions": tool_versions,
        "component_count": len(inventory["components"]),
        "issues": issues,
        "ports": ports,
        "writer_facts": writer_facts,
        "database_facts": database_facts,
        "permissions": permissions,
        "provider_calls": 0,
        "secret_values_read": database_credentials_used,
    }


def backup(
    manifest_path: Path,
    destination: Path,
    *,
    source_inventory: Path | None = None,
) -> dict[str, object]:
    context = _load_context(manifest_path)
    try:
        with maintenance_lock(context["run_root"], owner="backup"):
            return _backup_unlocked(
                context,
                destination,
                source_inventory=source_inventory,
            )
    except MaintenanceLockBusy as exc:
        raise MaintenanceError("maintenance_busy") from exc


def _backup_unlocked(
    context: dict[str, object],
    destination: Path,
    *,
    source_inventory: Path | None = None,
) -> dict[str, object]:
    inventory = _load_inventory(context, source_inventory=source_inventory)
    database_facts = _postgres_backup_facts(
        inventory["components"], context, inventory["persistence"]
    )
    destination = _absolute(destination, "destination")
    if destination.exists():
        raise MaintenanceError("destination_must_not_exist")
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True, mode=stat.S_IRWXU)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=parent))
    os.chmod(temporary, stat.S_IRWXU)
    try:
        files: list[dict[str, object]] = []
        excluded: list[dict[str, str]] = []
        restore_targets: set[str] = set()
        git_facts: dict[str, dict[str, object]] = {}
        component_records = [
            {
                "component_id": component["component_id"],
                "kind": component["kind"],
                "restore_relative_path": component["restore_relative_path"],
                "schema_identity": component["schema_identity"],
                "required": component["required"],
                "secret_policy": component["secret_policy"],
            }
            for component in inventory["components"]
        ]
        for component in inventory["components"]:
            _validate_component(component, context)
            policy = component["secret_policy"]
            if policy == "exclude":
                excluded.append(
                    {
                        "component_id": component["component_id"],
                        "reason": "secret_policy_exclude",
                    }
                )
                if component["required"]:
                    raise MaintenanceError("required_component_excluded")
                continue
            if component["kind"] == "postgres":
                relative = _snapshot_postgres(component, context, temporary)
                restore_list_entries = _verify_postgres_dump(
                    temporary / "payload" / relative, component, context
                )
                fact = database_facts.get(component["component_id"])
                if fact is None:
                    raise MaintenanceError("database_identity_unverified")
                fact["restore_list_entries"] = restore_list_entries
                fact["dump_format"] = "custom"
                digest, size = _file_digest(temporary / "payload" / relative)
                files.append(
                    {
                        "relative_path": str(Path("payload") / relative),
                        "component_id": component["component_id"],
                        "sha256": digest,
                        "size": size,
                    }
                )
                continue
            if component["kind"] == "sqlite" and str(component["source_ref"]).startswith(
                "sqlite:file:"
            ):
                relative = _snapshot_sqlite(component, context, temporary)
                digest, size = _file_digest(temporary / "payload" / relative)
                files.append(
                    {
                        "relative_path": str(Path("payload") / relative),
                        "component_id": component["component_id"],
                        "sha256": digest,
                        "size": size,
                    }
                )
                continue
            if component["kind"] == "git":
                relatives, fact = _snapshot_git(component, context, temporary)
                git_facts[component["component_id"]] = fact
                for relative in relatives:
                    relative_target = str(relative)
                    if relative_target in restore_targets:
                        raise MaintenanceError("restore_path_conflict")
                    restore_targets.add(relative_target)
                    digest, size = _file_digest(temporary / "payload" / relative)
                    files.append(
                        {
                            "relative_path": str(Path("payload") / relative),
                            "component_id": component["component_id"],
                            "sha256": digest,
                            "size": size,
                        }
                    )
                continue
            source = _component_source(component, context)
            _ensure_destination_not_in_source(destination, source)
            paths = _source_files(source, component["kind"])
            if not paths and component["required"]:
                raise MaintenanceError("required_component_empty")
            for source_file, relative in paths:
                rel = _restore_path(component["restore_relative_path"], relative)
                relative_target = str(rel)
                if relative_target in restore_targets:
                    raise MaintenanceError("restore_path_conflict")
                restore_targets.add(relative_target)
                if _looks_sensitive(source_file):
                    raise MaintenanceError("sensitive_path_not_excluded")
                target = temporary / "payload" / rel
                _copy_verified(source_file, target)
                digest, size = _file_digest(target)
                files.append(
                    {
                        "relative_path": str(Path("payload") / rel),
                        "component_id": component["component_id"],
                        "sha256": digest,
                        "size": size,
                    }
                )
        files_by_component = {
            str(entry["component_id"]): 0 for entry in files
        }
        for entry in files:
            files_by_component[str(entry["component_id"])] += 1
        excluded_ids = {entry["component_id"] for entry in excluded}
        for component in inventory["components"]:
            if (
                component["required"]
                and files_by_component.get(component["component_id"], 0) == 0
                and component["component_id"] not in excluded_ids
            ):
                raise MaintenanceError("required_component_empty")
        if not files and inventory["components"]:
            raise MaintenanceError("backup_inventory_incomplete")
        backup_manifest: dict[str, object] = {
            "schema_version": BACKUP_SCHEMA,
            "status": "COMPLETE",
            "restore_mode": "read_only",
            "run_id": context["run_id"],
            "workspace_id": context["workspace_id"],
            "source_commit": context["source_commit"],
            "implementation_fingerprint": context["implementation_fingerprint"],
            "runtime_fingerprint": context["runtime_fingerprint"],
            "inventory_sha256": _sha256_file(_inventory_path(context, source_inventory)),
            "components": component_records,
            "files": files,
            "database_facts": database_facts,
            "git_facts": git_facts,
            "excluded": excluded,
            "requires_reconfiguration": True,
            "created_at": datetime.now(UTC).isoformat(),
        }
        _write_json(temporary / "backup-manifest.json", backup_manifest)
        _verify_backup_manifest(temporary, backup_manifest)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return _public_backup_summary(destination, backup_manifest)


def inspect_backup(backup_path: Path) -> dict[str, object]:
    backup_path = _absolute(backup_path, "backup")
    manifest_path = backup_path / "backup-manifest.json"
    try:
        raw = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MaintenanceError("backup_manifest_invalid") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != BACKUP_SCHEMA:
        raise MaintenanceError("backup_schema_unsupported")
    _verify_backup_manifest(backup_path, raw)
    return _public_backup_summary(backup_path, raw)


def restore_preflight(backup_path: Path, target_manifest_path: Path) -> dict[str, object]:
    preflight = _restore_preflight(backup_path, target_manifest_path)
    return {
        "status": "READY",
        "restore_mode": "read_only",
        "source_run_id": preflight["summary"]["run_id"],
        "target_run_id": preflight["target"]["run_id"],
        "target_root": str(preflight["target_root"]),
        "database_targets": preflight["database_targets"],
        "database_imports": 0,
        "provider_calls": 0,
        "writes_enabled": False,
    }


def _restore_preflight(
    backup_path: Path, target_manifest_path: Path
) -> dict[str, object]:
    backup_path = _absolute(backup_path, "backup")
    summary = inspect_backup(backup_path)
    target_manifest_path = _absolute(target_manifest_path, "target_manifest")
    target = _load_restore_target_manifest(target_manifest_path)
    if target.get("restore_mode") != "read_only":
        raise MaintenanceError("restore_must_be_read_only")
    target_root = _validate_restore_target(
        target,
        target_manifest_path=target_manifest_path,
        backup_path=backup_path,
        source_run_id=str(summary["run_id"]),
    )
    if target_root.exists():
        raise MaintenanceError("target_must_be_new_or_empty")
    database_targets = _validate_restore_database_targets(
        summary,
        target,
        backup_path=backup_path,
    )
    return {
        "summary": summary,
        "target": target,
        "target_root": target_root,
        "database_targets": database_targets,
    }


def _load_restore_target_manifest(target_manifest_path: Path) -> dict[str, object]:
    try:
        target = json.loads(
            target_manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MaintenanceError("target_manifest_invalid") from exc
    if not isinstance(target, dict):
        raise MaintenanceError("target_manifest_invalid")
    return target


def _validate_restore_database_targets(
    summary: dict[str, object],
    target: dict[str, object],
    *,
    backup_path: Path,
) -> dict[str, dict[str, object]]:
    components = summary.get("components", [])
    postgres_components = [
        component
        for component in components
        if isinstance(component, dict) and component.get("kind") == "postgres"
    ]
    target_values = target.get("database_targets", [])
    if not postgres_components:
        if target_values not in (None, []):
            raise MaintenanceError("restore_database_target_unexpected")
        return {}
    if not isinstance(target_values, list) or len(target_values) != len(postgres_components):
        raise MaintenanceError("restore_database_targets_required")

    component_by_id = {
        str(component["component_id"]): component for component in postgres_components
    }
    target_by_id: dict[str, dict[str, object]] = {}
    for value in target_values:
        if not isinstance(value, dict) or set(value) != {
            "component_id",
            "target_ref",
            "schema_identity",
        }:
            raise MaintenanceError("restore_database_target_invalid")
        component_id = value.get("component_id")
        if (
            not isinstance(component_id, str)
            or component_id in target_by_id
            or component_id not in component_by_id
            or not isinstance(value.get("target_ref"), str)
            or not isinstance(value.get("schema_identity"), str)
        ):
            raise MaintenanceError("restore_database_target_invalid")
        if value["schema_identity"] != component_by_id[component_id]["schema_identity"]:
            raise MaintenanceError("restore_schema_incompatible")
        target_by_id[component_id] = value
    if set(target_by_id) != set(component_by_id):
        raise MaintenanceError("restore_database_target_invalid")

    source_facts = summary.get("database_facts")
    if not isinstance(source_facts, dict):
        raise MaintenanceError("restore_source_database_unverified")
    results: dict[str, dict[str, object]] = {}
    for component_id, component in component_by_id.items():
        source_fact = source_facts.get(component_id)
        if not isinstance(source_fact, dict):
            raise MaintenanceError("restore_source_database_unverified")
        if source_fact.get("status") != "ready":
            raise MaintenanceError("restore_source_database_unverified")
        if str(source_fact.get("active_writer_sessions")) != "0":
            raise MaintenanceError("restore_source_writer_active")
        if source_fact.get("schema_identity") != component["schema_identity"]:
            raise MaintenanceError("restore_schema_incompatible")
        results[component_id] = _probe_restore_database_target(
            component_id,
            target_by_id[component_id],
            source_fact,
            backup_path=backup_path,
        )
    return results


def _probe_restore_database_target(
    component_id: str,
    target: dict[str, object],
    source_fact: dict[str, object],
    *,
    backup_path: Path,
) -> dict[str, object]:
    target_ref = str(target["target_ref"])
    reference = target_ref.removeprefix("postgres:ref:")
    if reference == target_ref or not reference:
        raise MaintenanceError("restore_database_target_invalid")
    if reference.startswith("file:"):
        reference_path = _absolute(
            Path(reference.removeprefix("file:")), "target_database_reference"
        )
        if reference_path.is_symlink() or _within(
            reference_path.resolve(strict=False), backup_path.resolve(strict=False)
        ):
            raise MaintenanceError("restore_database_target_invalid")
    elif not reference.startswith("env:"):
        raise MaintenanceError("restore_database_target_invalid")

    target_component = {"source_ref": target_ref}
    try:
        dsn, environment = _postgres_dump_config(target_component, {})
    except MaintenanceError as exc:
        raise MaintenanceError("restore_database_target_invalid") from exc
    parsed = urlsplit(dsn)
    if (
        parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.query
        or parsed.fragment
    ):
        raise MaintenanceError("restore_target_not_loopback")
    pg_isready = shutil.which("pg_isready")
    psql = shutil.which("psql")
    if pg_isready is None or psql is None:
        raise MaintenanceError("restore_database_tools_missing")
    try:
        ready = subprocess.run(
            [pg_isready, "--dbname", dsn],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MaintenanceError("restore_target_probe_failed") from exc
    if ready.returncode != 0:
        raise MaintenanceError("restore_target_unready")
    identity = _run_psql(
        psql,
        dsn,
        "SELECT current_database(), current_user",
        environment,
    )
    if identity.returncode != 0:
        raise MaintenanceError("restore_target_probe_failed")
    target_identity = identity.stdout.strip().splitlines()[0] if identity.stdout.strip() else ""
    source_identity = str(source_fact.get("identity", ""))
    if not target_identity or _database_name_from_identity(target_identity) == _database_name_from_identity(source_identity):
        raise MaintenanceError("restore_target_identity_conflict")
    relation_count = _run_psql(
        psql,
        dsn,
        "SELECT count(*) FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
        "AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')",
        environment,
    )
    if relation_count.returncode != 0:
        raise MaintenanceError("restore_target_probe_failed")
    if relation_count.stdout.strip() != "0":
        raise MaintenanceError("restore_target_not_empty")
    heads = _run_psql(
        psql,
        dsn,
        "SELECT version_num FROM alembic_version ORDER BY version_num",
        environment,
    )
    if heads.returncode != 0:
        missing_heads_table = (
            "alembic_version" in heads.stderr
            and "does not exist" in heads.stderr
        )
        if not missing_heads_table:
            raise MaintenanceError("restore_target_probe_failed")
        target_heads = []
    else:
        target_heads = [line.strip() for line in heads.stdout.splitlines() if line.strip()]
    if target_heads:
        raise MaintenanceError("restore_target_schema_not_empty")
    return {
        "component_id": component_id,
        "identity": target_identity,
        "heads": target_heads,
        "relation_count": 0,
        "status": "empty",
    }


def _database_name_from_identity(identity: str) -> str:
    return identity.split("|", 1)[0].strip()


def _import_postgres_databases(
    preflight: dict[str, object], backup_path: Path
) -> list[dict[str, object]]:
    summary = preflight["summary"]
    target = preflight["target"]
    components = summary.get("components", [])
    files = summary.get("files", [])
    source_facts = summary.get("database_facts", {})
    target_definitions = {
        str(value["component_id"]): value
        for value in target.get("database_targets", [])
        if isinstance(value, dict)
    }
    imported: list[dict[str, object]] = []
    for component in components:
        if not isinstance(component, dict) or component.get("kind") != "postgres":
            continue
        component_id = str(component["component_id"])
        dump_entries = [
            entry
            for entry in files
            if isinstance(entry, dict) and entry.get("component_id") == component_id
        ]
        if len(dump_entries) != 1:
            raise MaintenanceError("restore_database_dump_mapping_invalid")
        target_definition = target_definitions.get(component_id)
        if target_definition is None:
            raise MaintenanceError("restore_database_targets_required")
        dump_path = backup_path / _safe_relative(str(dump_entries[0]["relative_path"]))
        if not dump_path.is_file() or dump_path.is_symlink():
            raise MaintenanceError("backup_file_missing")
        try:
            dsn, environment = _postgres_dump_config(
                {"source_ref": target_definition["target_ref"]}, {}
            )
        except MaintenanceError as exc:
            raise MaintenanceError("restore_database_target_invalid") from exc
        executable = shutil.which("pg_restore")
        if executable is None:
            raise MaintenanceError("pg_restore_missing")
        command = [
            executable,
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--single-transaction",
            "--dbname",
            dsn,
            str(dump_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MaintenanceError("restore_postgres_import_failed") from exc
        if completed.returncode != 0:
            raise MaintenanceError("restore_postgres_import_failed")
        source_fact = source_facts.get(component_id)
        target_fact = preflight["database_targets"].get(component_id)
        if not isinstance(source_fact, dict) or not isinstance(target_fact, dict):
            raise MaintenanceError("restore_source_database_unverified")
        verified = _verify_restored_postgres_database(
            psql=shutil.which("psql"),
            dsn=dsn,
            environment=environment,
            expected_identity=str(target_fact["identity"]),
            expected_heads=[str(head) for head in source_fact.get("heads", [])],
        )
        imported.append(
            {
                "component_id": component_id,
                "dump_sha256": dump_entries[0]["sha256"],
                "dump_size": dump_entries[0]["size"],
                "identity": verified["identity"],
                "heads": verified["heads"],
                "status": "IMPORTED",
            }
        )
    return imported


def _verify_restored_postgres_database(
    *,
    psql: str | None,
    dsn: str,
    environment: dict[str, str],
    expected_identity: str,
    expected_heads: list[str],
) -> dict[str, object]:
    if psql is None:
        raise MaintenanceError("restore_database_tools_missing")
    identity = _run_psql(
        psql,
        dsn,
        "SELECT current_database(), current_user",
        environment,
    )
    if identity.returncode != 0:
        raise MaintenanceError("restore_schema_verification_failed")
    actual_identity = identity.stdout.strip().splitlines()[0] if identity.stdout.strip() else ""
    if actual_identity != expected_identity:
        raise MaintenanceError("restore_target_identity_conflict")
    heads = _run_psql(
        psql,
        dsn,
        "SELECT version_num FROM alembic_version ORDER BY version_num",
        environment,
    )
    if heads.returncode != 0:
        raise MaintenanceError("restore_schema_verification_failed")
    actual_heads = [line.strip() for line in heads.stdout.splitlines() if line.strip()]
    if actual_heads != expected_heads:
        raise MaintenanceError("restore_schema_verification_failed")
    return {"identity": actual_identity, "heads": actual_heads}


def _restore_git_repositories(
    preflight: dict[str, object], backup_path: Path, temporary: Path
) -> list[dict[str, object]]:
    summary = preflight["summary"]
    components = summary.get("components", [])
    git_facts = summary.get("git_facts", {})
    git = shutil.which("git")
    if git is None:
        raise MaintenanceError("git_missing")
    restored: list[dict[str, object]] = []
    for component in components:
        if not isinstance(component, dict) or component.get("kind") != "git":
            continue
        component_id = str(component["component_id"])
        fact = git_facts.get(component_id)
        if not isinstance(fact, dict):
            raise MaintenanceError("restore_git_mapping_invalid")
        bundle_relative = _safe_relative(str(fact.get("bundle_path", "")))
        bundle = backup_path / "payload" / bundle_relative
        if not bundle.is_file() or bundle.is_symlink():
            raise MaintenanceError("restore_git_bundle_missing")
        restore_root = _safe_relative(
            str(component.get("restore_relative_path", "repository"))
        )
        repository = temporary / restore_root
        repository.parent.mkdir(parents=True, exist_ok=True)
        clone = subprocess.run(
            [git, "clone", "--no-local", str(bundle), str(repository)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "PATH": os.environ.get("PATH", ""),
            },
        )
        if clone.returncode != 0:
            raise MaintenanceError("restore_git_clone_failed")
        state = {
            "head": str(fact.get("head", "")),
            "tree": str(fact.get("tree", "")),
        }
        cloned_head = _run_git_restore_command(git, repository, "rev-parse", "HEAD")
        cloned_tree = _run_git_restore_command(git, repository, "rev-parse", "HEAD^{tree}")
        if cloned_head != state["head"] or cloned_tree != state["tree"]:
            raise MaintenanceError("restore_git_clone_failed")
        if (repository / ".git" / "commondir").exists():
            raise MaintenanceError("restore_git_clone_failed")

        patches = fact.get("patches", {})
        if not isinstance(patches, dict):
            raise MaintenanceError("restore_git_mapping_invalid")
        staged_patch = patches.get("staged.patch")
        if staged_patch:
            _apply_git_patch(
                git,
                repository,
                backup_path / "payload" / _safe_relative(str(staged_patch)),
                cached=True,
            )
        worktree_root = backup_path / "payload" / _safe_relative(
            str(fact.get("worktree_root", ""))
        )
        if not worktree_root.is_dir() or worktree_root.is_symlink():
            raise MaintenanceError("restore_git_worktree_missing")
        for source_file in sorted(worktree_root.rglob("*")):
            if source_file.is_symlink() or not source_file.is_file():
                if source_file.is_symlink() or not source_file.is_dir():
                    raise MaintenanceError("restore_git_worktree_invalid")
                continue
            relative = _safe_relative(str(source_file.relative_to(worktree_root)))
            _copy_verified(source_file, repository / relative)
        for deleted in fact.get("deleted_paths", []):
            target = repository / _safe_relative(str(deleted))
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    raise MaintenanceError("restore_git_worktree_invalid")
                target.unlink()
        restored.append(
            {
                "component_id": component_id,
                "repository": str(restore_root),
                "head": cloned_head,
                "tree": cloned_tree,
                "status": "IMPORTED",
            }
        )
    return restored


def _run_git_restore_command(git: str, repository: Path, *args: str) -> str:
    completed = subprocess.run(
        [git, "-C", str(repository), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "PATH": os.environ.get("PATH", ""),
        },
    )
    if completed.returncode != 0:
        raise MaintenanceError("restore_git_command_failed")
    return completed.stdout.strip()


def _apply_git_patch(
    git: str, repository: Path, patch: Path, *, cached: bool
) -> None:
    if not patch.is_file() or patch.is_symlink():
        raise MaintenanceError("restore_git_patch_missing")
    command = [git, "-C", str(repository), "apply", "--binary"]
    if cached:
        command.append("--cached")
    command.append(str(patch))
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "PATH": os.environ.get("PATH", ""),
        },
    )
    if completed.returncode != 0:
        raise MaintenanceError("restore_git_patch_failed")


def restore(backup_path: Path, target_manifest_path: Path) -> dict[str, object]:
    preflight = _restore_preflight(backup_path, target_manifest_path)
    summary = preflight["summary"]
    target = preflight["target"]
    target_root = preflight["target_root"]
    target_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target_root.name}.", dir=target_root.parent))
    os.chmod(temporary, stat.S_IRWXU)
    imported_databases: list[dict[str, object]] = []
    restored_repositories: list[dict[str, object]] = []
    try:
        imported_databases = _import_postgres_databases(preflight, backup_path)
        restored_files: list[dict[str, object]] = []
        postgres_component_ids = {
            str(component["component_id"])
            for component in summary.get("components", [])
            if isinstance(component, dict) and component.get("kind") == "postgres"
        }
        for entry in summary["files"]:
            if entry["component_id"] in postgres_component_ids:
                continue
            relative = _safe_relative(entry["relative_path"])
            source = backup_path / relative
            if not source.is_file() or source.is_symlink():
                raise MaintenanceError("backup_file_missing")
            digest, size = _file_digest(source)
            if digest != entry["sha256"] or size != entry["size"]:
                raise MaintenanceError("backup_hash_mismatch")
            target_file = temporary / relative
            _copy_verified(source, target_file)
            restored_files.append(entry)
        restored_repositories = _restore_git_repositories(
            preflight, backup_path, temporary
        )
        payload_root = temporary / "payload"
        if not payload_root.is_dir() or payload_root.is_symlink():
            raise MaintenanceError("restore_payload_missing")
        os.chmod(payload_root, stat.S_IRWXU)
        for component in summary.get("components", []):
            if not isinstance(component, dict) or component.get("kind") != "sqlite":
                continue
            component_id = str(component["component_id"])
            sqlite_entries = [
                entry
                for entry in restored_files
                if entry["component_id"] == component_id
            ]
            if len(sqlite_entries) != 1:
                raise MaintenanceError("restore_sqlite_mapping_invalid")
            restored_db = temporary / _safe_relative(sqlite_entries[0]["relative_path"])
            try:
                with sqlite3.connect(restored_db) as connection:
                    integrity = connection.execute("PRAGMA integrity_check").fetchone()
            except sqlite3.Error as exc:
                raise MaintenanceError("restore_sqlite_verification_failed") from exc
            if integrity != ("ok",):
                raise MaintenanceError("restore_sqlite_verification_failed")
        result = {
            "schema_version": 1,
            "status": "COMPLETE",
            "restore_mode": "read_only",
            "run_id": target.get("run_id", summary["run_id"]),
            "workspace_id": target.get("workspace_id", summary["workspace_id"]),
            "source_run_id": summary["run_id"],
            "source_backup": str(backup_path),
            "files": restored_files,
            "database_imports": len(imported_databases),
            "database_facts": imported_databases,
            "repositories": restored_repositories,
            "requires_reconfiguration": True,
            "reauthorize_copied_ledger": True,
            "provider_calls": 0,
            "writes_enabled": False,
        }
        _write_json(temporary / "restore-result.json", result)
        os.replace(temporary, target_root)
    except Exception as exc:
        if imported_databases:
            failure_code = (
                exc.code if isinstance(exc, MaintenanceError) else "restore_failed"
            )
            _write_json(
                target_root.parent / f".{target_root.name}.restore-failure.json",
                {
                    "schema_version": 1,
                    "status": "INCOMPLETE",
                    "source_run_id": summary["run_id"],
                    "target_run_id": target.get("run_id", ""),
                    "error_code": failure_code,
                    "database_imports": imported_databases,
                    "provider_calls": 0,
                },
            )
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "status": "COMPLETE",
        "restore_mode": "read_only",
        "target_root": str(target_root),
        "source_run_id": summary["run_id"],
        "reauthorize_copied_ledger": True,
        "provider_calls": 0,
        "database_imports": len(imported_databases),
        "repositories": restored_repositories,
    }


def _load_context(manifest_path: Path) -> dict[str, object]:
    manifest_path = _absolute(manifest_path, "manifest")
    try:
        raw = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintenanceError("manifest_invalid") from exc
    if not isinstance(raw, dict):
        raise MaintenanceError("manifest_invalid")
    try:
        if raw.get("mode") == "interview-demo":
            manifest = load_demo_manifest(manifest_path)
            validate_demo_manifest(manifest, PROJECT_ROOT)
            kind = "demo"
            runtime_fingerprint = raw.get("runtime_fingerprint", "0" * 64)
        else:
            manifest = load_basic_manifest(manifest_path)
            validate_manifest_isolation(manifest, sidera_root=PROJECT_ROOT)
            kind = "basic"
            runtime_fingerprint = raw.get("runtime_fingerprint", "0" * 64)
    except Exception as exc:
        raise MaintenanceError("manifest_invalid") from exc
    return {
        "path": manifest_path,
        "raw": raw,
        "kind": kind,
        "run_id": str(raw.get("run_id")),
        "workspace_id": str(raw.get("workspace_id")),
        "run_root": Path(str(raw.get("run_root"))).expanduser(),
        "repository": Path(str(raw.get("repository"))).expanduser(),
        "source_commit": str(raw.get("code_commit", raw.get("base_commit", ""))),
        "implementation_fingerprint": str(
            raw.get("implementation_fingerprint", "0" * 64)
        ),
        "runtime_fingerprint": str(runtime_fingerprint),
    }


def _load_inventory(
    context: dict[str, object],
    *,
    source_inventory: Path | None = None,
    require_complete_coverage: bool = True,
    require_stopped: bool = True,
) -> dict[str, object]:
    path = source_inventory or _inventory_path(context, None)
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintenanceError("maintenance_inventory_invalid") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != INVENTORY_SCHEMA:
        raise MaintenanceError("maintenance_inventory_schema_unsupported")
    allowed = INVENTORY_REQUIRED_FIELDS | INVENTORY_OPTIONAL_FIELDS
    if set(raw) - allowed or not INVENTORY_REQUIRED_FIELDS.issubset(raw):
        raise MaintenanceError("maintenance_inventory_invalid")
    if str(raw["run_id"]) != context["run_id"] or str(raw["workspace_id"]) != context["workspace_id"]:
        raise MaintenanceError("maintenance_identity_mismatch")
    if (
        str(raw["code_commit"]) != context["source_commit"]
        or str(raw["implementation_fingerprint"]) != context["implementation_fingerprint"]
        or str(raw["runtime_fingerprint"]) != context["runtime_fingerprint"]
    ):
        raise MaintenanceError("maintenance_identity_mismatch")
    _validate_writer_state(raw["writers"], require_stopped=require_stopped)
    components = raw["components"]
    if not isinstance(components, list) or not components:
        raise MaintenanceError("maintenance_inventory_incomplete")
    component_ids: list[str] = []
    restore_roots: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            raise MaintenanceError("maintenance_component_invalid")
        component_id = component.get("component_id")
        if not isinstance(component_id, str) or not component_id.strip():
            raise MaintenanceError("maintenance_component_invalid")
        if component_id in component_ids:
            raise MaintenanceError("maintenance_component_duplicate")
        component_ids.append(component_id)
        restore_root = component.get("restore_relative_path")
        if not isinstance(restore_root, str):
            raise MaintenanceError("maintenance_component_invalid")
        restore_root = str(_safe_relative(restore_root))
        if restore_root in restore_roots:
            raise MaintenanceError("restore_path_conflict")
        restore_roots.add(restore_root)
    _validate_persistence_coverage(
        raw["persistence"],
        component_ids,
        require_complete=require_complete_coverage,
    )
    source_identities = raw.get("source_identities")
    if source_identities is not None:
            if not isinstance(source_identities, dict):
                raise MaintenanceError("maintenance_inventory_invalid")
            raw_context = context.get("raw")
            if isinstance(raw_context, dict):
                identity_fields = {
                    "database_ref": ("database_ref", "database_name"),
                    "knowledge_database_ref": (
                        "knowledge_database_ref",
                        "knowledge_database_name",
                    ),
                }
                for field, candidates in identity_fields.items():
                    if field not in source_identities:
                        continue
                    expected = next(
                        (
                            raw_context[candidate]
                            for candidate in candidates
                            if raw_context.get(candidate) is not None
                        ),
                        None,
                    )
                    if expected is None or source_identities[field] != expected:
                        raise MaintenanceError("maintenance_identity_mismatch")
    raw["components"] = components
    return raw


def _validate_persistence_coverage(
    value: object,
    component_ids: list[str],
    *,
    require_complete: bool = True,
) -> None:
    if not isinstance(value, dict) or set(value) != PERSISTENCE_FIELDS:
        raise MaintenanceError("backup_inventory_incomplete")
    if value["status"] not in {"complete", "incomplete"}:
        raise MaintenanceError("backup_inventory_incomplete")
    declared = value["component_ids"]
    unsupported = value["unsupported_component_ids"]
    if not _unique_text_list(declared) or not _unique_text_list(unsupported):
        raise MaintenanceError("backup_inventory_incomplete")
    if set(declared) != set(component_ids) or not set(unsupported).issubset(
        set(component_ids)
    ):
        raise MaintenanceError("backup_inventory_incomplete")
    if value["status"] == "complete" and unsupported:
        raise MaintenanceError("backup_inventory_incomplete")
    if require_complete and (value["status"] != "complete" or unsupported):
        raise MaintenanceError("backup_inventory_incomplete")


def _validate_restore_target(
    target: dict[str, object],
    *,
    target_manifest_path: Path,
    backup_path: Path,
    source_run_id: str,
) -> Path:
    target_root_value = target.get("run_root") or target.get("restore_root")
    if not isinstance(target_root_value, str) or not target_root_value:
        raise MaintenanceError("target_root_required")
    target_root = _absolute(Path(target_root_value), "target_root")
    resolved_target = target_root.resolve(strict=False)
    resolved_backup = backup_path.resolve(strict=False)
    if _within(resolved_target, resolved_backup) or _within(
        resolved_backup, resolved_target
    ):
        raise MaintenanceError("target_inside_backup")
    if _within(resolved_target, PROJECT_ROOT.resolve()) or resolved_target == Path.home().resolve():
        raise MaintenanceError("target_scope_invalid")
    if _within(target_manifest_path.resolve(strict=False), resolved_backup):
        raise MaintenanceError("target_manifest_inside_backup")
    if str(target.get("run_id", "")) == source_run_id:
        raise MaintenanceError("restore_target_identity_conflict")
    _validate_target_urls(target)
    return target_root


def _validate_target_urls(target: dict[str, object]) -> None:
    values: list[object] = []
    endpoints = target.get("endpoints")
    if isinstance(endpoints, dict):
        values.extend(endpoints.values())
    values.extend(value for key, value in target.items() if key.endswith("_url"))
    for value in values:
        if not isinstance(value, str):
            raise MaintenanceError("target_endpoint_invalid")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise MaintenanceError("target_endpoint_invalid")
def _validate_writer_state(value: object, *, require_stopped: bool = True) -> None:
    if not isinstance(value, dict) or set(value) != WRITER_FIELDS:
        raise MaintenanceError("backup_inventory_incomplete")
    if value["status"] not in {"stopped", "running", "unknown"}:
        raise MaintenanceError("writers_not_stopped")
    if not _unique_text_list(value["active_component_ids"]):
        raise MaintenanceError("backup_inventory_incomplete")
    if require_stopped and (
        value["status"] != "stopped" or value["active_component_ids"]
    ):
        raise MaintenanceError("writers_not_stopped")
    if not isinstance(value["observed_at"], str) or not value["observed_at"].strip():
        raise MaintenanceError("backup_inventory_incomplete")


def _tool_version(name: str) -> str | None:
    executable = shutil.which(name)
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (completed.stdout or completed.stderr).strip()
    return output.splitlines()[0][:200] if output else None


def _inspect_manifest_ports(
    context: dict[str, object],
) -> tuple[dict[str, dict[str, object]], list[str]]:
    raw = context.get("raw")
    if not isinstance(raw, dict):
        return {}, ["manifest_endpoints_unavailable"]
    values: dict[str, str] = {}
    endpoints = raw.get("endpoints")
    if isinstance(endpoints, dict):
        values.update(
            {
                str(name): value
                for name, value in endpoints.items()
                if isinstance(value, str)
            }
        )
    values.update(
        {
            key: value
            for key, value in raw.items()
            if key.endswith("_url") and isinstance(value, str)
        }
    )
    facts: dict[str, dict[str, object]] = {}
    issues: list[str] = []
    for name, value in sorted(values.items()):
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError:
            port = None
        if parsed.hostname is None or port is None:
            issues.append(f"endpoint_invalid:{name}")
            continue
        occupied = False
        try:
            with socket.create_connection((parsed.hostname, port), timeout=0.2):
                occupied = True
        except OSError:
            pass
        facts[name] = {
            "host": parsed.hostname,
            "port": port,
            "occupied": occupied,
        }
    return facts, issues


def _process_snapshot(pid: int) -> tuple[str, str] | None:
    executable = shutil.which("ps")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [
                executable,
                "-p",
                str(pid),
                "-o",
                "stat=",
                "-o",
                "lstart=",
                "-o",
                "command=",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    fields = completed.stdout.strip().split(maxsplit=6)
    if len(fields) < 7 or fields[0].startswith("Z") or fields[6] == "<defunct>":
        return None
    command = fields[6]
    return " ".join(fields[1:6]), hashlib.sha256(command.encode()).hexdigest()


def _read_key_value_identity(path: Path) -> dict[str, str] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or not key or key in values:
            return None
        values[key] = value
    return values


def _inspect_writer_identity(
    context: dict[str, object], writers: dict[str, object]
) -> tuple[dict[str, object], list[str]]:
    run_root = Path(str(context["run_root"])).expanduser()
    status = str(writers["status"])
    active = set(writers["active_component_ids"])
    if context["kind"] == "demo":
        identity_paths = sorted((run_root / "processes").glob("*.json"))
        mode = "demo"
        receipt_path = None
    else:
        runtime_value = os.environ.get("SIDERA_R6_BASIC_RUNTIME_DIR")
        runtime = (
            Path(runtime_value).expanduser()
            if runtime_value
            else run_root / ".sidera-runtime" / "r6-basic"
        )
        identity_paths = sorted((runtime / "identity").glob("*.identity"))
        mode = "basic"
        receipt_path = runtime / "stopped-receipt.json"
    facts: dict[str, object] = {
        "mode": mode,
        "status": status,
        "identity_count": len(identity_paths),
        "components": {},
    }
    issues: list[str] = []
    for path in identity_paths:
        component = path.stem
        raw: dict[str, object] | None
        if path.suffix == ".json":
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = None
            raw = loaded if isinstance(loaded, dict) else None
        else:
            raw = _read_key_value_identity(path)
        if raw is None:
            issues.append(f"writer_identity_invalid:{component}")
            continue
        try:
            pid = int(raw["pid"])
        except (KeyError, TypeError, ValueError):
            issues.append(f"writer_identity_invalid:{component}")
            continue
        expected = {
            "run_id": str(context["run_id"]),
            "workspace_id": str(context["workspace_id"]),
            "component": component,
        }
        if any(str(raw.get(key)) != value for key, value in expected.items()):
            issues.append(f"writer_identity_mismatch:{component}")
        snapshot = _process_snapshot(pid)
        live = snapshot is not None
        identity_matches = live and (
            snapshot[0] == str(raw.get("started_at"))
            and snapshot[1] == str(raw.get("argv_hash"))
        )
        facts["components"][component] = {
            "pid": pid,
            "live": live,
            "identity_matches": identity_matches,
        }
        if live and not identity_matches:
            issues.append(f"writer_identity_mismatch:{component}")
        if status == "stopped" and identity_matches:
            issues.append(f"writer_process_alive:{component}")
        if status != "stopped" and component in active and not identity_matches:
            issues.append(f"writer_identity_unverified:{component}")
    if status != "stopped" and active:
        observed = set(facts["components"])
        for component in sorted(active - observed):
            issues.append(f"writer_identity_missing:{component}")
    if mode == "basic" and status == "stopped" and not identity_paths:
        if receipt_path is None or not receipt_path.is_file():
            issues.append("writer_stop_receipt_missing")
        else:
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                receipt = None
            if not isinstance(receipt, dict) or not isinstance(
                receipt.get("components"), dict
            ):
                issues.append("writer_stop_receipt_invalid")
            else:
                facts["stopped_receipt"] = {
                    "components": sorted(receipt["components"]),
                    "verified_exit": True,
                }
                for component, raw in receipt["components"].items():
                    if not isinstance(raw, dict):
                        issues.append(f"writer_stop_receipt_invalid:{component}")
                        continue
                    try:
                        pid = int(raw["pid"])
                    except (KeyError, TypeError, ValueError):
                        issues.append(f"writer_stop_receipt_invalid:{component}")
                        continue
                    snapshot = _process_snapshot(pid)
                    if snapshot is not None:
                        if snapshot[0] == str(raw.get("process_start")) and snapshot[
                            1
                        ] == str(raw.get("argv_hash")):
                            issues.append(f"writer_process_alive:{component}")
                        else:
                            issues.append(f"writer_stop_receipt_pid_reused:{component}")
    return facts, issues


def _inspect_permissions(
    context: dict[str, object],
) -> tuple[dict[str, object], list[str]]:
    run_root = Path(str(context["run_root"])).expanduser()
    paths = {"run_root": run_root}
    try:
        paths["inventory"] = _inventory_path(context, None)
    except MaintenanceError:
        pass
    facts: dict[str, object] = {}
    issues: list[str] = []
    for name, path in paths.items():
        if not path.exists() or path.is_symlink():
            facts[name] = {"exists": False}
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        facts[name] = {"exists": True, "mode": oct(mode)}
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            issues.append(f"permission_not_private:{name}")
    return facts, issues


def _inspect_postgres_components(
    components: list[object],
    context: dict[str, object],
    persistence: dict[str, object],
) -> tuple[dict[str, object], list[str], bool]:
    facts: dict[str, object] = {}
    issues: list[str] = []
    credentials_used = False
    unsupported = set(persistence["unsupported_component_ids"])
    for component in components:
        if not isinstance(component, dict) or component.get("kind") != "postgres":
            continue
        component_id = str(component.get("component_id", "unknown"))
        coverage = "unsupported" if component_id in unsupported else "supported"
        try:
            dsn, environment = _postgres_dump_config(component, context)
        except MaintenanceError as exc:
            issues.append(f"database:{component_id}:{exc.code}")
            continue
        credentials_used = credentials_used or "PGPASSWORD" in environment
        if shutil.which("pg_isready") is None or shutil.which("psql") is None:
            issues.append(f"database:{component_id}:database_tools_missing")
            continue
        try:
            ready = subprocess.run(
                [str(shutil.which("pg_isready")), "--dbname", dsn],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            issues.append(f"database:{component_id}:database_probe_failed")
            continue
        if ready.returncode != 0:
            issues.append(f"database:{component_id}:database_unready")
            continue
        try:
            identity = _run_psql(
                str(shutil.which("psql")),
                dsn,
                "SELECT current_database(), current_user",
                environment,
            )
            heads = _run_psql(
                str(shutil.which("psql")),
                dsn,
                "SELECT version_num FROM alembic_version ORDER BY version_num",
                environment,
            )
            writers = _run_psql(
                str(shutil.which("psql")),
                dsn,
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() AND state <> 'idle'",
                environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            issues.append(f"database:{component_id}:identity_probe_failed")
            continue
        if identity.returncode != 0:
            issues.append(f"database:{component_id}:identity_probe_failed")
        if heads.returncode != 0:
            issues.append(f"database:{component_id}:heads_probe_failed")
        values = [line.strip() for line in heads.stdout.splitlines() if line.strip()]
        if len(values) != 1:
            issues.append(f"database:{component_id}:expected_single_head")
        active_writer_count = writers.stdout.strip()
        if active_writer_count != "0":
            issues.append(f"database:{component_id}:active_writer_sessions")
        facts[component_id] = {
            "status": "ready" if identity.returncode == 0 and heads.returncode == 0 else "error",
            "coverage": coverage,
            "identity": identity.stdout.strip().splitlines()[0] if identity.stdout.strip() else None,
            "heads": values,
            "active_writer_sessions": active_writer_count or None,
        }
    return facts, issues, credentials_used


def _run_psql(
    executable: str,
    dsn: str,
    query: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            executable,
            "--no-psqlrc",
            "--no-align",
            "--tuples-only",
            "--dbname",
            dsn,
            "--command",
            query,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        env=environment,
    )


def _inventory_path(context: dict[str, object], source_inventory: Path | None) -> Path:
    if source_inventory is not None:
        return _absolute(source_inventory, "source_inventory")
    value = context["raw"].get("maintenance_inventory_ref")
    if not isinstance(value, str) or not value.strip():
        raise MaintenanceError("maintenance_inventory_missing")
    path = _absolute(Path(value.removeprefix("file:")), "maintenance_inventory")
    if path.is_symlink():
        raise MaintenanceError("external_symlink_rejected")
    run_root = Path(str(context["run_root"])).resolve(strict=False)
    resolved = path.resolve(strict=False)
    if not _within(resolved, run_root):
        raise MaintenanceError("maintenance_inventory_outside_run_root")
    return resolved


def _validate_component(component: object, context: dict[str, object]) -> None:
    if not isinstance(component, dict):
        raise MaintenanceError("maintenance_component_invalid")
    fields = {
        "component_id",
        "kind",
        "source_ref",
        "restore_relative_path",
        "schema_identity",
        "required",
        "secret_policy",
    }
    if set(component) - fields or not fields.issubset(component):
        raise MaintenanceError("maintenance_component_invalid")
    if not isinstance(component["component_id"], str) or not component["component_id"].strip():
        raise MaintenanceError("maintenance_component_invalid")
    if component["kind"] not in ALLOWED_KINDS:
        raise MaintenanceError("maintenance_kind_unsupported")
    if component["secret_policy"] not in ALLOWED_SECRET_POLICIES:
        raise MaintenanceError("maintenance_secret_policy_invalid")
    if not isinstance(component["required"], bool):
        raise MaintenanceError("maintenance_component_invalid")
    if component["required"] and component["secret_policy"] == "exclude":
        raise MaintenanceError("required_component_excluded")
    _safe_relative(str(component["restore_relative_path"]))
    if not isinstance(component["schema_identity"], str) or not component["schema_identity"].strip():
        raise MaintenanceError("maintenance_component_invalid")
    source = component["source_ref"]
    if not isinstance(source, str) or not source.strip() or source.startswith("env:"):
        raise MaintenanceError("maintenance_source_ref_invalid")
    if component["kind"] == "postgres":
        reference = source.removeprefix("postgres:ref:")
        if reference == source or not reference:
            raise MaintenanceError("database_reference_invalid")
        if reference.startswith("env:"):
            if not reference.removeprefix("env:"):
                raise MaintenanceError("database_reference_invalid")
        elif reference.startswith("file:"):
            if not reference.removeprefix("file:"):
                raise MaintenanceError("database_reference_invalid")
        else:
            raise MaintenanceError("database_reference_invalid")
        return
    if component["kind"] == "git":
        _git_component_source(component, context)
        return
    if component["kind"] == "sqlite" and source.startswith("sqlite:file:"):
        source_path = _absolute(
            Path(source.removeprefix("sqlite:file:")), "component_source"
        )
        if source_path in {Path("/"), Path.home().resolve(), PROJECT_ROOT.resolve()}:
            raise MaintenanceError("broad_source_path_rejected")
        if component["secret_policy"] != "exclude":
            _component_source(component, context)
        return
    source = source.removeprefix("file:")
    source_path = _absolute(Path(source), "component_source")
    if source_path in {Path("/"), Path.home().resolve(), PROJECT_ROOT.resolve()}:
        raise MaintenanceError("broad_source_path_rejected")
    if component["kind"] == "sqlite" and source_path.is_file():
        raise MaintenanceError("database_source_requires_export")
    if component["secret_policy"] != "exclude":
        _component_source(component, context)


def _component_source(component: dict[str, object], context: dict[str, object]) -> Path:
    source = str(component["source_ref"])
    if source.startswith("sqlite:file:"):
        source = source.removeprefix("sqlite:file:")
    else:
        source = source.removeprefix("file:")
    path = _absolute(Path(source), "component_source")
    if path.is_symlink():
        raise MaintenanceError("external_symlink_rejected")
    if not path.exists():
        raise MaintenanceError("component_source_unavailable")
    if context.get("kind") in {"demo", "basic"}:
        allowed_roots = (
            Path(str(context["run_root"])).expanduser().resolve(strict=False),
            Path(str(context["repository"])).expanduser().resolve(strict=False),
        )
        resolved = path.resolve(strict=False)
        if not any(_within(resolved, root) for root in allowed_roots):
            raise MaintenanceError("component_source_outside_allowed_root")
    _reject_symlink_tree(path)
    _reject_hardlink_tree(path)
    return path


def _git_component_source(
    component: dict[str, object], context: dict[str, object]
) -> Path:
    source = str(component["source_ref"]).removeprefix("file:")
    path = _absolute(Path(source), "component_source")
    if path in {Path("/"), Path.home().resolve(), PROJECT_ROOT.resolve()}:
        raise MaintenanceError("broad_source_path_rejected")
    if path.is_symlink() or not path.is_dir():
        raise MaintenanceError("component_source_unavailable")
    if context.get("kind") in {"demo", "basic"}:
        allowed_roots = (
            Path(str(context["run_root"])).expanduser().resolve(strict=False),
            Path(str(context["repository"])).expanduser().resolve(strict=False),
        )
        resolved = path.resolve(strict=False)
        if not any(_within(resolved, root) for root in allowed_roots):
            raise MaintenanceError("component_source_outside_allowed_root")
    if not (path / ".git").exists():
        return path
    git = shutil.which("git")
    if git is None:
        raise MaintenanceError("git_missing")
    try:
        tracked = subprocess.run(
            [git, "-C", str(path), "ls-files", "--stage", "-z"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MaintenanceError("git_snapshot_failed") from exc
    if tracked.returncode != 0:
        raise MaintenanceError("git_snapshot_failed")
    tracked_symlinks = {
        os.fsdecode(record.split(b"\t", 1)[1])
        for record in tracked.stdout.split(b"\0")
        if record.startswith(b"120000 ") and b"\t" in record
    }
    for child in path.rglob("*"):
        if ".git" in child.relative_to(path).parts or not child.is_symlink():
            continue
        relative = str(child.relative_to(path))
        if relative not in tracked_symlinks:
            raise MaintenanceError("external_symlink_rejected")
        if not _within(child.resolve(strict=False), path.resolve(strict=False)):
            raise MaintenanceError("external_symlink_rejected")
    _reject_hardlink_tree(path)
    return path


def _snapshot_sqlite(
    component: dict[str, object], context: dict[str, object], temporary: Path
) -> Path:
    source = _component_source(component, context)
    relative = _safe_relative(str(component["restore_relative_path"]))
    target = temporary / "payload" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_connection = sqlite3.connect(source)
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
            integrity = target_connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            target_connection.close()
            source_connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise MaintenanceError("sqlite_backup_failed") from exc
    if integrity != ("ok",):
        raise MaintenanceError("sqlite_integrity_check_failed")
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    return relative


def _snapshot_postgres(
    component: dict[str, object], context: dict[str, object], temporary: Path
) -> Path:
    if shutil.which("pg_dump") is None:
        raise MaintenanceError("pg_dump_missing")
    dsn, environment = _postgres_dump_config(component, context)
    root = _safe_relative(str(component["restore_relative_path"]))
    relative = root if root.suffix == ".dump" else root.with_name(f"{root.name}.dump")
    target = temporary / "payload" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(target),
        "--dbname",
        dsn,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    except OSError as exc:
        raise MaintenanceError("pg_dump_failed") from exc
    if completed.returncode != 0 or not target.is_file():
        raise MaintenanceError("pg_dump_failed")
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    return relative


def _snapshot_git(
    component: dict[str, object],
    context: dict[str, object],
    temporary: Path,
) -> tuple[list[Path], dict[str, object]]:
    source = _git_component_source(component, context)
    if not (source / ".git").exists():
        raise MaintenanceError("git_snapshot_required")
    git = shutil.which("git")
    if git is None:
        raise MaintenanceError("git_missing")

    def run_git(*args: str, binary: bool = False) -> str | bytes:
        try:
            completed = subprocess.run(
                [git, "-C", str(source), *args],
                capture_output=True,
                text=not binary,
                timeout=30,
                check=False,
                env={
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_OPTIONAL_LOCKS": "0",
                    "PATH": os.environ.get("PATH", ""),
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MaintenanceError("git_snapshot_failed") from exc
        if completed.returncode != 0:
            raise MaintenanceError("git_snapshot_failed")
        return completed.stdout

    is_work_tree = str(run_git("rev-parse", "--is-inside-work-tree")).strip()
    if is_work_tree != "true":
        raise MaintenanceError("git_snapshot_required")
    top_level = Path(str(run_git("rev-parse", "--show-toplevel")).strip()).resolve()
    if top_level != source.resolve():
        raise MaintenanceError("git_snapshot_required")
    git_common_dir = Path(str(run_git("rev-parse", "--git-common-dir")).strip())
    if not git_common_dir.is_absolute():
        git_common_dir = (source / git_common_dir).resolve()
    else:
        git_common_dir = git_common_dir.resolve()
    expected_git_dir = (source / ".git").resolve()
    if git_common_dir != expected_git_dir:
        raise MaintenanceError("git_common_dir_external")

    head = str(run_git("rev-parse", "HEAD")).strip()
    tree = str(run_git("rev-parse", "HEAD^{tree}")).strip()
    status_bytes = run_git("status", "--porcelain=v1", "-z", binary=True)
    if not isinstance(status_bytes, bytes):
        raise MaintenanceError("git_snapshot_failed")
    status_records: list[str] = []
    staged_paths: list[str] = []
    unstaged_paths: list[str] = []
    untracked_paths: list[str] = []
    deleted_paths: list[str] = []
    worktree_paths: list[Path] = []
    for raw_record in status_bytes.split(b"\0"):
        if not raw_record:
            continue
        record = os.fsdecode(raw_record)
        if len(record) < 4 or record[2] != " ":
            raise MaintenanceError("git_worktree_state_unsupported")
        xy = record[:2]
        relative_text = record[3:]
        relative = _safe_relative(relative_text)
        if relative.parts[0] == ".git":
            raise MaintenanceError("git_worktree_state_unsupported")
        if "R" in xy or "C" in xy:
            raise MaintenanceError("git_worktree_state_unsupported")
        status_records.append(record)
        if xy == "??":
            untracked_paths.append(str(relative))
        else:
            if xy[0] != " ":
                staged_paths.append(str(relative))
            if xy[1] != " ":
                unstaged_paths.append(str(relative))
        source_file = source / relative
        if source_file.is_file():
            if _looks_sensitive(source_file):
                raise MaintenanceError("sensitive_path_not_excluded")
            _reject_symlink_tree(source_file)
            _reject_hardlink_tree(source_file)
            worktree_paths.append(relative)
        elif xy[1] == "D" or xy[0] == "D":
            deleted_paths.append(str(relative))

    root = _safe_relative(str(component["restore_relative_path"]))
    bundle_relative = root / "repository.bundle"
    bundle_path = temporary / "payload" / bundle_relative
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_result = subprocess.run(
        [git, "-C", str(source), "bundle", "create", str(bundle_path), "--all"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "PATH": os.environ.get("PATH", ""),
        },
    )
    if bundle_result.returncode != 0 or not bundle_path.is_file():
        raise MaintenanceError("git_bundle_failed")
    verify_result = subprocess.run(
        [git, "bundle", "verify", str(bundle_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={"GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ.get("PATH", "")},
    )
    if verify_result.returncode != 0:
        raise MaintenanceError("git_bundle_invalid")

    staged_patch = run_git("diff", "--cached", "--binary", "--no-ext-diff", binary=True)
    unstaged_patch = run_git("diff", "--binary", "--no-ext-diff", binary=True)
    if not isinstance(staged_patch, bytes) or not isinstance(unstaged_patch, bytes):
        raise MaintenanceError("git_snapshot_failed")
    patch_paths: dict[str, Path] = {}
    for name, content in (("staged.patch", staged_patch), ("unstaged.patch", unstaged_patch)):
        if content:
            patch_path = temporary / "payload" / root / name
            patch_path.write_bytes(content)
            os.chmod(patch_path, stat.S_IRUSR | stat.S_IWUSR)
            patch_paths[name] = root / name

    worktree_root = root / "worktree"
    (temporary / "payload" / worktree_root).mkdir(parents=True, exist_ok=True)
    for relative in worktree_paths:
        target = temporary / "payload" / worktree_root / relative
        _copy_verified(source / relative, target)

    state_relative = root / "git-state.json"
    state = {
        "schema_version": 1,
        "head": head,
        "tree": tree,
        "bundle_path": str(bundle_relative),
        "worktree_root": str(worktree_root),
        "status_porcelain": status_records,
        "staged_paths": sorted(staged_paths),
        "unstaged_paths": sorted(unstaged_paths),
        "untracked_paths": sorted(untracked_paths),
        "deleted_paths": sorted(deleted_paths),
        "patches": {name: str(path) for name, path in patch_paths.items()},
    }
    _write_json(temporary / "payload" / state_relative, state)

    verification_root = Path(tempfile.mkdtemp(prefix="sidera-git-verify-"))
    detached = verification_root / "repository"
    try:
        clone = subprocess.run(
            [git, "clone", "--no-local", str(bundle_path), str(detached)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={"GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ.get("PATH", "")},
        )
        if clone.returncode != 0:
            raise MaintenanceError("git_detached_restore_failed")
        cloned_head = str(
            subprocess.run(
                [git, "-C", str(detached), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env={"GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ.get("PATH", "")},
            ).stdout
        ).strip()
        cloned_tree = str(
            subprocess.run(
                [git, "-C", str(detached), "rev-parse", "HEAD^{tree}"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env={"GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ.get("PATH", "")},
            ).stdout
        ).strip()
        if (
            cloned_head != head
            or cloned_tree != tree
            or not (detached / ".git").is_dir()
            or (detached / ".git" / "commondir").exists()
        ):
            raise MaintenanceError("git_detached_restore_failed")
    finally:
        shutil.rmtree(verification_root, ignore_errors=True)

    relatives = [bundle_relative, state_relative, *patch_paths.values()]
    relatives.extend(worktree_root / relative for relative in worktree_paths)
    return relatives, {
        "schema_identity": component["schema_identity"],
        "head": head,
        "tree": tree,
        "status_porcelain": status_records,
        "staged_paths": sorted(staged_paths),
        "unstaged_paths": sorted(unstaged_paths),
        "untracked_paths": sorted(untracked_paths),
        "deleted_paths": sorted(deleted_paths),
        "bundle_path": str(bundle_relative),
        "worktree_root": str(worktree_root),
        "detached_restore_verified": True,
        "source_common_dir_external": False,
    }


def _postgres_backup_facts(
    components: list[object],
    context: dict[str, object],
    persistence: object,
) -> dict[str, dict[str, object]]:
    postgres_components = [
        component
        for component in components
        if isinstance(component, dict) and component.get("kind") == "postgres"
    ]
    if not postgres_components:
        return {}
    if not isinstance(persistence, dict):
        raise MaintenanceError("database_identity_unverified")
    facts, issues, _ = _inspect_postgres_components(
        postgres_components,
        context,
        persistence,
    )
    if issues:
        raise MaintenanceError("database_stop_not_confirmed")
    result: dict[str, dict[str, object]] = {}
    for component in postgres_components:
        component_id = str(component["component_id"])
        fact = facts.get(component_id)
        if not isinstance(fact, dict) or fact.get("status") != "ready":
            raise MaintenanceError("database_identity_unverified")
        result[component_id] = {
            **fact,
            "schema_identity": component["schema_identity"],
        }
    return result


def _verify_postgres_dump(
    dump_path: Path,
    component: dict[str, object],
    context: dict[str, object],
) -> int:
    executable = shutil.which("pg_restore")
    if executable is None:
        raise MaintenanceError("pg_restore_missing")
    _, environment = _postgres_dump_config(component, context)
    try:
        completed = subprocess.run(
            [executable, "--list", str(dump_path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MaintenanceError("pg_restore_list_failed") from exc
    if completed.returncode != 0:
        raise MaintenanceError("pg_restore_list_failed")
    entries = [
        line
        for line in completed.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith(";")
    ]
    if not entries:
        raise MaintenanceError("pg_restore_list_failed")
    return len(entries)


def _postgres_dump_config(
    component: dict[str, object], context: dict[str, object]
) -> tuple[str, dict[str, str]]:
    source = str(component["source_ref"])
    reference = source.removeprefix("postgres:ref:")
    if reference == source:
        raise MaintenanceError("database_reference_unavailable")
    if reference.startswith("env:"):
        variable = reference.removeprefix("env:")
        value = os.environ.get(variable)
        if not value:
            raise MaintenanceError("database_reference_unavailable")
    elif reference.startswith("file:"):
        path = _absolute(Path(reference.removeprefix("file:")), "database_reference")
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise MaintenanceError("database_reference_unavailable") from exc
    else:
        raise MaintenanceError("database_reference_invalid")
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"} or not parsed.hostname:
        raise MaintenanceError("database_reference_invalid")
    username = quote(unquote(parsed.username or ""), safe="")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{username}@" if username else ""
    netloc += host
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    sanitized = urlunsplit(
        ("postgresql", netloc, parsed.path, parsed.query, "")
    )
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PGPASSFILE": os.environ.get("PGPASSFILE", ""),
    }
    if parsed.password is not None:
        environment["PGPASSWORD"] = unquote(parsed.password)
    if not environment["PGPASSFILE"]:
        environment.pop("PGPASSFILE")
    return sanitized, environment


def _source_files(source: Path, kind: str) -> list[tuple[Path, Path]]:
    if source.is_file():
        return [(source, Path("."))]
    if kind in {"postgres", "sqlite", "metadata", "ledger"} and not source.is_dir():
        raise MaintenanceError("component_source_unavailable")
    result: list[tuple[Path, Path]] = []
    for child in sorted(source.rglob("*")):
        if child.is_symlink():
            raise MaintenanceError("external_symlink_rejected")
        if child.is_file():
            result.append((child, child.relative_to(source)))
        elif not child.is_dir():
            raise MaintenanceError("component_file_type_unsupported")
    return result


def _restore_path(root: str, relative: Path) -> Path:
    if relative == Path("."):
        return _safe_relative(root)
    return _safe_relative(str(Path(root) / relative))


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MaintenanceError("restore_path_invalid")
    return path


def _reject_symlink_tree(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise MaintenanceError("external_symlink_rejected")
        current = current.parent
    if path.is_dir():
        for child in path.rglob("*"):
            if child.is_symlink():
                raise MaintenanceError("external_symlink_rejected")


def _reject_hardlink_tree(path: Path) -> None:
    candidates = [path] if path.is_file() else path.rglob("*")
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_nlink > 1:
            raise MaintenanceError("hardlink_source_rejected")


def _ensure_destination_not_in_source(destination: Path, source: Path) -> None:
    destination_resolved = destination.resolve(strict=False)
    source_resolved = source.resolve(strict=False)
    if _within(destination_resolved, source_resolved) or _within(
        source_resolved, destination_resolved
    ):
        raise MaintenanceError("destination_inside_source")


def _looks_sensitive(path: Path) -> bool:
    names = {part.lower() for part in path.parts}
    if names & SENSITIVE_NAMES:
        return True
    return path.name.lower().startswith(".env") or "keychain" in path.name.lower()


def _copy_verified(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, stat.S_IRWXU)
    with source.open("rb") as source_file, target.open("wb") as target_file:
        shutil.copyfileobj(source_file, target_file, length=1024 * 1024)
        target_file.flush()
        os.fsync(target_file.fileno())
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _sha256_file(path: Path) -> str:
    return _file_digest(path)[0]


def _verify_backup_manifest(root: Path, manifest: dict[str, object]) -> None:
    if manifest.get("status") != "COMPLETE" or manifest.get("restore_mode") != "read_only":
        raise MaintenanceError("backup_not_complete")
    components = manifest.get("components")
    if not isinstance(components, list) or not components:
        raise MaintenanceError("backup_manifest_invalid")
    component_by_id: dict[str, dict[str, object]] = {}
    for component in components:
        if not isinstance(component, dict):
            raise MaintenanceError("backup_manifest_invalid")
        component_id = component.get("component_id")
        if (
            not isinstance(component_id, str)
            or not component_id.strip()
            or component_id in component_by_id
            or component.get("kind") not in ALLOWED_KINDS
            or not isinstance(component.get("schema_identity"), str)
            or not component["schema_identity"].strip()
            or not isinstance(component.get("required"), bool)
            or component.get("secret_policy") not in ALLOWED_SECRET_POLICIES
        ):
            raise MaintenanceError("backup_manifest_invalid")
        component_by_id[component_id] = component
    excluded = manifest.get("excluded", [])
    if not isinstance(excluded, list):
        raise MaintenanceError("backup_manifest_invalid")
    excluded_ids: set[str] = set()
    for item in excluded:
        if not isinstance(item, dict):
            raise MaintenanceError("backup_manifest_invalid")
        component_id = item.get("component_id")
        reason = item.get("reason")
        if (
            not isinstance(component_id, str)
            or component_id in excluded_ids
            or component_id not in component_by_id
            or not isinstance(reason, str)
            or not reason.strip()
            or component_by_id[component_id]["required"]
        ):
            raise MaintenanceError("backup_manifest_invalid")
        excluded_ids.add(component_id)
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise MaintenanceError("backup_manifest_invalid")
    seen: set[str] = set()
    file_counts: dict[str, int] = {component_id: 0 for component_id in component_by_id}
    for entry in entries:
        if not isinstance(entry, dict):
            raise MaintenanceError("backup_manifest_invalid")
        relative = _safe_relative(str(entry.get("relative_path", "")))
        component_id = entry.get("component_id")
        if (
            str(relative) in seen
            or not isinstance(entry.get("sha256"), str)
            or not isinstance(component_id, str)
            or component_id not in component_by_id
            or component_id in excluded_ids
            or not isinstance(entry.get("size"), int)
            or entry["size"] < 0
        ):
            raise MaintenanceError("backup_manifest_invalid")
        seen.add(str(relative))
        file_counts[component_id] += 1
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise MaintenanceError("backup_file_missing")
        digest, size = _file_digest(path)
        if digest != entry["sha256"] or size != entry.get("size"):
            raise MaintenanceError("backup_hash_mismatch")
    for component_id, component in component_by_id.items():
        if component["required"] and file_counts[component_id] == 0:
            raise MaintenanceError("backup_manifest_invalid")
    payload_root = root / "payload"
    if not payload_root.is_dir() or payload_root.is_symlink():
        raise MaintenanceError("backup_manifest_invalid")
    payload_files: set[str] = set()
    for path in payload_root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            if path.is_symlink() or not path.is_dir():
                raise MaintenanceError("backup_manifest_invalid")
            continue
        payload_files.add(str(path.relative_to(root)))
    if payload_files != seen:
        raise MaintenanceError("backup_manifest_invalid")


def _public_backup_summary(root: Path, manifest: dict[str, object]) -> dict[str, object]:
    return {
        "status": manifest["status"],
        "backup": str(root),
        "run_id": manifest["run_id"],
        "workspace_id": manifest["workspace_id"],
        "components": manifest.get("components", []),
        "files": manifest["files"],
        "database_facts": manifest.get("database_facts", {}),
        "git_facts": manifest.get("git_facts", {}),
        "excluded": manifest.get("excluded", []),
        "requires_reconfiguration": manifest["requires_reconfiguration"],
        "restore_mode": manifest["restore_mode"],
        "provider_calls": 0,
    }


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary, path)


def _absolute(path: Path, label: str) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise MaintenanceError(f"{label}_must_be_absolute")
    return path


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _unique_text_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and item.strip() for item in value)
        and len(value) == len(set(value))
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--manifest", type=Path, required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--manifest", type=Path, required=True)
    backup_parser.add_argument("--source-inventory", type=Path)
    backup_parser.add_argument("--destination", type=Path, required=True)
    inspect_parser = subparsers.add_parser("inspect-backup")
    inspect_parser.add_argument("--backup", type=Path, required=True)
    preflight_parser = subparsers.add_parser("restore-preflight")
    preflight_parser.add_argument("--backup", type=Path, required=True)
    preflight_parser.add_argument("--target-manifest", type=Path, required=True)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--backup", type=Path, required=True)
    restore_parser.add_argument("--target-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor(args.manifest)
        elif args.command == "backup":
            result = backup(
                args.manifest,
                args.destination,
                source_inventory=args.source_inventory,
            )
        elif args.command == "inspect-backup":
            result = inspect_backup(args.backup)
        elif args.command == "restore-preflight":
            result = restore_preflight(args.backup, args.target_manifest)
        else:
            result = restore(args.backup, args.target_manifest)
    except (MaintenanceError, OSError) as exc:
        code = exc.code if isinstance(exc, MaintenanceError) else "maintenance_io_error"
        print(json.dumps({"status": "INCOMPLETE", "error_code": code}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"PASS", "READY", "COMPLETE"} else 2


if __name__ == "__main__":
    raise SystemExit(_main())
