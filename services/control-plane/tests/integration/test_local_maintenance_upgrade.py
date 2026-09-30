from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from sidera_control_plane.api.app import create_app as create_control_app
from sidera_control_plane.config import Settings as ControlSettings

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[4]
CONTROL_PLANE_ROOT = PROJECT_ROOT / "services" / "control-plane"
KNOWLEDGE_SERVICE_ROOT = PROJECT_ROOT / "services" / "knowledge-service"
EVIDENCE_PATH = (
    PROJECT_ROOT
    / ".sidera"
    / "release-closure"
    / "2026-09-18-continuation"
    / "lm-4.2-n1-old-data.json"
)


def _required_audit_root() -> Path:
    value = os.environ.get("SIDERA_N1_AUDIT_ROOT")
    if not value:
        pytest.fail("SIDERA_N1_AUDIT_ROOT is required")
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        pytest.fail(f"N-1 audit root does not exist: {root}")
    return root


def _read_database_ref(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value.startswith(("postgresql://", "postgresql+psycopg://")):
        raise AssertionError(f"invalid PostgreSQL reference: {path}")
    return value


def _psycopg_url(value: str) -> str:
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value


def _table_digests(database_url: str) -> dict[str, str]:
    engine = create_engine(_psycopg_url(database_url))
    try:
        with engine.connect() as connection:
            table_names = tuple(
                connection.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                        "AND table_name <> 'alembic_version' ORDER BY table_name"
                    )
                ).all()
            )
            result: dict[str, str] = {}
            for table_name in table_names:
                if not table_name.replace("_", "").isalnum():
                    raise AssertionError(f"unexpected table name: {table_name}")
                quoted = table_name.replace('"', '""')
                digest = connection.execute(
                    text(
                        "SELECT md5(COALESCE(string_agg(row_to_json(rows)::text, "
                        "E'\\n' ORDER BY row_to_json(rows)::text), '')) "
                        f'FROM (SELECT * FROM public."{quoted}") AS rows'
                    )
                ).scalar_one()
                result[table_name] = str(digest)
            return result
    finally:
        engine.dispose()


def _migration(
    service_root: Path,
    environment_name: str,
    database_url: str,
) -> str:
    environment = os.environ.copy()
    environment[environment_name] = _psycopg_url(database_url)
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--directory",
            str(service_root),
            "--frozen",
            "alembic",
            "-c",
            "alembic.ini",
            "upgrade",
            "head",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == 0, output

    check = subprocess.run(
        [
            "uv",
            "run",
            "--directory",
            str(service_root),
            "--frozen",
            "alembic",
            "-c",
            "alembic.ini",
            "check",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    check_output = f"{check.stdout}\n{check.stderr}"
    assert check.returncode == 0, check_output
    assert "No new upgrade operations detected" in check_output, check_output
    return output + check_output


def _run_inspect(backup_path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "local-maintenance.py"),
            "inspect-backup",
            "--backup",
            str(backup_path),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sqlite_digests(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        table_names = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        result: dict[str, str] = {}
        for table_name in table_names:
            quoted = table_name.replace('"', '""')
            cursor = connection.execute(f'SELECT * FROM "{quoted}"')
            rows = [
                json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":"))
                for row in cursor.fetchall()
            ]
            columns = [item[0] for item in cursor.description or ()]
            payload = json.dumps(
                {"columns": columns, "rows": sorted(rows)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            result[table_name] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return result


def test_n1_maintenance_upgrade_preserves_real_old_baseline() -> None:
    """The retained N-1 fixture survives backup, import, current migration and reads."""

    audit_root = _required_audit_root()
    if not EVIDENCE_PATH.is_file():
        pytest.fail(f"LM-4.2 evidence is required: {EVIDENCE_PATH}")
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    assert evidence["status"] == "PASS"
    assert evidence["source"]["commit"] == "763ff1fa719d5752d350d688f819d422e9bed48e"

    backup_path = audit_root / "lm-4.3-backup"
    target_root = audit_root / "upgrade-target"
    source_manifest = json.loads(
        (audit_root / "runtime" / "source-manifest.json").read_text(encoding="utf-8")
    )
    target_manifest = json.loads(
        (audit_root / "upgrade-target.json").read_text(encoding="utf-8")
    )
    backup = _run_inspect(backup_path)
    assert backup["status"] == "COMPLETE"
    assert backup["restore_mode"] == "read_only"
    assert {item["component_id"] for item in backup["components"]} == {
        "control-plane-postgres",
        "knowledge-postgres",
        "akashic-sessions",
        "akashic-bridge",
        "old-source-repository",
    }
    assert all(
        fact["active_writer_sessions"] == "0"
        for fact in backup["database_facts"].values()
    )

    restore_result = json.loads(
        (target_root / "restore-result.json").read_text(encoding="utf-8")
    )
    assert restore_result["status"] == "COMPLETE"
    assert restore_result["database_imports"] == 2
    assert restore_result["provider_calls"] == 0
    assert restore_result["source_run_id"] == backup["run_id"]
    assert target_manifest["run_id"] != source_manifest["run_id"]

    source_control = _read_database_ref(
        Path(source_manifest["database_ref"].removeprefix("file:"))
    )
    source_knowledge = _read_database_ref(
        Path(source_manifest["knowledge_database_ref"].removeprefix("file:"))
    )
    target_refs = {
        item["component_id"]: _read_database_ref(
            Path(item["target_ref"].removeprefix("postgres:ref:file:"))
        )
        for item in target_manifest["database_targets"]
    }
    target_control = target_refs["control-plane-postgres"]
    target_knowledge = target_refs["knowledge-postgres"]

    control_upgrade_output = _migration(
        CONTROL_PLANE_ROOT,
        "SIDERA_DATABASE_URL",
        target_control,
    )
    knowledge_upgrade_output = _migration(
        KNOWLEDGE_SERVICE_ROOT,
        "SIDERA_KNOWLEDGE_DATABASE_URL",
        target_knowledge,
    )
    assert "No new upgrade operations detected" in control_upgrade_output
    assert "No new upgrade operations detected" in knowledge_upgrade_output

    assert _table_digests(source_control) == _table_digests(target_control)
    assert _table_digests(source_knowledge) == _table_digests(target_knowledge)
    source_sessions = audit_root / "runtime" / "akashic" / "sessions.db"
    target_sessions = target_root / "payload" / "akashic" / "sessions.db"
    assert _sqlite_digests(source_sessions) == _sqlite_digests(target_sessions)
    session_backup_entry = next(
        item for item in backup["files"] if item["component_id"] == "akashic-sessions"
    )
    assert _sha256(target_sessions) == session_backup_entry["sha256"]
    assert _sha256(
        audit_root
        / "runtime"
        / "akashic"
        / "plugin-data"
        / "sidera-task-bridge-local"
        / ".kv.json"
    ) == _sha256(target_root / "payload" / "akashic" / "plugin-data.json")

    source_repository = audit_root / "source-repository"
    restored_repository = target_root / "repository"
    for repository in (source_repository, restored_repository):
        head = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head == evidence["source"]["commit"]
        assert tree == "1a0589f89de135fb0d161232dc50b69533c5622f"
        assert not subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert (repository / "akashic-agent" / "mcp_servers").is_symlink()

    workspace_one = "20000000-0000-0000-0000-000000000001"
    workspace_two = "20000000-0000-0000-0000-000000000002"
    root_task_id = evidence["control_plane"]["task_seed"]["root_task_id"]
    artifact_id = evidence["control_plane"]["task_seed"]["artifact_id"]
    transition_id = evidence["control_plane"]["unknown_seed"]["transition_id"]
    with TestClient(
        create_control_app(
            ControlSettings(
                database_url=_psycopg_url(target_control),
                restore_read_only=True,
                restore_schema_validation=True,
            )
        )
    ) as control_client:
        assert control_client.get("/healthz").status_code == 200
        tasks = control_client.get(
            "/api/v1/tasks",
            params={"workspace_id": workspace_one, "limit": 100},
        )
        assert tasks.status_code == 200
        task_ids = {item["id"] for item in tasks.json()["items"]}
        assert root_task_id in task_ids
        transition = control_client.get(
            f"/api/v1/workspaces/{workspace_two}/handoff-transitions/{transition_id}"
        )
        assert transition.status_code == 200
        assert transition.json()["status"] == "blocked_unknown"
        assert transition.json()["failure_code"] == "handoff_external_result_unknown"
        assert transition.json()["attention_required"] is True
        artifact = control_client.get(
            f"/api/v1/workspaces/{workspace_one}/artifacts/{artifact_id}/descriptor"
        )
        assert artifact.status_code == 200
        assert artifact.json()["content_hash"] == evidence["control_plane"][
            "task_seed"
        ]["artifact_content_hash"]
        assert artifact.json()["content_available"] is False

    knowledge_source = str(KNOWLEDGE_SERVICE_ROOT / "src")
    if knowledge_source not in sys.path:
        sys.path.insert(0, knowledge_source)
    os.environ["SIDERA_KNOWLEDGE_DATABASE_URL"] = _psycopg_url(target_knowledge)
    from sidera_knowledge.api.app import create_app as create_knowledge_app
    from sidera_knowledge.config import KnowledgeSettings

    page_id = evidence["knowledge"]["seed_ids"]["page_id"]
    revision_id = "c0000000-0000-0000-0000-000000000004"
    headers = {"Authorization": "Bearer n1-owner"}
    with TestClient(
        create_knowledge_app(
            KnowledgeSettings(
                database_url=_psycopg_url(target_knowledge),
                restore_read_only=True,
                restore_schema_validation=True,
                local_owner_token=SecretStr("n1-owner"),
            )
        )
    ) as knowledge_client:
        assert knowledge_client.get("/health/ready").status_code == 200
        without_token = knowledge_client.get(
            f"/api/v1/workspaces/{workspace_one}/knowledge/pages"
        )
        assert without_token.status_code == 403
        pages = knowledge_client.get(
            f"/api/v1/workspaces/{workspace_one}/knowledge/pages",
            headers=headers,
        )
        assert pages.status_code == 200
        assert pages.json()["items"][0]["page_id"] == page_id
        assert (
            pages.json()["items"][0]["current_revision"]["content_hash"]
            == evidence["databases"]["knowledge_service"]["revisions"][1][
                "content_hash"
            ]
        )
        revision = knowledge_client.get(
            f"/api/v1/workspaces/{workspace_one}/knowledge/pages/{page_id}/revisions/{revision_id}",
            headers=headers,
        )
        assert revision.status_code == 200
        assert revision.json()["content_hash"] == "3c28fbe3fc5696f0306a96603f329421db890df32593c619fba4467d75549ea8"
