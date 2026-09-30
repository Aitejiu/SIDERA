from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import Engine

from sidera_control_plane.demo.cli import prepare_demo_run
from sidera_control_plane.demo.contracts import DemoManifest
from sidera_control_plane.demo.database import database_url_for
from sidera_control_plane.demo.sample_repository import create_sample_repository
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    ContextPackageModel,
    ExecutionInputModel,
    IntegrationCandidateModel,
    RealResultRunBudgetModel,
    ResultOperationAttemptModel,
    ResultVerificationResultModel,
    TaskModel,
    WorkerItemModel,
)
from tests.integration.local_maintenance_support import (
    JsonFileKV,
    maintenance_baseline,
)

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[4]
CONTROL_PLANE_ROOT = PROJECT_ROOT / "services" / "control-plane"
BASIC_STACK = PROJECT_ROOT / "scripts" / "r6-basic-stack.sh"
DATABASE_NAME = re.compile(r"^sidera_interview_(?:test_|knowledge_)[0-9a-f]{32}$")
RESTORE_DATABASE_NAME = re.compile(r"^sidera_restore_(?:control|knowledge)_[0-9a-f]{32}$")


def test_interview_demo_prepare_materializes_an_explicit_inventory() -> None:
    """A new Demo run records its own known storage and its explicit gaps."""

    if not os.environ.get("SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL"):
        pytest.fail("SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL is required")

    manifest: DemoManifest | None = None
    try:
        manifest = prepare_demo_run("mainline")
        manifest_path = manifest.run_root / "manifest.json"
        assert manifest.maintenance_inventory_ref is not None
        inventory_path = Path(manifest.maintenance_inventory_ref)

        assert manifest_path.is_file()
        assert inventory_path.is_absolute()
        assert inventory_path == manifest.run_root / "maintenance-inventory.json"
        assert inventory_path.is_file()
        assert not inventory_path.is_symlink()

        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        assert raw_manifest["maintenance_inventory_ref"] == str(inventory_path)
        assert inventory["run_id"] == str(manifest.run_id)
        assert inventory["workspace_id"] == str(manifest.workspace_id)
        assert inventory["writers"]["status"] == "unknown"
        assert inventory["writers"]["active_component_ids"] == []

        component_ids = {item["component_id"] for item in inventory["components"]}
        assert {
            "state",
            "workspaces",
            "artifacts",
            "results",
            "planning",
            "candidate-spool",
            "knowledge-spool",
            "main-agent-conversations",
            "sample-repository",
            "demo-tokens",
            "control-plane-postgres",
            "knowledge-postgres",
        } == component_ids
        ordered_component_ids = [
            item["component_id"] for item in inventory["components"]
        ]
        assert inventory["persistence"] == {
            "status": "incomplete",
            "component_ids": ordered_component_ids,
            "unsupported_component_ids": [
                "control-plane-postgres",
                "knowledge-postgres",
            ],
        }
        assert inventory["source_identities"] == {
            "database_ref": manifest.database_name,
            "knowledge_database_ref": manifest.knowledge_database_name,
        }
    finally:
        if manifest is not None:
            _drop_demo_databases(manifest)
            shutil.rmtree(manifest.run_root, ignore_errors=True)
            shutil.rmtree(manifest.repository, ignore_errors=True)


def test_basic_stack_inventory_writer_and_doctor_receipt(tmp_path: Path) -> None:
    repository, base_commit = create_sample_repository(tmp_path)
    manifest_path = tmp_path / "basic-manifest.json"
    run_root = tmp_path / "basic-run"
    runtime_root = tmp_path / "runtime"
    capture_root = tmp_path / "capture"
    stub_path = tmp_path / "basic-stub.sh"
    stub_path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "component=${1:?component}\n"
        "capture=${SIDERA_R6_BASIC_TEST_CAPTURE:?capture}\n"
        "mkdir -p \"$capture\"\n"
        "env | sort > \"$capture/$component.env\"\n"
        "printf '{\"component\":\"%s\",\"status\":\"ready\",\"diagnostic\":\"test\"}\\n' \"$component\"\n"
        "trap 'exit 0' TERM INT\n"
        "while :; do sleep 1; done\n",
        encoding="utf-8",
    )
    stub_path.chmod(0o700)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                    "run_id": "10000000-0000-0000-0000-000000000301",
                    "workspace_id": "10000000-0000-0000-0000-000000000302",
                    "local_gate_record": str(tmp_path / "local-gate.json"),
                    "run_root": str(run_root),
                    "repository": str(repository),
                    "base_commit": base_commit,
                "runtime_fingerprint": "b" * 64,
                "database_ref": "env:SIDERA_TEST_DB",
                "knowledge_database_ref": "env:SIDERA_TEST_KNOWLEDGE_DB",
                "control_plane_url": "http://127.0.0.1:18301",
                "knowledge_url": "http://127.0.0.1:18302",
                    "main_agent_gateway_url": "http://127.0.0.1:18303",
                    "web_url": "http://127.0.0.1:18304",
                    "profiles": [
                        {
                            "role": role,
                            "runtime": "fake-runtime",
                            "provider": "fake-provider",
                            "model_id": f"fake-{role}",
                            "endpoint_host": "127.0.0.1:18310",
                            "credential_ref": "none",
                            "config_ref": f"module:test/{role}",
                            "config_version": 1,
                        }
                        for role in (
                            "main_agent",
                            "planner",
                            "worker",
                            "second_worker",
                        )
                    ],
                    "scenarios": {
                        "profile": {
                            "main_agent_turns": 0,
                            "planner_runs": 0,
                            "worker_sessions": 0,
                            "adapter_prompts": 0,
                            "max_seconds": 60,
                        }
                    },
                }
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "SIDERA_R6_BASIC_RUNTIME_DIR": str(runtime_root),
        "SIDERA_R6_BASIC_TEST_MODE": "1",
        "SIDERA_R6_BASIC_STUB_COMMAND": str(stub_path),
        "SIDERA_R6_BASIC_TEST_CAPTURE": str(capture_root),
        "SIDERA_TEST_DB": "postgresql+psycopg://aitejiu@127.0.0.1:55432/sidera_test_r6_20260918",
        "SIDERA_TEST_KNOWLEDGE_DB": "postgresql+psycopg://aitejiu@127.0.0.1:55432/sidera_test_knowledge_r6_20260918",
    }
    try:
        _run_basic_stack("start", manifest_path, environment)

        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        inventory_path = Path(raw_manifest["maintenance_inventory_ref"])
        assert inventory_path.is_absolute()
        assert inventory_path == run_root / "maintenance-inventory.json"
        assert inventory_path.is_file()
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        assert inventory["run_id"] == raw_manifest["run_id"]
        assert inventory["workspace_id"] == raw_manifest["workspace_id"]
        assert inventory["persistence"] == {
            "status": "incomplete",
            "component_ids": [
                "control-plane-postgres",
                "knowledge-postgres",
            ],
            "unsupported_component_ids": [
                "control-plane-postgres",
                "knowledge-postgres",
            ],
        }
        assert inventory["writers"]["status"] == "running"
        assert set(inventory["writers"]["active_component_ids"]) == {
            "knowledge-service",
            "control-plane",
            "akashic-acceptance",
            "planning-worker",
            "context-package-worker",
            "knowledge-candidate-worker",
            "activation-worker",
            "scheduler-worker",
            "gateway-worker",
            "plan-transition-worker",
            "handoff-worker",
            "collaboration-worker",
            "notification-worker",
            "result-worker",
            "main-agent-gateway",
            "web",
        }
    finally:
        _run_basic_stack("stop", manifest_path, environment, check=False)

    stopped = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert stopped["writers"]["status"] == "stopped"
    assert stopped["writers"]["active_component_ids"] == []
    inventory_bytes = inventory_path.read_bytes()

    doctor = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
            "doctor",
            "--manifest",
            str(manifest_path),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert doctor.returncode == 2
    report = json.loads(doctor.stdout)
    assert report["status"] == "INCOMPLETE"
    assert "persistence_incomplete" in report["issues"]
    assert "persistence_unsupported:control-plane-postgres" in report["issues"]
    assert "persistence_unsupported:knowledge-postgres" in report["issues"]
    assert "writers_not_stopped" not in report["issues"]
    assert report["writer_facts"]["stopped_receipt"]["verified_exit"] is True
    assert set(report["database_facts"]) == {
        "control-plane-postgres",
        "knowledge-postgres",
    }
    assert all(
        facts["active_writer_sessions"] == "0"
        for facts in report["database_facts"].values()
    )
    assert report["provider_calls"] == 0
    assert report["secret_values_read"] is False
    assert inventory_path.read_bytes() == inventory_bytes
    assert not (run_root / ".sidera-maintenance.lock").exists()


def test_maintenance_baseline_fixture_reads_back_real_storage_facts(
    tmp_path: Path,
) -> None:
    with maintenance_baseline(tmp_path) as baseline:
        payload = json.loads(baseline.baseline_path.read_text(encoding="utf-8"))

        assert payload["run_id"] == str(baseline.manifest.run_id)
        assert payload["workspace_id"] == str(baseline.manifest.workspace_id)
        assert payload["provider_calls"] == 0
        assert payload["secret_values_read"] is False

        control = payload["control_plane"]
        assert control["unknown_status"] == "unknown"
        assert control["input_status"] == "blocked"
        assert control["accepted_candidate_id"]
        assert control["accepted_tree"]
        assert control["artifact_hash"]
        assert control["context_package_hash"]
        assert control["operation_ledger"]["count"] >= 1
        assert control["operation_ledger"]["deadline"]

        with baseline.control_database.session() as session:
            root_task = session.get(TaskModel, UUID(control["root_task_id"]))
            subtask = session.get(TaskModel, UUID(control["subtask_id"]))
            worker_item = session.get(WorkerItemModel, UUID(control["worker_item_id"]))
            accepted_candidate = session.get(
                IntegrationCandidateModel, UUID(control["accepted_candidate_id"])
            )
            artifact = session.get(ArtifactModel, UUID(control["artifact_id"]))
            context_package = session.get(
                ContextPackageModel, UUID(control["context_package_id"])
            )
            execution_input = session.get(
                ExecutionInputModel, UUID(control["input_id"])
            )
            unknown_verification = session.get(
                ResultVerificationResultModel,
                UUID(control["unknown_verification_id"]),
            )
            budget = session.get(
                RealResultRunBudgetModel,
                UUID(control["operation_ledger"]["budget_run_id"]),
            )
            operation_count = session.scalar(
                select(func.count())
                .select_from(ResultOperationAttemptModel)
                .where(
                    ResultOperationAttemptModel.workspace_id
                    == baseline.manifest.workspace_id,
                    ResultOperationAttemptModel.root_task_id
                    == UUID(control["root_task_id"]),
                )
            )

        assert root_task is not None and root_task.status == "succeeded"
        assert subtask is not None
        assert worker_item is not None
        assert accepted_candidate is not None
        assert accepted_candidate.content_hash == control["accepted_candidate_hash"]
        assert artifact is not None and artifact.content_hash == control["artifact_hash"]
        assert (
            context_package is not None
            and context_package.package_hash == control["context_package_hash"]
        )
        assert execution_input is not None and execution_input.status == "blocked"
        assert unknown_verification is not None and unknown_verification.status == "unknown"
        assert budget is not None
        assert budget.expires_at.isoformat() == control["operation_ledger"]["deadline"]
        assert operation_count == control["operation_ledger"]["count"]

        knowledge = payload["knowledge"]
        assert knowledge["candidate_status"] == "accepted"
        assert knowledge["candidate_hash"]
        assert knowledge["revision_hash"]

        from sidera_knowledge.persistence.models import (
            KnowledgeCandidateModel,
            KnowledgeRevisionModel,
        )

        with baseline.knowledge_database.session() as session:
            knowledge_candidate = session.get(
                KnowledgeCandidateModel, UUID(knowledge["candidate_id"])
            )
            knowledge_revision = session.get(
                KnowledgeRevisionModel, UUID(knowledge["revision_id"])
            )
        assert knowledge_candidate is not None
        assert knowledge_candidate.status == "accepted"
        assert knowledge_candidate.candidate_hash == knowledge["candidate_hash"]
        assert knowledge_revision is not None
        assert knowledge_revision.content_hash == knowledge["revision_hash"]

        akashic = payload["akashic"]
        assert Path(akashic["session_wal"]).is_file()
        assert akashic["message_order"] == sorted(akashic["message_order"])
        assert len(akashic["message_ids"]) == 2
        assert akashic["memory_id"]
        assert akashic["memory_hash"]
        session_meta = baseline.session_store.get_session_meta(akashic["thread_id"])
        turn = baseline.session_store.read_turn(akashic["turn_id"])
        messages = baseline.session_store.fetch_session_messages(akashic["thread_id"])
        memory_rows = baseline.memory_store.list_by_type("preference")
        assert session_meta is not None
        assert turn is not None
        assert [str(row["id"]) for row in messages] == akashic["message_ids"]
        assert [int(row["seq"]) for row in messages] == akashic["message_order"]
        assert any(str(row["id"]) == akashic["memory_id"] for row in memory_rows)

        bridge = payload["bridge"]
        assert bridge["first_disposition"] == "new"
        assert bridge["replay_disposition"] == "recovered"
        assert bridge["cursor"] == 7
        assert len(bridge["unresolved_ids"]) == 1
        from sidera_task_bridge.state import BridgeStateStore

        bridge_snapshot = asyncio.run(
            BridgeStateStore(JsonFileKV(Path(bridge["state_path"]))).snapshot()
        )
        assert bridge_snapshot.notification_cursor == bridge["cursor"]
        assert set(bridge_snapshot.unresolved_commands) == set(bridge["unresolved_ids"])
        assert sorted(bridge_snapshot.source_routes) == bridge["routes"]
        assert sorted(bridge_snapshot.resource_sources) == bridge["resource_bindings"]

        assert "baseline-notes.md" in payload["repository"]["status"]
        assert payload["repository"]["uncommitted_file_hash"]

        assert baseline.baseline_path.stat().st_mode & 0o077 == 0
        assert "sk-" not in baseline.baseline_path.read_text(encoding="utf-8")


def test_maintenance_backup_exports_real_postgres_databases_and_records_heads(
    tmp_path: Path,
) -> None:
    with maintenance_baseline(tmp_path) as baseline:
        inventory_path = _write_postgres_export_inventory(baseline)
        destination = tmp_path / "postgres-backup"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PGPASSWORD", "PGPASSFILE"}
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                "backup",
                "--manifest",
                str(baseline.manifest.run_root / "manifest.json"),
                "--source-inventory",
                str(inventory_path),
                "--destination",
                str(destination),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert completed.returncode == 0, (
            f"backup failed ({completed.returncode})\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )

        summary = json.loads(completed.stdout)
        assert summary["status"] == "COMPLETE"
        backup_manifest = json.loads(
            (destination / "backup-manifest.json").read_text(encoding="utf-8")
        )
        assert {
            entry["component_id"] for entry in backup_manifest["files"]
        } == {"control-plane-postgres", "knowledge-postgres"}
        assert {
            entry["relative_path"] for entry in backup_manifest["files"]
        } == {
            "payload/databases/control-plane.dump",
            "payload/databases/knowledge.dump",
        }
        assert set(backup_manifest["database_facts"]) == {
            "control-plane-postgres",
            "knowledge-postgres",
        }
        for fact in backup_manifest["database_facts"].values():
            assert fact["heads"]
            assert fact["active_writer_sessions"] == "0"
            assert fact["restore_list_entries"] > 0
            assert fact["dump_format"] == "custom"

        for entry in backup_manifest["files"]:
            dump_path = destination / entry["relative_path"]
            listed = subprocess.run(
                ["pg_restore", "--list", str(dump_path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            assert listed.returncode == 0
            assert listed.stdout.strip()


def test_maintenance_backup_rejects_active_writer_without_publishing(
    tmp_path: Path,
) -> None:
    with maintenance_baseline(tmp_path) as baseline:
        inventory_path = _write_postgres_export_inventory(baseline)
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        inventory["writers"] = {
            "status": "running",
            "active_component_ids": ["control-plane-postgres"],
            "observed_at": "2026-09-19T04:00:00+00:00",
        }
        inventory_path.write_text(
            json.dumps(inventory, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        destination = tmp_path / "rejected-backup"
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                "backup",
                "--manifest",
                str(baseline.manifest.run_root / "manifest.json"),
                "--source-inventory",
                str(inventory_path),
                "--destination",
                str(destination),
            ],
            cwd=PROJECT_ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert completed.returncode == 2
        assert json.loads(completed.stdout)["error_code"] == "writers_not_stopped"
        assert not destination.exists()
        assert not list(tmp_path.glob(".rejected-backup.*"))


def test_maintenance_backup_exports_akashic_bridge_and_attachment_facts(
    tmp_path: Path,
) -> None:
    with maintenance_baseline(tmp_path) as baseline:
        inventory_path = _write_local_storage_export_inventory(baseline)
        destination = tmp_path / "local-storage-backup"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PGPASSWORD", "PGPASSFILE"}
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                "backup",
                "--manifest",
                str(baseline.manifest.run_root / "manifest.json"),
                "--source-inventory",
                str(inventory_path),
                "--destination",
                str(destination),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert completed.returncode == 0, (
            f"backup failed ({completed.returncode})\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )

        backup_manifest = json.loads(
            (destination / "backup-manifest.json").read_text(encoding="utf-8")
        )
        component_ids = {entry["component_id"] for entry in backup_manifest["files"]}
        assert {
            "control-plane-postgres",
            "knowledge-postgres",
            "akashic-sessions",
            "akashic-memory2",
            "akashic-bridge",
            "akashic-attachments",
            "artifact-content",
            "main-agent-conversations",
        } == component_ids

        session_copy = destination / "payload" / "akashic" / "sessions.db"
        memory_copy = destination / "payload" / "akashic" / "memory2.db"
        with sqlite3.connect(session_copy) as connection:
            assert connection.execute("SELECT count(*) FROM messages").fetchone() == (2,)
        with sqlite3.connect(memory_copy) as connection:
            assert connection.execute("SELECT count(*) FROM memory_items").fetchone() == (1,)

        from sidera_task_bridge.state import BridgeStateStore

        bridge_snapshot = asyncio.run(
            BridgeStateStore(
                JsonFileKV(destination / "payload" / "akashic" / "plugin-data.json")
            ).snapshot()
        )
        assert bridge_snapshot.notification_cursor == 7
        assert len(bridge_snapshot.unresolved_commands) == 1
        assert sorted(bridge_snapshot.source_routes) == ["telegram:fixture-chat"]
        assert sorted(bridge_snapshot.resource_sources) == [
            f"planning_request:{baseline.payload['control_plane']['planning_request_id']}"
        ]

        attachment_copy = (
            destination / "payload" / "akashic" / "attachments" / "baseline" / "spec.md"
        )
        assert attachment_copy.read_bytes() == Path(
            baseline.payload["akashic"]["attachment_path"]
        ).read_bytes()
        attachment_entry = next(
            entry
            for entry in backup_manifest["files"]
            if entry["component_id"] == "akashic-attachments"
        )
        assert attachment_entry["sha256"] == baseline.payload["akashic"]["attachment_hash"]
        main_agent_entry = next(
            entry
            for entry in backup_manifest["files"]
            if entry["component_id"] == "main-agent-conversations"
        )
        assert main_agent_entry["sha256"] == baseline.payload["main_agent"]["store_hash"]
        control = baseline.payload["control_plane"]
        artifact_relative = Path(control["artifact_content_path"]).relative_to(
            control["artifact_storage_root"]
        )
        artifact_copy = destination / "payload" / "artifacts" / artifact_relative
        assert artifact_copy.is_file()
        artifact_entry = next(
            entry
            for entry in backup_manifest["files"]
            if entry["component_id"] == "artifact-content"
            and entry["relative_path"].endswith(
                baseline.payload["control_plane"]["artifact_content_hash"]
            )
        )
        assert artifact_entry["sha256"] == baseline.payload["control_plane"][
            "artifact_content_hash"
        ]


def test_maintenance_backup_creates_detached_git_bundle_and_worktree_snapshot(
    tmp_path: Path,
) -> None:
    with maintenance_baseline(tmp_path) as baseline:
        inventory_path = _write_git_export_inventory(baseline)
        destination = tmp_path / "git-backup"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PGPASSWORD", "PGPASSFILE"}
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                "backup",
                "--manifest",
                str(baseline.manifest.run_root / "manifest.json"),
                "--source-inventory",
                str(inventory_path),
                "--destination",
                str(destination),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert completed.returncode == 0, (
            f"backup failed ({completed.returncode})\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )

        backup_manifest = json.loads(
            (destination / "backup-manifest.json").read_text(encoding="utf-8")
        )
        git_fact = backup_manifest["git_facts"]["sample-repository"]
        assert git_fact["detached_restore_verified"] is True
        assert git_fact["source_common_dir_external"] is False
        assert git_fact["staged_paths"] == ["staged-maintenance-notes.md"]
        assert git_fact["untracked_paths"] == ["baseline-notes.md"]
        assert git_fact["unstaged_paths"]
        assert git_fact["head"] == baseline.payload["repository"]["head"]
        assert git_fact["tree"] == baseline.payload["repository"]["tree"]

        bundle = destination / "payload" / "repository" / "repository.bundle"
        state = destination / "payload" / "repository" / "git-state.json"
        assert bundle.is_file()
        assert state.is_file()
        assert str(baseline.manifest.repository) not in state.read_text(encoding="utf-8")

        detached = tmp_path / "detached-repository"
        cloned = subprocess.run(
            ["git", "clone", "--no-local", str(bundle), str(detached)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert cloned.returncode == 0, cloned.stderr
        head = subprocess.run(
            ["git", "-C", str(detached), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert head.stdout.strip() == git_fact["head"]
        assert (destination / "payload" / "repository" / "worktree" / "baseline-notes.md").is_file()
        assert (
            destination
            / "payload"
            / "repository"
            / "worktree"
            / "staged-maintenance-notes.md"
        ).is_file()


def test_maintenance_backup_manifest_is_complete_and_excludes_secret_sentinel(
    tmp_path: Path,
) -> None:
    with maintenance_baseline(tmp_path) as baseline:
        inventory_path, sentinel = _write_complete_backup_inventory(baseline)
        destination = tmp_path / "complete-backup"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PGPASSWORD", "PGPASSFILE"}
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                "backup",
                "--manifest",
                str(baseline.manifest.run_root / "manifest.json"),
                "--source-inventory",
                str(inventory_path),
                "--destination",
                str(destination),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        assert completed.returncode == 0, (
            f"backup failed ({completed.returncode})\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )

        manifest_path = destination / "backup-manifest.json"
        backup_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        component_ids = {
            component["component_id"] for component in backup_manifest["components"]
        }
        file_component_ids = {
            entry["component_id"] for entry in backup_manifest["files"]
        }
        excluded_ids = {
            entry["component_id"] for entry in backup_manifest["excluded"]
        }
        assert component_ids >= {
            "control-plane-postgres",
            "knowledge-postgres",
            "akashic-sessions",
            "akashic-memory2",
            "akashic-bridge",
            "akashic-attachments",
            "artifact-content",
            "sample-repository",
        }
        assert {"auth-secrets", "model-registry-secrets", "keychain-secrets"} == excluded_ids
        assert excluded_ids.isdisjoint(file_component_ids)
        assert all(
            component["schema_identity"]
            and isinstance(component["required"], bool)
            and component["secret_policy"] in {"private_data", "metadata_only", "exclude"}
            for component in backup_manifest["components"]
        )
        payload_paths = {
            str(path.relative_to(destination))
            for path in (destination / "payload").rglob("*")
            if path.is_file()
        }
        assert payload_paths == {
            entry["relative_path"] for entry in backup_manifest["files"]
        }
        for path in destination.rglob("*"):
            if path.is_file():
                assert sentinel.encode("utf-8") not in path.read_bytes()

        tampered = tmp_path / "duplicate-key-backup"
        shutil.copytree(destination, tampered)
        (tampered / "backup-manifest.json").write_text(
            '{"status":"COMPLETE","status":"COMPLETE"}\n', encoding="utf-8"
        )
        inspected = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                "inspect-backup",
                "--backup",
                str(tampered),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert inspected.returncode == 2
        assert json.loads(inspected.stdout)["error_code"] == "backup_manifest_invalid"


def test_restore_preflight_checks_real_empty_databases_before_import(
    tmp_path: Path,
) -> None:
    with maintenance_baseline(tmp_path) as baseline:
        inventory_path, _ = _write_complete_backup_inventory(baseline)
        backup = tmp_path / "complete-backup"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PGPASSWORD", "PGPASSFILE"}
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                "backup",
                "--manifest",
                str(baseline.manifest.run_root / "manifest.json"),
                "--source-inventory",
                str(inventory_path),
                "--destination",
                str(backup),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        assert completed.returncode == 0, completed.stderr

        target_manifest, database_names = _create_restore_target_manifest(
            baseline, tmp_path
        )
        try:
            preflight = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                    "restore-preflight",
                    "--backup",
                    str(backup),
                    "--target-manifest",
                    str(target_manifest),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            assert preflight.returncode == 0, (
                f"preflight failed ({preflight.returncode})\n"
                f"stdout:\n{preflight.stdout}\n"
                f"stderr:\n{preflight.stderr}"
            )
            preflight_body = json.loads(preflight.stdout)
            assert preflight_body["status"] == "READY"
            assert preflight_body["database_imports"] == 0
            assert not (tmp_path / "restore-target-root").exists()

            restore = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                    "restore",
                    "--backup",
                    str(backup),
                    "--target-manifest",
                    str(target_manifest),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            assert restore.returncode == 0, restore.stdout
            restore_body = json.loads(restore.stdout)
            assert restore_body["status"] == "COMPLETE"
            assert restore_body["database_imports"] == 2
            assert (tmp_path / "restore-target-root").is_dir()

            dump_entry = next(
                entry
                for entry in json.loads(
                    (backup / "backup-manifest.json").read_text(encoding="utf-8")
                )["files"]
                if entry["component_id"] == "control-plane-postgres"
            )
            dump_path = backup / dump_entry["relative_path"]
            original_backup_manifest = (backup / "backup-manifest.json").read_bytes()
            dump_bytes = dump_path.read_bytes()
            dump_path.write_bytes(dump_bytes[:-1] + bytes([dump_bytes[-1] ^ 1]))
            corrupted = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                    "restore-preflight",
                    "--backup",
                    str(backup),
                    "--target-manifest",
                    str(target_manifest),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            assert corrupted.returncode == 2
            assert json.loads(corrupted.stdout)["error_code"] == "backup_hash_mismatch"
            assert (backup / "backup-manifest.json").read_bytes() == original_backup_manifest
            assert (tmp_path / "restore-target-root" / "restore-result.json").is_file()
        finally:
            _drop_restore_databases(database_names)


def test_maintenance_cli_round_trip_and_rejects_corrupt_backup_copy(
    tmp_path: Path,
) -> None:
    """Run the public CLI and reject a damaged copy before any import."""

    with maintenance_baseline(tmp_path) as baseline:
        inventory_path, _ = _write_complete_backup_inventory(baseline)
        source_manifest = tmp_path / "maintenance-source-manifest.json"
        raw_manifest = json.loads(
            (baseline.manifest.run_root / "manifest.json").read_text(encoding="utf-8")
        )
        raw_manifest["maintenance_inventory_ref"] = str(inventory_path)
        source_manifest.write_text(
            json.dumps(raw_manifest, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PGPASSWORD", "PGPASSFILE"}
        }

        def run_cli(*arguments: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                    *arguments,
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )

        backup = tmp_path / "cli-backup"
        doctor = run_cli("doctor", "--manifest", str(source_manifest))
        assert doctor.returncode == 0, doctor.stdout + doctor.stderr
        assert json.loads(doctor.stdout)["status"] == "PASS"

        backed_up = run_cli(
            "backup",
            "--manifest",
            str(source_manifest),
            "--source-inventory",
            str(inventory_path),
            "--destination",
            str(backup),
        )
        assert backed_up.returncode == 0, backed_up.stdout + backed_up.stderr
        assert json.loads(backed_up.stdout)["status"] == "COMPLETE"

        inspected = run_cli("inspect-backup", "--backup", str(backup))
        assert inspected.returncode == 0, inspected.stdout + inspected.stderr
        assert json.loads(inspected.stdout)["status"] == "COMPLETE"

        target_manifest, database_names = _create_restore_target_manifest(
            baseline, tmp_path
        )
        try:
            preflight = run_cli(
                "restore-preflight",
                "--backup",
                str(backup),
                "--target-manifest",
                str(target_manifest),
            )
            assert preflight.returncode == 0, preflight.stdout + preflight.stderr
            assert json.loads(preflight.stdout)["status"] == "READY"

            restored = run_cli(
                "restore",
                "--backup",
                str(backup),
                "--target-manifest",
                str(target_manifest),
            )
            assert restored.returncode == 0, restored.stdout + restored.stderr
            assert json.loads(restored.stdout)["status"] == "COMPLETE"
            restore_result = tmp_path / "restore-target-root" / "restore-result.json"
            assert restore_result.is_file()
            original_result = restore_result.read_bytes()

            tampered = tmp_path / "tampered-cli-backup"
            shutil.copytree(backup, tampered)
            original_manifest = (backup / "backup-manifest.json").read_bytes()
            original_dump = (
                backup / "payload" / "databases" / "control-plane.dump"
            ).read_bytes()
            tampered_dump = tampered / "payload" / "databases" / "control-plane.dump"
            tampered_bytes = tampered_dump.read_bytes()
            tampered_dump.write_bytes(tampered_bytes[:-1] + bytes([tampered_bytes[-1] ^ 1]))

            rejected = run_cli(
                "restore-preflight",
                "--backup",
                str(tampered),
                "--target-manifest",
                str(target_manifest),
            )
            assert rejected.returncode == 2
            assert json.loads(rejected.stdout)["error_code"] == "backup_hash_mismatch"
            assert (backup / "backup-manifest.json").read_bytes() == original_manifest
            assert (
                backup / "payload" / "databases" / "control-plane.dump"
            ).read_bytes() == original_dump
            assert restore_result.read_bytes() == original_result
        finally:
            _drop_restore_databases(database_names)


def test_restore_preflight_rejects_a_real_non_empty_target_database(
    tmp_path: Path,
) -> None:
    with maintenance_baseline(tmp_path) as baseline:
        inventory_path, _ = _write_complete_backup_inventory(baseline)
        backup = tmp_path / "complete-backup"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PGPASSWORD", "PGPASSFILE"}
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                "backup",
                "--manifest",
                str(baseline.manifest.run_root / "manifest.json"),
                "--source-inventory",
                str(inventory_path),
                "--destination",
                str(backup),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        assert completed.returncode == 0, completed.stderr
        target_manifest, database_names = _create_restore_target_manifest(
            baseline, tmp_path
        )
        try:
            target_url = _restore_database_url(baseline.manifest, database_names["control-plane-postgres"])
            engine = create_engine(target_url)
            try:
                with engine.begin() as connection:
                    connection.exec_driver_sql("CREATE TABLE non_empty_target (id integer)")
            finally:
                engine.dispose()
            preflight = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                    "restore-preflight",
                    "--backup",
                    str(backup),
                    "--target-manifest",
                    str(target_manifest),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            assert preflight.returncode == 2
            assert json.loads(preflight.stdout)["error_code"] == "restore_target_not_empty"
            assert not (tmp_path / "restore-target-root").exists()
        finally:
            _drop_restore_databases(database_names)


def test_restore_imports_real_postgres_and_local_state_from_complete_backup(
    tmp_path: Path,
) -> None:
    with maintenance_baseline(tmp_path) as baseline:
        inventory_path, _ = _write_complete_backup_inventory(baseline)
        backup = tmp_path / "complete-backup"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PGPASSWORD", "PGPASSFILE"}
        }
        backed_up = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                "backup",
                "--manifest",
                str(baseline.manifest.run_root / "manifest.json"),
                "--source-inventory",
                str(inventory_path),
                "--destination",
                str(backup),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        assert backed_up.returncode == 0, backed_up.stderr
        target_manifest, database_names = _create_restore_target_manifest(
            baseline, tmp_path
        )
        try:
            restored = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
                    "restore",
                    "--backup",
                    str(backup),
                    "--target-manifest",
                    str(target_manifest),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            assert restored.returncode == 0, (
                f"restore failed ({restored.returncode})\n"
                f"stdout:\n{restored.stdout}\n"
                f"stderr:\n{restored.stderr}"
            )
            result = json.loads(restored.stdout)
            assert result["status"] == "COMPLETE"
            assert result["database_imports"] == 2
            target_root = tmp_path / "restore-target-root"
            assert target_root.is_dir()
            result_body = json.loads(
                (target_root / "restore-result.json").read_text(encoding="utf-8")
            )
            assert result_body["database_imports"] == 2
            assert result_body["writes_enabled"] is False
            assert result_body["provider_calls"] == 0

            control_engine = create_engine(
                _restore_database_url(
                    baseline.manifest, database_names["control-plane-postgres"]
                )
            )
            knowledge_engine = create_engine(
                _restore_database_url(
                    baseline.manifest, database_names["knowledge-postgres"]
                )
            )
            try:
                with control_engine.connect() as connection:
                    assert connection.execute(
                        text("SELECT count(*) FROM tasks WHERE id = :task_id"),
                        {"task_id": baseline.payload["control_plane"]["root_task_id"]},
                    ).scalar_one() == 1
                    assert connection.execute(
                        text("SELECT count(*) FROM execution_inputs WHERE id = :input_id"),
                        {"input_id": baseline.payload["control_plane"]["input_id"]},
                    ).scalar_one() == 1
                with knowledge_engine.connect() as connection:
                    assert connection.execute(
                        text("SELECT count(*) FROM knowledge_candidates WHERE id = :candidate_id"),
                        {"candidate_id": baseline.payload["knowledge"]["candidate_id"]},
                    ).scalar_one() == 1
            finally:
                control_engine.dispose()
                knowledge_engine.dispose()

            restored_session = target_root / "payload" / "akashic" / "sessions.db"
            restored_memory = target_root / "payload" / "akashic" / "memory2.db"
            with sqlite3.connect(restored_session) as connection:
                assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                assert connection.execute("SELECT count(*) FROM messages").fetchone() == (2,)
            with sqlite3.connect(restored_memory) as connection:
                assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                assert connection.execute("SELECT count(*) FROM memory_items").fetchone() == (1,)
            assert (
                target_root / "payload" / "akashic" / "attachments" / "baseline" / "spec.md"
            ).read_bytes() == Path(baseline.payload["akashic"]["attachment_path"]).read_bytes()
            assert (target_root / "payload" / "artifacts").is_dir()
            restored_main_agent = target_root / "payload" / "main-agent" / "store.json"
            assert restored_main_agent.read_bytes() == Path(
                baseline.payload["main_agent"]["store_path"]
            ).read_bytes()

            repository = target_root / "repository"
            cloned_head = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            assert cloned_head.stdout.strip() == baseline.payload["repository"]["head"]
            assert (repository / "baseline-notes.md").is_file()
            assert (target_root / "payload" / "repository" / "git-state.json").is_file()
        finally:
            _drop_restore_databases(database_names)


def test_read_only_services_validate_real_schema_heads_before_serving_queries(
    tmp_path: Path,
) -> None:
    with maintenance_baseline(tmp_path) as baseline:
        from sidera_knowledge.api.app import create_app as create_knowledge_app
        from sidera_knowledge.config import KnowledgeSettings

        from sidera_control_plane.api.app import create_app as create_control_app
        from sidera_control_plane.config import Settings as ControlSettings

        control_settings = ControlSettings(
            database_url=database_url_for(baseline.manifest),
            restore_read_only=True,
            restore_schema_validation=True,
        )
        knowledge_settings = KnowledgeSettings(
            database_url=database_url_for(baseline.manifest, knowledge=True),
            restore_read_only=True,
            restore_schema_validation=True,
        )
        with TestClient(create_control_app(control_settings)) as control_client:
            assert control_client.get("/healthz").status_code == 200
            control_write = control_client.post("/api/v1/tasks", json={})
        with TestClient(create_knowledge_app(knowledge_settings)) as knowledge_client:
            assert knowledge_client.get("/health/live").status_code == 200
            knowledge_write = knowledge_client.post("/api/v1/commands", json={})

        assert control_write.status_code == 409
        assert control_write.json()["error"]["code"] == "restore_read_only"
        assert knowledge_write.status_code == 409
        assert knowledge_write.json()["error"]["code"] == "restore_read_only"


def test_read_only_query_paths_do_not_mutate_restored_databases_or_local_history(
    tmp_path: Path,
) -> None:
    with maintenance_baseline(tmp_path) as baseline:
        from sidera_knowledge.api.app import create_app as create_knowledge_app
        from sidera_knowledge.config import KnowledgeSettings

        from sidera_control_plane.api.app import create_app as create_control_app
        from sidera_control_plane.config import Settings as ControlSettings

        workspace_id = str(baseline.manifest.workspace_id)
        control = baseline.payload["control_plane"]
        root_task_id = control["root_task_id"]
        worker_item_id = control["worker_item_id"]
        execution_id = control["execution_id"]
        session_id = control["session_id"]
        knowledge = baseline.payload["knowledge"]
        mutations: list[str] = []

        def sql_guard(_connection, _cursor, statement, _parameters, _context, _many):
            if _MUTATING_SQL.search(statement):
                mutations.append(statement)

        event.listen(Engine, "before_cursor_execute", sql_guard)
        try:
            control_settings = ControlSettings(
                database_url=database_url_for(baseline.manifest),
                restore_read_only=True,
                restore_schema_validation=True,
            )
            with TestClient(create_control_app(control_settings)) as client:
                before = _postgres_table_counts(client.app.state.database.engine)
                params = {"workspace_id": workspace_id}
                control_requests = (
                    ("/healthz", {}),
                    ("/api/v1/tasks", params),
                    (f"/api/v1/tasks/{root_task_id}", params),
                    (f"/api/v1/tasks/{root_task_id}/timeline", params),
                    (f"/api/v1/tasks/{root_task_id}/overview", params),
                    (f"/api/v1/tasks/{root_task_id}/graph", params),
                    (f"/api/v1/tasks/{root_task_id}/work-items/summary", params),
                    (f"/api/v1/tasks/{root_task_id}/activity", params),
                    (
                        f"/api/v1/tasks/{root_task_id}/plan-diff",
                        {**params, "left": 1, "right": 2},
                    ),
                    (f"/api/v1/workspaces/{workspace_id}/task-overviews", {}),
                    (f"/api/v1/workspaces/{workspace_id}/capabilities", {}),
                    (
                        f"/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/workspace",
                        {},
                    ),
                    (
                        f"/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/activity",
                        {},
                    ),
                    (f"/api/v1/gateway/executions/{execution_id}/binding", params),
                    (f"/api/v1/gateway/executions/{execution_id}/workspace", params),
                    ("/api/v1/gateway/sessions", {**params, "session_id": session_id}),
                    ("/api/v1/gateway/events", {**params, "session_id": session_id}),
                    ("/api/v1/gateway/artifacts", {**params, "task_id": root_task_id}),
                    ("/api/v1/gateway/evidence", {**params, "task_id": root_task_id}),
                    ("/api/v1/gateway/inputs", {**params, "session_id": session_id}),
                    (f"/api/v1/workspaces/{workspace_id}/notification-summary", {}),
                    (f"/api/v1/workspaces/{workspace_id}/notification-metrics", {}),
                    (f"/api/v1/workspaces/{workspace_id}/notification-readiness", {}),
                    (f"/api/v1/workspaces/{workspace_id}/notification-threads", {}),
                    (
                        f"/api/v1/workspaces/{workspace_id}/projection-changes",
                        {"schema_version": 2},
                    ),
                )
                for path, request_params in control_requests:
                    response = client.get(path, params=request_params)
                    assert response.status_code < 500, (
                        f"{path}: {response.status_code} {response.text}"
                    )
                after = _postgres_table_counts(client.app.state.database.engine)
                assert after == before

            knowledge_settings = KnowledgeSettings(
                database_url=database_url_for(baseline.manifest, knowledge=True),
                restore_read_only=True,
                restore_schema_validation=True,
                workspace_id=baseline.manifest.workspace_id,
                local_owner_token=SecretStr("lm-3.4-owner"),
                main_agent_token=SecretStr("lm-3.4-main-agent"),
                control_plane_token=SecretStr("lm-3.4-control-plane"),
            )
            knowledge_headers = {"Authorization": "Bearer lm-3.4-owner"}
            page_id = knowledge["page_id"]
            revision_id = knowledge["revision_id"]
            candidate_id = knowledge["candidate_id"]
            knowledge_command_id = str(uuid4())
            with TestClient(create_knowledge_app(knowledge_settings)) as client:
                before = _postgres_table_counts(client.app.state.database.engine)
                knowledge_requests = (
                    ("/health/live", {}),
                    ("/health/ready", {}),
                    (f"/api/v1/workspaces/{workspace_id}/knowledge/pages", {}),
                    (
                        f"/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}",
                        {},
                    ),
                    (
                        f"/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}/revisions/{revision_id}",
                        {},
                    ),
                    (
                        f"/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}/diff",
                        {"from_revision_id": revision_id, "to_revision_id": revision_id},
                    ),
                    (
                        f"/api/v1/workspaces/{workspace_id}/knowledge/search",
                        {"q": "maintenance"},
                    ),
                    (f"/api/v1/workspaces/{workspace_id}/knowledge/candidates", {}),
                    (
                        f"/api/v1/workspaces/{workspace_id}/knowledge/candidates/{candidate_id}",
                        {},
                    ),
                    (f"/api/v1/workspaces/{workspace_id}/knowledge/sources", {}),
                    (
                        f"/api/v1/workspaces/{workspace_id}/commands/{knowledge_command_id}",
                        {},
                    ),
                )
                for path, request_params in knowledge_requests:
                    response = client.get(
                        path, params=request_params, headers=knowledge_headers
                    )
                    assert response.status_code < 500, (
                        f"{path}: {response.status_code} {response.text}"
                    )
                after = _postgres_table_counts(client.app.state.database.engine)
                assert after == before

        finally:
            event.remove(Engine, "before_cursor_execute", sql_guard)

        assert mutations == []


_MUTATING_SQL = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|ALTER|CREATE|DROP|TRUNCATE|VACUUM|"
    r"GRANT|REVOKE|REFRESH)\b|\bFOR\s+UPDATE\b",
    re.IGNORECASE,
)


def _postgres_table_counts(engine) -> dict[str, int]:
    with engine.connect() as connection:
        names = connection.scalars(
            text(
                "SELECT tablename FROM pg_catalog.pg_tables "
                "WHERE schemaname = 'public' ORDER BY tablename"
            )
        ).all()
        return {
            name: int(
                connection.execute(
                    text(f'SELECT count(*) FROM "{name.replace(chr(34), chr(34) * 2)}"')
                ).scalar_one()
            )
            for name in names
        }


def _write_postgres_export_inventory(baseline) -> Path:
    manifest = baseline.manifest
    reference_root = manifest.run_root / "maintenance-db-refs"
    reference_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    references = {
        "control-plane-postgres": database_url_for(manifest),
        "knowledge-postgres": database_url_for(manifest, knowledge=True),
    }
    for component_id, value in references.items():
        path = reference_root / f"{component_id}.url"
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o600)

    raw_manifest = json.loads(
        (manifest.run_root / "manifest.json").read_text(encoding="utf-8")
    )
    components = [
        {
            "component_id": component_id,
            "kind": "postgres",
            "source_ref": f"postgres:ref:file:{reference_root / f'{component_id}.url'}",
            "restore_relative_path": (
                "databases/control-plane"
                if component_id == "control-plane-postgres"
                else "databases/knowledge"
            ),
            "schema_identity": (
                "alembic:control-plane"
                if component_id == "control-plane-postgres"
                else "alembic:knowledge-service"
            ),
            "required": True,
            "secret_policy": "private_data",
        }
        for component_id in references
    ]
    path = manifest.run_root / "postgres-export-inventory.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": str(manifest.run_id),
                "workspace_id": str(manifest.workspace_id),
                "code_commit": raw_manifest["code_commit"],
                "implementation_fingerprint": raw_manifest.get(
                    "implementation_fingerprint", "0" * 64
                ),
                "runtime_fingerprint": raw_manifest.get(
                    "runtime_fingerprint", "0" * 64
                ),
                "writers": {
                    "status": "stopped",
                    "active_component_ids": [],
                    "observed_at": "2026-09-19T04:00:00+00:00",
                },
                "persistence": {
                    "status": "complete",
                    "component_ids": list(references),
                    "unsupported_component_ids": [],
                },
                "components": components,
                "source_identities": {
                    "database_ref": manifest.database_name,
                    "knowledge_database_ref": manifest.knowledge_database_name,
                },
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _restore_database_url(manifest: DemoManifest, database_name: str) -> str:
    return (
        f"postgresql+psycopg://{quote(manifest.pg_user, safe='')}@"
        f"{manifest.pg_host}:{manifest.pg_port}/{database_name}"
    )


def _create_restore_target_manifest(
    baseline, tmp_path: Path
) -> tuple[Path, dict[str, str]]:
    manifest = baseline.manifest
    admin_url = os.environ["SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL"]
    database_names = {
        "control-plane-postgres": f"sidera_restore_control_{uuid4().hex}",
        "knowledge-postgres": f"sidera_restore_knowledge_{uuid4().hex}",
    }
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            for database_name in database_names.values():
                if RESTORE_DATABASE_NAME.fullmatch(database_name) is None:
                    raise AssertionError(f"unexpected restore database name: {database_name}")
                connection.exec_driver_sql(
                    f'CREATE DATABASE "{database_name}" OWNER "{manifest.pg_user}"'
                )
    finally:
        engine.dispose()

    reference_root = tmp_path / "restore-db-refs"
    reference_root.mkdir(parents=True, mode=0o700)
    targets = []
    for component_id, database_name in database_names.items():
        reference = reference_root / f"{component_id}.url"
        reference.write_text(
            _restore_database_url(manifest, database_name) + "\n", encoding="utf-8"
        )
        reference.chmod(0o600)
        targets.append(
            {
                "component_id": component_id,
                "target_ref": f"postgres:ref:file:{reference}",
                "schema_identity": (
                    "alembic:control-plane"
                    if component_id == "control-plane-postgres"
                    else "alembic:knowledge-service"
                ),
            }
        )
    target_manifest = tmp_path / "restore-target.json"
    target_manifest.write_text(
        json.dumps(
            {
                "run_id": str(uuid4()),
                "workspace_id": str(uuid4()),
                "run_root": str(tmp_path / "restore-target-root"),
                "restore_mode": "read_only",
                "database_targets": targets,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    target_manifest.chmod(0o600)
    return target_manifest, database_names


def _write_local_storage_export_inventory(baseline) -> Path:
    path = _write_postgres_export_inventory(baseline)
    inventory = json.loads(path.read_text(encoding="utf-8"))
    akashic = baseline.payload["akashic"]
    bridge = baseline.payload["bridge"]
    local_components = [
        {
            "component_id": "akashic-sessions",
            "kind": "sqlite",
            "source_ref": f"sqlite:file:{akashic['session_db']}",
            "restore_relative_path": "akashic/sessions.db",
            "schema_identity": "akashic-session-store-v1",
            "required": True,
            "secret_policy": "private_data",
        },
        {
            "component_id": "akashic-memory2",
            "kind": "sqlite",
            "source_ref": f"sqlite:file:{akashic['memory_db']}",
            "restore_relative_path": "akashic/memory2.db",
            "schema_identity": "akashic-memory2-v1",
            "required": True,
            "secret_policy": "private_data",
        },
        {
            "component_id": "akashic-bridge",
            "kind": "metadata",
            "source_ref": f"file:{bridge['state_path']}",
            "restore_relative_path": "akashic/plugin-data.json",
            "schema_identity": "task-bridge-state-v2",
            "required": True,
            "secret_policy": "private_data",
        },
        {
            "component_id": "akashic-attachments",
            "kind": "directory",
            "source_ref": f"file:{Path(akashic['attachment_path']).parent.parent}",
            "restore_relative_path": "akashic/attachments",
            "schema_identity": "attachment-fixture-v1",
            "required": True,
            "secret_policy": "private_data",
        },
        {
            "component_id": "artifact-content",
            "kind": "directory",
            "source_ref": f"file:{baseline.payload['control_plane']['artifact_storage_root']}",
            "restore_relative_path": "artifacts",
            "schema_identity": "artifact-content-store-v1",
            "required": True,
            "secret_policy": "private_data",
        },
        {
            "component_id": "main-agent-conversations",
            "kind": "metadata",
            "source_ref": f"file:{baseline.payload['main_agent']['store_path']}",
            "restore_relative_path": "main-agent/store.json",
            "schema_identity": "demo-main-agent-conversations-v1",
            "required": True,
            "secret_policy": "private_data",
        },
    ]
    inventory["components"].extend(local_components)
    inventory["persistence"]["component_ids"] = [
        component["component_id"] for component in inventory["components"]
    ]
    inventory_path = Path(baseline.manifest.run_root) / "local-storage-export-inventory.json"
    inventory_path.write_text(
        json.dumps(inventory, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    inventory_path.chmod(0o600)
    return inventory_path


def _write_git_export_inventory(baseline) -> Path:
    path = _write_postgres_export_inventory(baseline)
    inventory = json.loads(path.read_text(encoding="utf-8"))
    inventory["components"].append(
        {
            "component_id": "sample-repository",
            "kind": "git",
            "source_ref": f"file:{baseline.manifest.repository}",
            "restore_relative_path": "repository",
            "schema_identity": "git-bundle-v1",
            "required": True,
            "secret_policy": "private_data",
        }
    )
    inventory["persistence"]["component_ids"] = [
        component["component_id"] for component in inventory["components"]
    ]
    inventory_path = Path(baseline.manifest.run_root) / "git-export-inventory.json"
    inventory_path.write_text(
        json.dumps(inventory, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    inventory_path.chmod(0o600)
    return inventory_path


def _write_complete_backup_inventory(baseline) -> tuple[Path, str]:
    inventory_path = _write_local_storage_export_inventory(baseline)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    repository_component = {
        "component_id": "sample-repository",
        "kind": "git",
        "source_ref": f"file:{baseline.manifest.repository}",
        "restore_relative_path": "repository",
        "schema_identity": "git-bundle-v1",
        "required": True,
        "secret_policy": "private_data",
    }
    sentinel = "LM25_SECRET_SENTINEL_DO_NOT_COPY"
    secret_components = []
    for component_id, relative in (
        ("auth-secrets", "auth/credentials.json"),
        ("model-registry-secrets", "model-registry/registry.json"),
        ("keychain-secrets", "keychain/item.json"),
    ):
        source = baseline.manifest.run_root / relative
        source.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        source.write_text(sentinel + "\n", encoding="utf-8")
        source.chmod(0o600)
        secret_components.append(
            {
                "component_id": component_id,
                "kind": "metadata",
                "source_ref": f"file:{source}",
                "restore_relative_path": f"excluded/{relative}",
                "schema_identity": f"excluded:{component_id}:v1",
                "required": False,
                "secret_policy": "exclude",
            }
        )
    inventory["components"].extend([repository_component, *secret_components])
    inventory["persistence"]["component_ids"] = [
        component["component_id"] for component in inventory["components"]
    ]
    path = Path(baseline.manifest.run_root) / "complete-backup-inventory.json"
    path.write_text(
        json.dumps(inventory, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    path.chmod(0o600)
    return path, sentinel


def _run_basic_stack(
    action: str,
    manifest_path: Path,
    environment: dict[str, str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["bash", str(BASIC_STACK), action, "--manifest", str(manifest_path)],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"basic stack {action} failed ({completed.returncode})\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return completed


def _drop_demo_databases(manifest: DemoManifest) -> None:
    admin_url = os.environ["SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL"]
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        for database_name in (
            manifest.database_name,
            manifest.knowledge_database_name,
        ):
            if DATABASE_NAME.fullmatch(database_name) is None:
                raise AssertionError(f"unexpected test database name: {database_name}")
            with engine.connect() as connection:
                connection.exec_driver_sql(
                    f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
                )
    finally:
        engine.dispose()


def _drop_restore_databases(database_names: dict[str, str]) -> None:
    admin_url = os.environ["SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL"]
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        for database_name in database_names.values():
            if RESTORE_DATABASE_NAME.fullmatch(database_name) is None:
                raise AssertionError(f"unexpected restore database name: {database_name}")
            with engine.connect() as connection:
                connection.exec_driver_sql(
                    f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
                )
    finally:
        engine.dispose()
