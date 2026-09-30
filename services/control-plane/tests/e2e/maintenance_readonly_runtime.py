"""Prepare and clean an isolated, disconnected read-only maintenance runtime."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from urllib.parse import quote
from uuid import UUID, uuid4

from sqlalchemy import create_engine

from sidera_control_plane.demo.cli import prepare_demo_run
from sidera_control_plane.demo.contracts import DemoManifest
from sidera_control_plane.demo.environment import (
    allocate_local_ports,
    create_demo_tokens,
)
from sidera_control_plane.demo.manifest import load_manifest, validate_manifest
from sidera_control_plane.persistence.database import Database

PROJECT_ROOT = Path(__file__).resolve().parents[4]
CONTROL_PLANE_ROOT = PROJECT_ROOT / "services" / "control-plane"
KNOWLEDGE_SOURCE_ROOT = PROJECT_ROOT / "services" / "knowledge-service" / "src"
MAINTENANCE_SCRIPT = PROJECT_ROOT / "scripts" / "local-maintenance.py"
SOURCE_SEED = CONTROL_PLANE_ROOT / "tests" / "e2e" / "seed_r4_workspace.py"
KNOWLEDGE_SEED = (
    PROJECT_ROOT
    / "services"
    / "knowledge-service"
    / "tests"
    / "e2e"
    / "seed_r5_3_knowledge.py"
)
DATABASE_NAME = re.compile(r"^sidera_interview_test_[0-9a-f]{32}$")
KNOWLEDGE_DATABASE_NAME = re.compile(
    r"^sidera_interview_knowledge_[0-9a-f]{32}$"
)
SEED_WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "cleanup"))
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(json.dumps({"manifest_path": str(_prepare())}, sort_keys=True))
        return 0
    if args.manifest is None:
        raise SystemExit("--manifest is required for cleanup")
    _cleanup(args.manifest)
    return 0


def _prepare() -> Path:
    workspace_root = Path(tempfile.mkdtemp(prefix="sidera-maintenance-browser-")).resolve()
    source: DemoManifest | None = None
    target_database_names: tuple[str, str] | None = None
    try:
        source = prepare_demo_run("mainline")
        source = _seed_source(source)
        _write_source_markers(source)
        source_inventory = _write_complete_inventory(source)
        backup = workspace_root / "backup"
        _run_maintenance(
            "backup",
            "--manifest",
            str(source.run_root / "manifest.json"),
            "--source-inventory",
            str(source_inventory),
            "--destination",
            str(backup),
        )

        target_run_id = uuid4()
        target_database_names = _target_database_names(target_run_id)
        target_database_names = _create_target_databases(source, target_database_names)
        target_root = workspace_root / "restored"
        target_input = workspace_root / "restore-target.json"
        _write_restore_target(
            source,
            target_input,
            target_run_id,
            target_root,
            target_database_names,
        )
        _run_maintenance(
            "restore",
            "--backup",
            str(backup),
            "--target-manifest",
            str(target_input),
        )

        source_identity = source
        _drop_database_pair(source.database_name, source.knowledge_database_name)
        shutil.rmtree(source.run_root, ignore_errors=True)
        shutil.rmtree(source.repository, ignore_errors=True)
        runtime_manifest = _write_runtime_manifest(
            source_identity,
            target_root=target_root,
            target_run_id=target_run_id,
            target_database_names=target_database_names,
        )
        source = None
        return runtime_manifest
    except BaseException:
        if source is not None:
            _drop_database_pair(source.database_name, source.knowledge_database_name)
            shutil.rmtree(source.run_root, ignore_errors=True)
            shutil.rmtree(source.repository, ignore_errors=True)
        if target_database_names is not None:
            _drop_database_pair(*target_database_names)
        shutil.rmtree(workspace_root, ignore_errors=True)
        raise


def _seed_source(manifest: DemoManifest) -> DemoManifest:
    database = Database(_database_url(manifest, manifest.database_name))
    try:
        control_seed = _load_seed_module(SOURCE_SEED, "sidera_maintenance_r4_seed")
        with database.session() as session:
            control_seed._seed_context_knowledge(session, SEED_WORKSPACE_ID)
            session.commit()
    finally:
        database.dispose()
    knowledge_seed = _load_seed_module(
        KNOWLEDGE_SEED,
        "sidera_maintenance_knowledge_seed",
        extra_path=KNOWLEDGE_SOURCE_ROOT,
    )
    knowledge_database = Database(
        _database_url(manifest, manifest.knowledge_database_name)
    )
    try:
        knowledge_seed._seed(knowledge_database, SEED_WORKSPACE_ID)
    finally:
        knowledge_database.dispose()

    manifest = manifest.model_copy(update={"workspace_id": SEED_WORKSPACE_ID})
    manifest_path = manifest.run_root / "manifest.json"
    manifest_path.write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    manifest_path.chmod(0o600)
    reloaded = load_manifest(manifest_path)
    validate_manifest(reloaded, PROJECT_ROOT)

    store_path = manifest.run_root / "main-agent" / "store.json"
    thread_id = f"maintenance-thread-{manifest.workspace_id}"
    turn_id = f"maintenance-turn-{manifest.workspace_id}"
    document = {
        "demo_main_agent_conversations_v1": {
            "threads": {
                thread_id: {
                    "id": thread_id,
                    "title": "恢复后的维护历史",
                    "created_at": "2026-09-19T04:00:00+00:00",
                    "updated_at": "2026-09-19T04:00:02+00:00",
                    "context_refs": [f"workspace:{manifest.workspace_id}"],
                    "turns": [
                        {
                            "id": turn_id,
                            "thread_id": thread_id,
                            "status": "completed",
                            "created_at": "2026-09-19T04:00:01+00:00",
                            "started_at": "2026-09-19T04:00:01+00:00",
                            "completed_at": "2026-09-19T04:00:02+00:00",
                            "final_response": "恢复副本中的主 Agent 历史仍然可读。",
                            "items": [
                                {
                                    "id": f"{turn_id}:input",
                                    "type": "userMessage",
                                    "data": {"text": "读取恢复后的任务历史。"},
                                },
                                {
                                    "id": f"{turn_id}:output",
                                    "type": "assistantMessage",
                                    "data": {"text": "恢复副本中的主 Agent 历史仍然可读。"},
                                },
                            ],
                            "usage": {"inputTokens": 12, "outputTokens": 8},
                            "error": None,
                            "events": [
                                {
                                    "sequence": 1,
                                    "type": "completed",
                                    "data": {"status": "completed"},
                                }
                            ],
                        }
                    ],
                }
            }
        }
    }
    store_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    store_path.chmod(0o600)
    return manifest


def _load_seed_module(
    path: Path, module_name: str, *, extra_path: Path | None = None
) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load seed module: {path}")
    module = importlib.util.module_from_spec(spec)
    path_value = str(extra_path) if extra_path is not None else None
    if path_value is not None:
        sys.path.insert(0, path_value)
    try:
        spec.loader.exec_module(module)
    finally:
        if path_value is not None:
            sys.path.remove(path_value)
    return module


def _write_source_markers(manifest: DemoManifest) -> None:
    for name in (
        "state",
        "workspaces",
        "artifacts",
        "results",
        "planning",
        "candidate-spool",
        "knowledge-spool",
    ):
        directory = manifest.run_root / name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = directory / "maintenance-browser-fixture.json"
        if not any(directory.iterdir()):
            marker.write_text(
                json.dumps(
                    {"schema_version": 1, "component": name, "read_only": True},
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            marker.chmod(0o600)


def _write_complete_inventory(manifest: DemoManifest) -> Path:
    source_path = manifest.run_root / "maintenance-inventory.json"
    inventory = json.loads(source_path.read_text(encoding="utf-8"))
    inventory["workspace_id"] = str(manifest.workspace_id)
    components = inventory["components"]
    reference_root = manifest.run_root / "maintenance-database-refs"
    reference_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(reference_root, 0o700)
    database_names = {
        "control-plane-postgres": manifest.database_name,
        "knowledge-postgres": manifest.knowledge_database_name,
    }
    for component in components:
        component_id = component.get("component_id")
        database_name = database_names.get(component_id)
        if database_name is None:
            continue
        reference = reference_root / f"{component_id}.url"
        reference.write_text(
            _database_url(manifest, database_name) + "\n", encoding="utf-8"
        )
        reference.chmod(0o600)
        component["source_ref"] = f"postgres:ref:file:{reference}"
    inventory["writers"] = {
        "status": "stopped",
        "active_component_ids": [],
        "observed_at": "2026-09-19T04:00:00+00:00",
    }
    inventory["persistence"] = {
        "status": "complete",
        "component_ids": [item["component_id"] for item in components],
        "unsupported_component_ids": [],
    }
    output = manifest.run_root / "maintenance-browser-inventory.json"
    output.write_text(
        json.dumps(inventory, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    output.chmod(0o600)
    return output


def _target_database_names(run_id: UUID) -> tuple[str, str]:
    control = f"sidera_interview_test_{run_id.hex}"
    knowledge = f"sidera_interview_knowledge_{run_id.hex}"
    if not DATABASE_NAME.fullmatch(control) or not KNOWLEDGE_DATABASE_NAME.fullmatch(knowledge):
        raise ValueError("generated target database name is invalid")
    return control, knowledge


def _create_target_databases(
    source: DemoManifest, names: tuple[str, str]
) -> tuple[str, str]:
    admin_url = os.environ.get("SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL")
    if not admin_url:
        raise RuntimeError("SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL is required")
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            for name in names:
                connection.exec_driver_sql(
                    f'CREATE DATABASE "{_quote_identifier(name)}" OWNER "{_quote_identifier(source.pg_user)}"'
                )
    finally:
        engine.dispose()
    return names


def _write_restore_target(
    source: DemoManifest,
    path: Path,
    run_id: UUID,
    target_root: Path,
    database_names: tuple[str, str],
) -> None:
    reference_root = path.parent / "restore-db-refs"
    reference_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    targets = []
    for component_id, database_name, schema_identity in (
        ("control-plane-postgres", database_names[0], "alembic:control-plane"),
        ("knowledge-postgres", database_names[1], "alembic:knowledge-service"),
    ):
        reference = reference_root / f"{component_id}.url"
        reference.write_text(_database_url(source, database_name) + "\n", encoding="utf-8")
        reference.chmod(0o600)
        targets.append(
            {
                "component_id": component_id,
                "target_ref": f"postgres:ref:file:{reference}",
                "schema_identity": schema_identity,
            }
        )
    path.write_text(
        json.dumps(
            {
                "run_id": str(run_id),
                "workspace_id": str(source.workspace_id),
                "run_root": str(target_root),
                "restore_mode": "read_only",
                "database_targets": targets,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_runtime_manifest(
    source_identity: DemoManifest,
    *,
    target_root: Path,
    target_run_id: UUID,
    target_database_names: tuple[str, str],
) -> Path:
    data_root = target_root / "payload"
    runtime_root = target_root / "runtime"
    control_port, knowledge_port, gateway_port, web_port = allocate_local_ports(4)
    raw = source_identity.model_dump(mode="json")
    raw.update(
        {
            "run_id": str(target_run_id),
            "run_root": str(data_root),
            "repository": str(data_root / "repository"),
            "database_name": target_database_names[0],
            "knowledge_database_name": target_database_names[1],
            "endpoints": {
                "control_plane": f"http://127.0.0.1:{control_port}",
                "knowledge": f"http://127.0.0.1:{knowledge_port}",
                "main_agent": f"http://127.0.0.1:{gateway_port}",
                "web": f"http://127.0.0.1:{web_port}",
            },
            "maintenance_inventory_ref": None,
            "restore_mode": "read_only",
            "runtime_root": str(runtime_root),
        }
    )
    manifest = DemoManifest.model_validate(raw)
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime_root, 0o700)
    create_demo_tokens(runtime_root)
    path = runtime_root / "manifest.json"
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _database_url(manifest: DemoManifest, database_name: str) -> str:
    return (
        f"postgresql+psycopg://{quote(manifest.pg_user, safe='')}@"
        f"{manifest.pg_host}:{manifest.pg_port}/{database_name}"
    )


def _safe_environment(**values: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PGPASSWORD", "PGPASSFILE"}
    }
    environment.update(values)
    return environment


def _run(command: list[str], environment: dict[str, str]) -> None:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command_failed:{completed.returncode}:"
            f"stdout={completed.stdout[-2000:]}"
            f"stderr={completed.stderr[-2000:]}"
        )


def _run_maintenance(*arguments: str) -> None:
    _run(
        [sys.executable, str(MAINTENANCE_SCRIPT), *arguments],
        _safe_environment(),
    )


def _drop_database_pair(control: str, knowledge: str) -> None:
    if not control or not knowledge:
        return
    if not DATABASE_NAME.fullmatch(control):
        return
    if not KNOWLEDGE_DATABASE_NAME.fullmatch(knowledge):
        return
    admin_url = os.environ.get("SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL")
    if not admin_url:
        return
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            for name in (control, knowledge):
                connection.exec_driver_sql(
                    f'DROP DATABASE IF EXISTS "{_quote_identifier(name)}" WITH (FORCE)'
                )
    finally:
        engine.dispose()


def _cleanup(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path.expanduser().resolve())
    target_root = manifest.run_root.parent
    _drop_database_pair(manifest.database_name, manifest.knowledge_database_name)
    shutil.rmtree(target_root.parent, ignore_errors=True)


def _quote_identifier(value: str) -> str:
    return value.replace('"', '""')


if __name__ == "__main__":
    raise SystemExit(main())
