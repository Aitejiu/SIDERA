from __future__ import annotations

import importlib.util
import json
import sqlite3
import stat
import subprocess
import types
from pathlib import Path
from types import ModuleType

import pytest


def _module() -> ModuleType:
    path = Path(__file__).resolve().parents[4] / "scripts" / "local-maintenance.py"
    spec = importlib.util.spec_from_file_location("sidera_local_maintenance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _context(run_root: Path) -> dict[str, object]:
    return {
        "path": run_root / "manifest.json",
        "raw": {},
        "kind": "test",
        "run_id": "10000000-0000-0000-0000-000000000001",
        "workspace_id": "20000000-0000-0000-0000-000000000001",
        "run_root": run_root,
        "repository": run_root / "repository",
        "source_commit": "a" * 40,
        "implementation_fingerprint": "b" * 64,
        "runtime_fingerprint": "c" * 64,
    }


def _inventory(run_root: Path, source: Path) -> Path:
    path = run_root / "maintenance-inventory.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "10000000-0000-0000-0000-000000000001",
                "workspace_id": "20000000-0000-0000-0000-000000000001",
                "code_commit": "a" * 40,
                "implementation_fingerprint": "b" * 64,
                "runtime_fingerprint": "c" * 64,
                "writers": {
                    "status": "stopped",
                    "active_component_ids": [],
                    "observed_at": "2026-09-18T00:00:00+00:00",
                },
                "persistence": {
                    "status": "complete",
                    "component_ids": ["history", "ledger"],
                    "unsupported_component_ids": [],
                },
                "components": [
                    {
                        "component_id": "history",
                        "kind": "metadata",
                        "source_ref": f"file:{source}",
                        "restore_relative_path": "history.json",
                        "schema_identity": "history-v1",
                        "required": True,
                        "secret_policy": "metadata_only",
                    },
                    {
                        "component_id": "ledger",
                        "kind": "ledger",
                        "source_ref": "file:/does/not/exist",
                        "restore_relative_path": "acceptance-state.json",
                        "schema_identity": "runtime-guard-v2",
                        "required": False,
                        "secret_policy": "exclude",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _postgres_backup(tmp_path: Path) -> Path:
    module = _module()
    backup = tmp_path / "postgres-backup"
    payload = backup / "payload" / "databases"
    payload.mkdir(parents=True)
    dump = payload / "control.dump"
    dump.write_bytes(b"provider-free pg dump fixture\n")
    digest, size = module._file_digest(dump)
    manifest = {
        "schema_version": 1,
        "status": "COMPLETE",
        "restore_mode": "read_only",
        "run_id": "10000000-0000-0000-0000-000000000001",
        "workspace_id": "20000000-0000-0000-0000-000000000001",
        "source_commit": "a" * 40,
        "implementation_fingerprint": "b" * 64,
        "runtime_fingerprint": "c" * 64,
        "components": [
            {
                "component_id": "control-plane-postgres",
                "kind": "postgres",
                "schema_identity": "alembic:control-plane",
                "required": True,
                "secret_policy": "private_data",
            }
        ],
        "files": [
            {
                "relative_path": "payload/databases/control.dump",
                "component_id": "control-plane-postgres",
                "sha256": digest,
                "size": size,
            }
        ],
        "database_facts": {
            "control-plane-postgres": {
                "status": "ready",
                "identity": "source-db|tester",
                "heads": ["head-1"],
                "active_writer_sessions": "0",
                "schema_identity": "alembic:control-plane",
            }
        },
        "git_facts": {},
        "excluded": [],
        "requires_reconfiguration": True,
    }
    (backup / "backup-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return backup


def _restore_target_manifest(tmp_path: Path, *, schema_identity: str = "alembic:control-plane") -> Path:
    target_manifest = tmp_path / "target.json"
    target_manifest.write_text(
        json.dumps(
            {
                "run_id": "30000000-0000-0000-0000-000000000001",
                "workspace_id": "40000000-0000-0000-0000-000000000001",
                "run_root": str(tmp_path / "restored"),
                "restore_mode": "read_only",
                "database_targets": [
                    {
                        "component_id": "control-plane-postgres",
                        "target_ref": "postgres:ref:env:SIDERA_RESTORE_TARGET_DB",
                        "schema_identity": schema_identity,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return target_manifest


def _fake_restore_database_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    monkeypatch.setenv(
        "SIDERA_RESTORE_TARGET_DB",
        "postgresql+psycopg://tester@127.0.0.1:55432/target-db",
    )

    def fake_run(command, **kwargs):
        executable = Path(command[0]).name
        if executable == "pg_isready":
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if executable == "psql":
            query = command[command.index("--command") + 1]
            if "current_database" in query:
                stdout = "target-db|tester\n"
            elif "pg_class" in query:
                stdout = "0\n"
            elif "version_num" in query:
                stdout = "\n"
            else:
                raise AssertionError(f"unexpected query: {query}")
            return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        raise AssertionError(f"unexpected executable: {executable}")

    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(module.subprocess, "run", fake_run)


def test_backup_inspect_restore_is_read_only_and_does_not_reauthorize_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "history.json"
    source.write_text('{"message":"kept"}\n', encoding="utf-8")
    inventory = _inventory(run_root, source)
    monkeypatch.setattr(module, "_load_context", lambda _path: _context(run_root))

    backup_root = tmp_path / "backup"
    result = module.backup(
        run_root / "manifest.json",
        backup_root,
        source_inventory=inventory,
    )
    assert result["status"] == "COMPLETE"
    assert result["requires_reconfiguration"] is True
    assert result["provider_calls"] == 0
    assert module.inspect_backup(backup_root)["status"] == "COMPLETE"

    target_manifest = tmp_path / "target.json"
    target_manifest.write_text(
        json.dumps(
            {
                "run_id": "30000000-0000-0000-0000-000000000001",
                "workspace_id": "40000000-0000-0000-0000-000000000001",
                "run_root": str(tmp_path / "restored"),
                "restore_mode": "read_only",
            }
        ),
        encoding="utf-8",
    )
    restored = module.restore(backup_root, target_manifest)
    assert restored["restore_mode"] == "read_only"
    assert restored["reauthorize_copied_ledger"] is True
    assert (tmp_path / "restored" / "payload" / "history.json").read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    result_body = json.loads(
        (tmp_path / "restored" / "restore-result.json").read_text(encoding="utf-8")
    )
    assert result_body["writes_enabled"] is False
    assert result_body["provider_calls"] == 0


def test_restore_preflight_validates_target_and_refuses_database_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    backup = _postgres_backup(tmp_path)
    target_manifest = _restore_target_manifest(tmp_path)
    _fake_restore_database_probes(monkeypatch)

    ready = module.restore_preflight(backup, target_manifest)

    assert ready["status"] == "READY"
    assert ready["database_imports"] == 0
    assert not (tmp_path / "restored").exists()


def test_restore_preflight_rejects_active_source_before_target_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    backup = _postgres_backup(tmp_path)
    manifest_path = backup / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database_facts"]["control-plane-postgres"]["active_writer_sessions"] = "1"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = manifest_path.read_bytes()
    target_manifest = _restore_target_manifest(tmp_path)

    with pytest.raises(module.MaintenanceError, match="restore_source_writer_active"):
        module.restore_preflight(backup, target_manifest)
    assert manifest_path.read_bytes() == original
    assert not (tmp_path / "restored").exists()


def test_restore_preflight_rejects_schema_mapping_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    backup = _postgres_backup(tmp_path)
    target_manifest = _restore_target_manifest(tmp_path, schema_identity="alembic:wrong")
    _fake_restore_database_probes(monkeypatch)

    with pytest.raises(module.MaintenanceError, match="restore_schema_incompatible"):
        module.restore_preflight(backup, target_manifest)
    assert not (tmp_path / "restored").exists()


def test_doctor_reports_incomplete_inventory_without_claiming_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "history.json"
    source.write_text('{"message":"kept"}\n', encoding="utf-8")
    inventory = _inventory(run_root, source)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["persistence"] = {
        "status": "incomplete",
        "component_ids": ["history", "ledger"],
        "unsupported_component_ids": ["ledger"],
    }
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    context = _context(run_root)
    context["raw"] = {"maintenance_inventory_ref": str(inventory)}
    monkeypatch.setattr(module, "_load_context", lambda _path: context)

    result = module.doctor(run_root / "manifest.json")

    assert result["status"] == "INCOMPLETE"
    assert "persistence_incomplete" in result["issues"]
    assert "persistence_unsupported:ledger" in result["issues"]
    assert result["provider_calls"] == 0
    assert result["secret_values_read"] is False


def test_backup_refuses_a_run_with_an_active_maintenance_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "history.json"
    source.write_text("safe\n", encoding="utf-8")
    inventory = _inventory(run_root, source)
    context = _context(run_root)
    monkeypatch.setattr(module, "_load_context", lambda _path: context)

    with module.maintenance_lock(run_root, owner="test-holder"), pytest.raises(
        module.MaintenanceError, match="maintenance_busy"
    ):
        module.backup(
            run_root / "manifest.json",
            tmp_path / "backup",
            source_inventory=inventory,
        )


def test_backup_rejects_existing_destination_and_symlink_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "source.json"
    source.write_text("safe\n", encoding="utf-8")
    inventory = _inventory(run_root, source)
    monkeypatch.setattr(module, "_load_context", lambda _path: _context(run_root))
    destination = tmp_path / "backup"
    destination.mkdir()
    with pytest.raises(module.MaintenanceError, match="destination_must_not_exist"):
        module.backup(run_root / "manifest.json", destination, source_inventory=inventory)

    symlink = tmp_path / "link.json"
    symlink.symlink_to(source)
    symlink_inventory = _inventory(run_root, symlink)
    destination.rmdir()
    with pytest.raises(module.MaintenanceError, match="external_symlink_rejected"):
        module.backup(
            run_root / "manifest.json",
            destination,
            source_inventory=symlink_inventory,
        )


def test_backup_keeps_existing_parent_directory_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "history.json"
    source.write_text('{"message":"kept"}\n', encoding="utf-8")
    inventory = _inventory(run_root, source)
    monkeypatch.setattr(module, "_load_context", lambda _path: _context(run_root))
    parent = tmp_path / "shared-backups"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)

    module.backup(run_root / "manifest.json", parent / "backup", source_inventory=inventory)

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE((parent / "backup").stat().st_mode) == 0o700


def test_backup_failure_removes_temporary_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    empty_source = tmp_path / "empty-source"
    empty_source.mkdir()
    inventory = _inventory(run_root, empty_source)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["components"][0]["source_ref"] = f"file:{empty_source}"
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    context = _context(run_root)
    monkeypatch.setattr(module, "_load_context", lambda _path: context)
    parent = tmp_path / "backups"
    destination = parent / "failed-backup"

    with pytest.raises(module.MaintenanceError, match="required_component_empty"):
        module.backup(run_root / "manifest.json", destination, source_inventory=inventory)

    assert not destination.exists()
    assert not list(parent.glob(".failed-backup.*"))


def test_backup_sets_private_modes_on_new_root_and_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "history.json"
    source.write_text("safe\n", encoding="utf-8")
    inventory = _inventory(run_root, source)
    monkeypatch.setattr(module, "_load_context", lambda _path: _context(run_root))

    backup_root = tmp_path / "backup"
    module.backup(run_root / "manifest.json", backup_root, source_inventory=inventory)

    assert stat.S_IMODE(backup_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((backup_root / "backup-manifest.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((backup_root / "payload" / "history.json").stat().st_mode) == 0o600


def test_backup_copies_only_the_declared_nested_directory_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "state"
    nested = source / "nested" / "events.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("nested\n", encoding="utf-8")
    inventory = _inventory(run_root, source)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["components"][0].update(
        {
            "kind": "directory",
            "source_ref": f"file:{source}",
            "restore_relative_path": "state",
        }
    )
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(module, "_load_context", lambda _path: _context(run_root))

    backup_root = tmp_path / "backup"
    module.backup(run_root / "manifest.json", backup_root, source_inventory=inventory)

    assert (
        backup_root / "payload" / "state" / "nested" / "events.json"
    ).read_text(encoding="utf-8") == "nested\n"


def test_inventory_rejects_broad_path_and_secret_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    context = _context(run_root)
    monkeypatch.setattr(module, "_load_context", lambda _path: context)
    broad = {
        "component_id": "root",
        "kind": "directory",
        "source_ref": "file:/",
        "restore_relative_path": "root",
        "schema_identity": "v1",
        "required": True,
        "secret_policy": "private_data",
    }
    with pytest.raises(module.MaintenanceError, match="broad_source_path_rejected"):
        module._validate_component(broad, context)

    secret = tmp_path / ".env.local"
    secret.write_text("SECRET_SENTINEL\n", encoding="utf-8")
    inventory = _inventory(run_root, secret)
    with pytest.raises(module.MaintenanceError, match="sensitive_path_not_excluded"):
        module.backup(
            run_root / "manifest.json",
            tmp_path / "backup",
            source_inventory=inventory,
        )


def test_required_component_cannot_use_exclude_policy(
    tmp_path: Path,
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    component = {
        "component_id": "history",
        "kind": "metadata",
        "source_ref": f"file:{tmp_path / 'history.json'}",
        "restore_relative_path": "history.json",
        "schema_identity": "history-v1",
        "required": True,
        "secret_policy": "exclude",
    }

    with pytest.raises(module.MaintenanceError, match="required_component_excluded"):
        module._validate_component(component, _context(run_root))


def test_inventory_requires_complete_runtime_persistence_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "history.json"
    source.write_text("safe\n", encoding="utf-8")
    inventory = _inventory(run_root, source)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["persistence"]["status"] = "incomplete"
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    context = _context(run_root)
    monkeypatch.setattr(module, "_load_context", lambda _path: context)

    with pytest.raises(module.MaintenanceError, match="backup_inventory_incomplete"):
        module._load_inventory(context, source_inventory=inventory)


def test_inventory_rejects_an_unresolved_component_even_if_marked_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "history.json"
    source.write_text("safe\n", encoding="utf-8")
    inventory = _inventory(run_root, source)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["persistence"]["unsupported_component_ids"] = ["unregistered-plugin"]
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    context = _context(run_root)
    monkeypatch.setattr(module, "_load_context", lambda _path: context)

    with pytest.raises(module.MaintenanceError, match="backup_inventory_incomplete"):
        module._load_inventory(context, source_inventory=inventory)


def test_inventory_identity_and_restore_paths_cannot_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "history.json"
    source.write_text("safe\n", encoding="utf-8")
    inventory = _inventory(run_root, source)
    context = _context(run_root)
    monkeypatch.setattr(module, "_load_context", lambda _path: context)

    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["code_commit"] = "d" * 40
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.MaintenanceError, match="maintenance_identity_mismatch"):
        module._load_inventory(context, source_inventory=inventory)

    payload["code_commit"] = "a" * 40
    payload["components"][1]["restore_relative_path"] = "history.json"
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.MaintenanceError, match="restore_path_conflict"):
        module._load_inventory(context, source_inventory=inventory)


def test_inventory_rejects_demo_database_identity_drift(
    tmp_path: Path,
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "history.json"
    source.write_text("safe\n", encoding="utf-8")
    inventory = _inventory(run_root, source)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["source_identities"] = {
        "database_ref": "demo-control-plane",
        "knowledge_database_ref": "demo-knowledge",
    }
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    context = _context(run_root)
    context["raw"] = {
        "database_name": "demo-control-plane",
        "knowledge_database_name": "demo-knowledge",
    }

    assert module._load_inventory(context, source_inventory=inventory)[
        "source_identities"
    ] == payload["source_identities"]

    payload["source_identities"]["database_ref"] = "other-database"
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.MaintenanceError, match="maintenance_identity_mismatch"):
        module._load_inventory(context, source_inventory=inventory)


def test_database_file_hardlink_and_destination_overlap_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    context = _context(run_root)
    monkeypatch.setattr(module, "_load_context", lambda _path: context)

    database = tmp_path / "state.sqlite"
    database.write_bytes(b"sqlite placeholder")
    inventory = _inventory(run_root, database)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["components"][0]["kind"] = "sqlite"
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.MaintenanceError, match="database_source_requires_export"):
        module.backup(run_root / "manifest.json", tmp_path / "database-backup", source_inventory=inventory)

    source_dir = tmp_path / "state"
    source_dir.mkdir()
    state = source_dir / "state.json"
    state.write_text("state\n", encoding="utf-8")
    directory_inventory = _inventory(run_root, source_dir)
    with pytest.raises(module.MaintenanceError, match="destination_inside_source"):
        module.backup(run_root / "manifest.json", source_dir / "backup", source_inventory=directory_inventory)

    original = tmp_path / "original.txt"
    original.write_text("secret\n", encoding="utf-8")
    hardlink = tmp_path / "hardlink.txt"
    hardlink.hardlink_to(original)
    hardlink_inventory = _inventory(run_root, hardlink)
    with pytest.raises(module.MaintenanceError, match="hardlink_source_rejected"):
        module.backup(run_root / "manifest.json", tmp_path / "hardlink-backup", source_inventory=hardlink_inventory)


def test_git_component_never_uses_directory_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    (repository / "README.md").write_text("git source\n", encoding="utf-8")
    inventory = _inventory(run_root, tmp_path / "history.json")
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["components"][0].update(
        {
            "kind": "git",
            "source_ref": f"file:{repository}",
            "restore_relative_path": "repository",
            "schema_identity": "git-bundle-v1",
        }
    )
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    context = _context(run_root)
    monkeypatch.setattr(module, "_load_context", lambda _path: context)

    with pytest.raises(module.MaintenanceError, match="git_snapshot_failed"):
        module.backup(
            run_root / "manifest.json",
            tmp_path / "git-backup",
            source_inventory=inventory,
        )


def test_git_component_preserves_a_tracked_internal_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    repository = tmp_path / "repository"
    (repository / "private_runtime" / "mcp_servers").mkdir(parents=True)
    (repository / "private_runtime" / "mcp_servers" / "README.md").write_text(
        "internal target\n", encoding="utf-8"
    )
    (repository / "mcp_servers").symlink_to("private_runtime/mcp_servers")
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repository), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=SIDERA test",
            "-c",
            "user.email=sidera-test@example.invalid",
            "commit",
            "-m",
            "internal symlink",
        ],
        check=True,
        capture_output=True,
    )
    inventory = _inventory(run_root, repository)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["components"][0].update(
        {
            "kind": "git",
            "source_ref": f"file:{repository}",
            "restore_relative_path": "repository",
            "schema_identity": "git-bundle-v1",
        }
    )
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    context = _context(run_root)
    monkeypatch.setattr(module, "_load_context", lambda _path: context)

    result = module.backup(
        run_root / "manifest.json",
        tmp_path / "git-backup-with-internal-symlink",
        source_inventory=inventory,
    )

    assert result["status"] == "COMPLETE"
    assert result["git_facts"]["history"]["detached_restore_verified"] is True


def test_postgres_component_requires_a_resolvable_reference(
    tmp_path: Path,
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    component = {
        "component_id": "control-plane-postgres",
        "kind": "postgres",
        "source_ref": "postgres:raw-db",
        "restore_relative_path": "databases/control-plane",
        "schema_identity": "postgres-v1",
        "required": True,
        "secret_policy": "private_data",
    }

    with pytest.raises(module.MaintenanceError, match="database_reference_invalid"):
        module._validate_component(component, _context(run_root))


def test_sqlite_component_uses_online_backup_and_preserves_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    database = tmp_path / "state.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, body TEXT)")
    connection.execute("INSERT INTO events(body) VALUES ('from-wal')")
    connection.commit()
    connection.close()
    inventory = _inventory(run_root, database)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["components"][0].update(
        {
            "kind": "sqlite",
            "source_ref": f"sqlite:file:{database}",
            "schema_identity": "sqlite-events-v1",
            "restore_relative_path": "state/events.db",
        }
    )
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    context = _context(run_root)
    monkeypatch.setattr(module, "_load_context", lambda _path: context)

    result = module.backup(
        run_root / "manifest.json", tmp_path / "sqlite-backup", source_inventory=inventory
    )

    assert result["status"] == "COMPLETE"
    restored_db = tmp_path / "sqlite-backup" / "payload" / "state" / "events.db"
    restored = sqlite3.connect(restored_db)
    assert restored.execute("SELECT body FROM events").fetchone() == ("from-wal",)
    restored.close()


def test_postgres_component_uses_sanitized_dump_argv_and_private_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "history.json"
    source.write_text("safe\n", encoding="utf-8")
    inventory = _inventory(run_root, source)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["components"][0].update(
        {
            "kind": "postgres",
            "source_ref": "postgres:ref:env:SIDERA_TEST_DB",
            "restore_relative_path": "databases/control-plane.dump",
            "schema_identity": "postgres:alembic-v1",
        }
    )
    payload["components"] = payload["components"][:1]
    payload["persistence"]["component_ids"] = ["history"]
    payload["persistence"]["unsupported_component_ids"] = []
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    context = _context(run_root)
    context["raw"] = {"database_ref": "env:SIDERA_TEST_DB"}
    monkeypatch.setattr(module, "_load_context", lambda _path: context)
    monkeypatch.setenv(
        "SIDERA_TEST_DB",
        "postgresql+psycopg://tester:private-password@127.0.0.1:55432/sidera_test",
    )
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        assert "private-password" not in " ".join(command)
        assert kwargs["env"]["PGPASSWORD"] == "private-password"
        executable = Path(command[0]).name
        if executable == "pg_dump":
            output = Path(command[command.index("--file") + 1])
            output.write_bytes(b"pg dump")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if executable == "pg_isready":
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if executable == "psql":
            query = command[command.index("--command") + 1]
            if "pg_stat_activity" in query:
                stdout = "0\n"
            elif "current_database" in query:
                stdout = "sidera_test|tester\n"
            elif "version_num" in query:
                stdout = "head\n"
            else:
                raise AssertionError(f"unexpected query: {query}")
            return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if executable == "pg_restore":
            return types.SimpleNamespace(
                returncode=0,
                stdout="1; 0 TABLE public history tester\n",
                stderr="",
            )
        raise AssertionError(f"unexpected executable: {executable}")

    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.backup(
        run_root / "manifest.json", tmp_path / "postgres-backup", source_inventory=inventory
    )

    assert result["status"] == "COMPLETE"
    assert any(command[0] == "pg_dump" for command in commands)
    assert any(command[0] == "/usr/bin/pg_restore" for command in commands)


def test_restore_requires_an_isolated_loopback_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "history.json"
    source.write_text("safe\n", encoding="utf-8")
    inventory = _inventory(run_root, source)
    monkeypatch.setattr(module, "_load_context", lambda _path: _context(run_root))
    backup_root = tmp_path / "backup"
    module.backup(run_root / "manifest.json", backup_root, source_inventory=inventory)

    target_manifest = tmp_path / "target.json"
    target_manifest.write_text(
        json.dumps(
            {
                "run_id": "30000000-0000-0000-0000-000000000001",
                "workspace_id": "40000000-0000-0000-0000-000000000001",
                "run_root": str(tmp_path / "endpoint-restored"),
                "restore_mode": "read_only",
                "control_plane_url": "https://example.com",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(module.MaintenanceError, match="target_endpoint_invalid"):
        module.restore(backup_root, target_manifest)

    target_manifest.write_text(
        json.dumps(
            {
                "run_id": "10000000-0000-0000-0000-000000000001",
                "workspace_id": "40000000-0000-0000-0000-000000000001",
                "run_root": str(tmp_path / "restored"),
                "restore_mode": "read_only",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(module.MaintenanceError, match="restore_target_identity_conflict"):
        module.restore(backup_root, target_manifest)


def test_restore_requires_an_explicit_read_only_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "history.json"
    source.write_text("safe\n", encoding="utf-8")
    inventory = _inventory(run_root, source)
    monkeypatch.setattr(module, "_load_context", lambda _path: _context(run_root))
    backup_root = tmp_path / "backup"
    module.backup(run_root / "manifest.json", backup_root, source_inventory=inventory)

    target_manifest = tmp_path / "target.json"
    target_manifest.write_text(
        json.dumps(
            {
                "run_id": "30000000-0000-0000-0000-000000000001",
                "workspace_id": "40000000-0000-0000-0000-000000000001",
                "run_root": str(tmp_path / "restored"),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.MaintenanceError, match="restore_must_be_read_only"):
        module.restore(backup_root, target_manifest)


def test_restore_failure_removes_all_temporary_target_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "history.json"
    source.write_text("safe\n", encoding="utf-8")
    inventory = _inventory(run_root, source)
    monkeypatch.setattr(module, "_load_context", lambda _path: _context(run_root))
    backup_root = tmp_path / "backup"
    module.backup(run_root / "manifest.json", backup_root, source_inventory=inventory)

    target_manifest = tmp_path / "target.json"
    target_manifest.write_text(
        json.dumps(
            {
                "run_id": "30000000-0000-0000-0000-000000000001",
                "workspace_id": "40000000-0000-0000-0000-000000000001",
                "run_root": str(tmp_path / "restored"),
                "restore_mode": "read_only",
            }
        ),
        encoding="utf-8",
    )

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        raise module.MaintenanceError("injected_restore_copy_failure")

    monkeypatch.setattr(module, "_copy_verified", fail_copy)
    with pytest.raises(module.MaintenanceError, match="injected_restore_copy_failure"):
        module.restore(backup_root, target_manifest)

    assert not list(tmp_path.glob(".restored.*"))


def test_restore_refuses_a_non_empty_existing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "history.json"
    source.write_text("safe\n", encoding="utf-8")
    inventory = _inventory(run_root, source)
    monkeypatch.setattr(module, "_load_context", lambda _path: _context(run_root))
    backup_root = tmp_path / "backup"
    module.backup(run_root / "manifest.json", backup_root, source_inventory=inventory)

    target_root = tmp_path / "restored"
    target_root.mkdir()
    sentinel = target_root / "do-not-overwrite.txt"
    sentinel.write_text("original\n", encoding="utf-8")
    target_manifest = tmp_path / "target.json"
    target_manifest.write_text(
        json.dumps(
            {
                "run_id": "30000000-0000-0000-0000-000000000001",
                "workspace_id": "40000000-0000-0000-0000-000000000001",
                "run_root": str(target_root),
                "restore_mode": "read_only",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.MaintenanceError, match="target_must_be_new_or_empty"):
        module.restore(backup_root, target_manifest)
    assert sentinel.read_text(encoding="utf-8") == "original\n"
