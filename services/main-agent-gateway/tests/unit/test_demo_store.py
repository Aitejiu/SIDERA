from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_demo_key_value_store_survives_a_new_instance(tmp_path: Path) -> None:
    from sidera_main_agent_gateway.adapters.demo.store import (
        DemoKeyValueStore,
        DemoStoreAlreadyOpen,
    )

    path = tmp_path / "main-agent" / "state.json"
    first = DemoKeyValueStore(path)
    first.set("conversation", {"turns": ["turn-1"]})

    with pytest.raises(DemoStoreAlreadyOpen):
        DemoKeyValueStore(path)

    first.close()
    second = DemoKeyValueStore(path)

    assert second.get("conversation") == {"turns": ["turn-1"]}
    assert second.get("missing", "default") == "default"
    second.close()


def test_demo_key_value_store_read_only_does_not_create_or_mutate_files(
    tmp_path: Path,
) -> None:
    from sidera_main_agent_gateway.adapters.demo.store import DemoKeyValueStore

    path = tmp_path / "restored" / "main-agent" / "store.json"
    store = DemoKeyValueStore(path, read_only=True)

    assert store.get("missing", "default") == "default"
    assert not path.exists()
    assert not path.parent.exists()
    with pytest.raises(RuntimeError, match="read-only"):
        store.set("conversation", {"turns": ["must-not-write"]})
    store.close()


def test_demo_key_value_store_read_only_reads_existing_history_without_lock_files(
    tmp_path: Path,
) -> None:
    from sidera_main_agent_gateway.adapters.demo.store import DemoKeyValueStore

    path = tmp_path / "main-agent" / "store.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"conversation": {"turns": ["turn-1"]}}), encoding="utf-8"
    )
    before = sorted(item.name for item in path.parent.iterdir())

    store = DemoKeyValueStore(path, read_only=True)
    assert store.get("conversation") == {"turns": ["turn-1"]}
    store.close()

    assert sorted(item.name for item in path.parent.iterdir()) == before


def test_demo_manifest_loader_rejects_non_local_or_secret_fields(tmp_path: Path) -> None:
    from sidera_main_agent_gateway.adapters.demo.store import load_demo_manifest

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        '{"schema_version": 1, "run_id": "00000000-0000-0000-0000-000000000001", '
        '"workspace_id": "00000000-0000-0000-0000-000000000002", '
        '"endpoints": {"control_plane": "http://127.0.0.1:8000", '
        '"knowledge": "http://127.0.0.1:8001", "main_agent": "http://127.0.0.1:8002", '
        '"web": "http://127.0.0.1:8003"}, "module_ids": {}, "worker_ids": {}, '
        '"planning_profile_id": "00000000-0000-0000-0000-000000000003", '
        '"planning_profile_version": 1, "run_root": "/tmp/demo", '
        '"repository": "/tmp/demo/repository", "database_name": "demo", '
        '"code_commit": "a", "credentials": "secret"}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest"):
        load_demo_manifest(manifest_path)


def test_demo_manifest_loader_accepts_the_current_schema_two_manifest(
    tmp_path: Path,
) -> None:
    from sidera_main_agent_gateway.adapters.demo.store import (
        _PROJECT_ROOT,
        load_demo_manifest,
    )

    run_id = "00000000-0000-0000-0000-000000000101"
    run_root = _PROJECT_ROOT / ".sidera" / "interview-demo" / run_id
    repository = tmp_path / "repository"
    repository.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "mode": "interview-demo",
                "run_id": run_id,
                "workspace_id": "00000000-0000-0000-0000-000000000102",
                "scenario": "mainline",
                "run_root": str(run_root),
                "repository": str(repository),
                "base_commit": "a" * 40,
                "code_commit": "b" * 40,
                "implementation_fingerprint": "c" * 64,
                "fingerprint_version": 2,
                "database_name": f"sidera_interview_test_{run_id.replace('-', '')}",
                "knowledge_database_name": (
                    f"sidera_interview_knowledge_{run_id.replace('-', '')}"
                ),
                "pg_host": "127.0.0.1",
                "pg_port": 55432,
                "pg_user": "sidera",
                "endpoints": {
                    "control_plane": "http://127.0.0.1:18000",
                    "knowledge": "http://127.0.0.1:18001",
                    "main_agent": "http://127.0.0.1:18002",
                    "web": "http://127.0.0.1:18003",
                },
                "module_ids": {
                    key: f"00000000-0000-0000-0000-{index:012d}"
                    for index, key in enumerate(
                        (
                            "worker_primary",
                            "worker_secondary",
                            "workspace",
                            "planner",
                            "collaboration",
                            "verification",
                            "notification",
                        ),
                        start=1,
                    )
                },
                "worker_ids": {
                    key: f"00000000-0000-0000-0001-{index:012d}"
                    for index, key in enumerate(
                        (
                            "validation",
                            "report",
                            "review",
                            "integration",
                            "replacement",
                            "verification",
                        ),
                        start=1,
                    )
                },
                "planning_profile_id": "00000000-0000-0000-0002-000000000101",
                "planning_profile_version": 1,
                "execution_deadline_seconds": 3600,
                "maintenance_inventory_ref": str(
                    run_root / "maintenance-inventory.json"
                ),
                "restore_mode": None,
            }
        ),
        encoding="utf-8",
    )

    identity = load_demo_manifest(manifest_path)

    assert identity.schema_version == 2
    assert identity.implementation_fingerprint == "c" * 64
