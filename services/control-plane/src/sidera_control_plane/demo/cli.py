from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from sidera_control_plane.local_gate_record import (
    IMPLEMENTATION_FINGERPRINT_VERSION,
    current_commit,
    implementation_fingerprint,
)
from sidera_control_plane.maintenance_inventory import write_inventory
from sidera_control_plane.persistence.database import Database

from .acceptance_batch import (
    build_batch,
    build_entry,
    write_batch,
    write_current_batch,
)
from .bootstrap import bootstrap_demo
from .checks import (
    action_catalog_fingerprint,
    action_catalog_ids,
    build_report,
    register_child_run,
)
from .contracts import DemoEndpoints, DemoManifest, Scenario
from .database import (
    DemoDatabaseError,
    assert_owned_database,
    create_demo_databases,
    database_url_for,
)
from .environment import (
    DemoTokens,
    allocate_local_ports,
    component_environment,
    create_demo_tokens,
    runtime_root_for,
)
from .http_client import DemoClient, DemoClientError
from .manifest import load_manifest, stable_id, validate_manifest
from .safety import audit_demo_configuration
from .sample_repository import create_sample_repository
from .scenarios import prepare_checkpoint, prepare_scenario
from .state import DemoStateStore
from .supervisor import (
    DemoSupervisor,
    ProcessIdentity,
    SupervisorError,
    build_demo_process_specs,
    read_run_phase,
    write_run_phase,
)
from .test_runner import (
    build_interview_test_environment,
    run_interview_tests,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_CONTROL_PLANE_ROOT = _PROJECT_ROOT / "services" / "control-plane"
_KNOWLEDGE_ROOT = _PROJECT_ROOT / "services" / "knowledge-service"
_GATEWAY_ROOT = _PROJECT_ROOT / "services" / "main-agent-gateway"
_WEB_ROOT = _PROJECT_ROOT / "apps" / "web"
_SCENARIOS: tuple[Scenario, ...] = (
    "mainline",
    "revision",
    "handoff",
    "rework",
    "recovery",
    "knowledge",
    "governance",
    "controls",
    "history",
)
_GATES = ("parallel", "before-artifact", "handoff", "governance")
_SUITES = ("unit", "integration", "browser", "safety", "all")
_COVERAGE_BROWSER_TESTS = (
    "Main Agent exposes explicit interruption and unsupported/offline boundaries",
    "planning form creates a request before execution and can cancel it",
    "notification settings expose server readiness and browser permission state",
    "task tabs keep inactive close, draft isolation, and a valid last entry",
    "two task tabs keep view position and WorkerItem drafts isolated",
    "replaying one execution input is idempotent",
    "queued execution inputs can be edited and cancelled before delivery",
    "blocked execution input can be retried without creating a second input",
    "stopping a WorkerItem execution stays scoped to that execution",
    "an interrupted result check retries the same candidate",
    "queued execution worker pin can be cleared without changing the execution",
    "additional rework budget stays scoped to the current result",
    "a blocked result context assembly retries through the knowledge service",
)


class DemoCliError(RuntimeError):
    """A fixed interview-demo CLI operation could not complete safely."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sidera-interview-demo")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--scenario", choices=_SCENARIOS, default="mainline")
    prepare.add_argument("--print-manifest-path", action="store_true")

    for name in ("start", "status", "doctor", "stop", "reset", "open"):
        operation = commands.add_parser(name)
        operation.add_argument("--manifest", required=True)

    present = commands.add_parser("present")
    present.add_argument("--manifest", required=True)

    restart = commands.add_parser("restart")
    restart.add_argument("--manifest", required=True)
    restart.add_argument("--component", required=True)

    continue_parser = commands.add_parser("continue")
    continue_parser.add_argument("--manifest", required=True)
    continue_parser.add_argument("--gate", required=True, choices=_GATES)

    check = commands.add_parser("check")
    check.add_argument("--suite", required=True, choices=_SUITES)
    check.add_argument("--manifest")
    check.add_argument("--pg-port", type=int)

    report = commands.add_parser("report")
    report.add_argument("--manifest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            manifest = prepare_demo_run(cast(Scenario, args.scenario))
            path = manifest.run_root / "manifest.json"
            if args.print_manifest_path:
                print(path)
            else:
                _print_json(
                    {
                        "status": "prepared",
                        "run_id": str(manifest.run_id),
                        "manifest_path": str(path),
                    }
                )
            return 0

        if args.command == "check":
            manifest = (
                _load_validated_manifest(Path(args.manifest))
                if args.manifest is not None
                else None
            )
            result = run_check_suite(
                cast(str, args.suite), manifest=manifest, pg_port=args.pg_port
            )
            _print_json(result)
            return 0 if result.get("status") == "passed" else 1

        manifest = _load_validated_manifest(Path(args.manifest))
        if args.command == "report":
            result = build_report(manifest)
            _print_json(result)
            return 0 if result.get("readiness") != "NOT_READY" else 1
        if args.command == "start":
            identities = start_run(manifest)
            _print_json(
                {
                    "status": "running",
                    "run_id": str(manifest.run_id),
                    "components": sorted(identities),
                }
            )
            return 0
        if args.command == "present":
            identities = start_run(manifest)
            preparation = prepare_scenario(manifest)
            checkpoint = prepare_checkpoint(manifest)
            _print_json(
                {
                    **checkpoint,
                    "preparation": preparation,
                    "status": "presented",
                    "components": sorted(identities),
                    "operator_action": checkpoint["pending_action"],
                }
            )
            return 0
        if args.command == "status":
            _print_json(status_run(manifest))
            return 0
        if args.command == "restart":
            identity = restart_run(manifest, args.component)
            _print_json(
                {
                    "status": "restarted",
                    "run_id": str(manifest.run_id),
                    "component": identity.component,
                    "pid": identity.pid,
                }
            )
            return 0
        if args.command == "doctor":
            failures = doctor_run(manifest)
            _print_json(
                {
                    "status": "passed" if not failures else "failed",
                    "run_id": str(manifest.run_id),
                    "failures": list(failures),
                }
            )
            return 0 if not failures else 1
        if args.command == "continue":
            continue_run(manifest, args.gate)
            _print_json(
                {"status": "released", "run_id": str(manifest.run_id), "gate": args.gate}
            )
            return 0
        if args.command in {"stop", "reset"}:
            stopped = stop_run(manifest)
            if args.command == "reset":
                replacement = prepare_demo_run(manifest.scenario, pg_port=manifest.pg_port)
                _print_json(
                    {
                        "status": "reset",
                        "previous_run_id": str(manifest.run_id),
                        "run_id": str(replacement.run_id),
                        "manifest_path": str(replacement.run_root / "manifest.json"),
                        "stopped_components": list(stopped),
                        "records_retained": True,
                    }
                )
                return 0
            _print_json(
                {
                    "status": "stopped",
                    "run_id": str(manifest.run_id),
                    "components": list(stopped),
                    "records_retained": True,
                }
            )
            return 0
        if args.command == "open":
            open_run(manifest)
            _print_json({"status": "opened", "url": manifest.endpoints.web})
            return 0
        raise DemoCliError("unknown_command")
    except DemoCliError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except SupervisorError as exc:
        print(f"demo_command_failed:{exc}", file=sys.stderr)
        return 1
    except (
        DemoClientError,
        OSError,
        SQLAlchemyError,
        ValueError,
        httpx.HTTPError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"demo_command_failed:{type(exc).__name__}", file=sys.stderr)
        return 1


def prepare_demo_run(scenario: Scenario, *, pg_port: int = 55432) -> DemoManifest:
    run_id = uuid4()
    storage_root = (_PROJECT_ROOT / ".sidera" / "interview-demo").resolve(
        strict=False
    )
    if not _is_within(storage_root, _PROJECT_ROOT.resolve(strict=False)):
        raise DemoCliError("demo_storage_must_be_inside_sidera")
    storage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(storage_root, 0o700)
    run_root = storage_root / str(run_id)
    run_root.mkdir(mode=0o700, exist_ok=False)
    os.chmod(run_root, 0o700)

    repository_parent = Path(tempfile.gettempdir()).resolve(strict=False)
    repository, base_commit = create_sample_repository(repository_parent)
    code_commit = current_commit(_PROJECT_ROOT)
    code_fingerprint = implementation_fingerprint(_PROJECT_ROOT)
    ports = allocate_local_ports(4)
    module_keys = (
        "worker_primary",
        "worker_secondary",
        "workspace",
        "planner",
        "collaboration",
        "verification",
        "notification",
    )
    worker_keys = (
        "validation",
        "report",
        "review",
        "integration",
        "replacement",
        "verification",
    )
    manifest = DemoManifest(
        schema_version=2,
        run_id=run_id,
        workspace_id=stable_id(run_id, "workspace"),
        scenario=scenario,
        run_root=run_root,
        repository=repository,
        base_commit=base_commit,
        code_commit=code_commit,
        implementation_fingerprint=code_fingerprint,
        fingerprint_version=IMPLEMENTATION_FINGERPRINT_VERSION,
        maintenance_inventory_ref=str(run_root / "maintenance-inventory.json"),
        database_name=f"sidera_interview_test_{run_id.hex}",
        knowledge_database_name=f"sidera_interview_knowledge_{run_id.hex}",
        pg_port=pg_port,
        endpoints=DemoEndpoints(
            control_plane=f"http://127.0.0.1:{ports[0]}",
            knowledge=f"http://127.0.0.1:{ports[1]}",
            main_agent=f"http://127.0.0.1:{ports[2]}",
            web=f"http://127.0.0.1:{ports[3]}",
        ),
        module_ids={
            key: stable_id(run_id, f"module-instance:{key}")
            for key in module_keys
        },
        worker_ids={key: stable_id(run_id, f"worker:{key}") for key in worker_keys},
        planning_profile_id=stable_id(run_id, "planning-profile"),
    )
    validate_manifest(manifest, _PROJECT_ROOT)
    manifest_path = run_root / "manifest.json"
    _write_private_text(manifest_path, manifest.model_dump_json(indent=2))
    tokens = create_demo_tokens(run_root)
    _prepare_run_directories(manifest)
    write_run_phase(manifest, "preparing")
    pgpassfile = _selected_pgpassfile(os.environ)
    try:
        _create_demo_databases(manifest, pgpassfile)
        _migrate_databases(manifest, tokens, os.environ, pgpassfile)
        with _postgres_authentication(pgpassfile):
            database = Database(database_url_for(manifest))
            try:
                bootstrap_demo(manifest, database=database, check_only=False)
            finally:
                database.dispose()
        _run_notification_baseline(manifest, tokens, pgpassfile=pgpassfile)
        DemoStateStore(run_root / "state", run_id)
        _write_demo_maintenance_inventory(manifest)
        write_run_phase(manifest, "prepared")
        return manifest
    except BaseException:
        write_run_phase(manifest, "failed")
        raise


def start_run(manifest: DemoManifest) -> dict[str, ProcessIdentity]:
    violations = audit_demo_configuration(manifest)
    if violations:
        raise DemoCliError("unsafe_demo_configuration:" + ",".join(violations))
    _write_process_log(
        runtime_root_for(manifest) / "logs" / "safety-audit.log",
        f"run_id={manifest.run_id}\ncode_commit={manifest.code_commit}\nstatus=passed\n",
        "",
    )
    specs = build_demo_process_specs(
        manifest,
        pgpassfile=_selected_pgpassfile(os.environ),
    )
    return DemoSupervisor(manifest, specs=specs).start()


def _run_notification_baseline(
    manifest: DemoManifest,
    tokens: DemoTokens,
    *,
    pgpassfile: Path | None,
) -> None:
    """Initialize the demo's notification cursors before any live workers run."""

    command = (
        str(_python_for(_CONTROL_PLANE_ROOT)),
        "-u",
        "-m",
        "sidera_control_plane.notification_worker",
        "--baseline",
        "--workspace-id",
        str(manifest.workspace_id),
        "--owner-id",
        f"interview-demo-baseline:{manifest.run_id}",
    )
    environment = component_environment(
        manifest,
        "notification-worker",
        tokens,
        os.environ,
        pgpassfile=pgpassfile,
    )
    log_path = manifest.run_root / "logs" / "notification-baseline.log"
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(log_path.parent, 0o700)
    with log_path.open("w", encoding="utf-8") as log_file:
        completed = subprocess.run(
            command,
            cwd=_CONTROL_PLANE_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=20,
        )
    os.chmod(log_path, 0o600)
    if completed.returncode != 0:
        raise DemoCliError("notification_baseline_failed")


def status_run(manifest: DemoManifest) -> dict[str, object]:
    client = DemoClient(manifest)
    try:
        demo_status = client.get("/api/v1/demo/status")
    finally:
        client.close()
    if not isinstance(demo_status, dict) or demo_status.get("run_id") != str(
        manifest.run_id
    ):
        raise DemoCliError("demo_status_identity_mismatch")
    phase = read_run_phase(manifest)
    if demo_status.get("phase") != phase:
        raise DemoCliError("demo_status_phase_mismatch")
    supervisor = DemoSupervisor(
        manifest,
        specs=build_demo_process_specs(manifest, include_environment=False),
    )
    return {
        "status": "ok",
        "run_id": str(manifest.run_id),
        "demo": demo_status,
        "processes": supervisor.check(),
    }


def doctor_run(manifest: DemoManifest) -> tuple[str, ...]:
    failures: list[str] = []
    specs = build_demo_process_specs(manifest, include_environment=False)
    process_states = DemoSupervisor(manifest, specs=specs).check()
    missing = [name for name, running in process_states.items() if not running]
    if missing:
        failures.append("process_identity_invalid:" + ",".join(missing))

    pgpassfile = _selected_pgpassfile(os.environ)
    try:
        with _postgres_authentication(pgpassfile):
            assert_owned_database(manifest, manifest.database_name)
            assert_owned_database(manifest, manifest.knowledge_database_name)
            _require_database_head(
                database_url_for(manifest),
                _CONTROL_PLANE_ROOT / "alembic.ini",
            )
            _require_database_head(
                database_url_for(manifest, knowledge=True),
                _KNOWLEDGE_ROOT / "alembic.ini",
            )
    except (DemoCliError, DemoDatabaseError, OSError, SQLAlchemyError, ValueError):
        failures.append("database_ownership_or_migration_invalid")

    try:
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
            control_health = _get_json(client, f"{manifest.endpoints.control_plane}/healthz")
            knowledge_health = _get_json(
                client, f"{manifest.endpoints.knowledge}/health/ready"
            )
            gateway_health = _get_json(
                client, f"{manifest.endpoints.main_agent}/main-agent/v1/health"
            )
            web_response = client.get(manifest.endpoints.web)
        if not isinstance(control_health, dict) or control_health.get("status") != "ok":
            failures.append("control_plane_unhealthy")
        if not isinstance(knowledge_health, dict) or knowledge_health.get("status") != "ok":
            failures.append("knowledge_service_unhealthy")
        if not isinstance(gateway_health, dict) or gateway_health.get("provider") != "available":
            failures.append("main_agent_gateway_unhealthy")
        if not 200 <= web_response.status_code < 300:
            failures.append("web_unhealthy")
    except (DemoCliError, OSError, httpx.HTTPError):
        failures.append("local_http_health_check_failed")

    client = DemoClient(manifest)
    try:
        demo_status = client.get("/api/v1/demo/status")
        tasks = client.get(f"/api/v1/tasks?workspace_id={manifest.workspace_id}")
        if not isinstance(demo_status, dict) or any(
            demo_status.get(key) != expected
            for key, expected in (
                ("mode", "interview-demo"),
                ("run_id", str(manifest.run_id)),
                ("workspace_id", str(manifest.workspace_id)),
                ("scenario", manifest.scenario),
                ("phase", "running"),
            )
        ):
            failures.append("demo_status_identity_invalid")
        if not isinstance(tasks, dict) or tasks.get("total") != 0 or tasks.get("items") != []:
            failures.append("mainline_not_empty")
    except (DemoClientError, OSError, ValueError):
        failures.append("control_plane_demo_status_unavailable")
    finally:
        client.close()
    return tuple(failures)


def continue_run(
    manifest: DemoManifest, gate: str
) -> None:
    if gate not in _GATES:
        raise DemoCliError("unknown_simulation_gate")
    store = DemoStateStore(runtime_root_for(manifest) / "state", manifest.run_id)
    store.set_gate(cast(AnyGate, gate), True)


def stop_run(manifest: DemoManifest) -> tuple[str, ...]:
    specs = build_demo_process_specs(manifest, include_environment=False)
    return DemoSupervisor(manifest, specs=specs).stop(timeout_seconds=20)


def restart_run(
    manifest: DemoManifest,
    component: str,
    *,
    supervisor: DemoSupervisor | None = None,
) -> ProcessIdentity:
    active_supervisor = supervisor or DemoSupervisor(
        manifest,
        specs=build_demo_process_specs(manifest),
    )
    return active_supervisor.restart(component, timeout_seconds=20)


def reset_run(
    manifest: DemoManifest,
    *,
    supervisor: DemoSupervisor | None = None,
) -> tuple[str, ...]:
    active_supervisor = supervisor or DemoSupervisor(
        manifest,
        specs=build_demo_process_specs(manifest, include_environment=False),
    )
    return active_supervisor.stop(timeout_seconds=20)


def open_run(manifest: DemoManifest) -> bool:
    entry_url = manifest.endpoints.web
    audit_path = runtime_root_for(manifest) / "reports" / "preparation-audit.json"
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        audit = None
    if isinstance(audit, dict) and isinstance(audit.get("entry_url"), str):
        candidate = audit["entry_url"]
        base = urlsplit(manifest.endpoints.web)
        parsed = urlsplit(candidate)
        if (
            parsed.scheme == base.scheme
            and parsed.netloc == base.netloc
            and parsed.path.startswith(base.path.rstrip("/") + "/")
            and not parsed.fragment
        ):
            entry_url = candidate
    return webbrowser.open(entry_url, new=2)


def _prepare_run_directories(manifest: DemoManifest) -> None:
    for name in (
        "main-agent",
        "workspaces",
        "artifacts",
        "results",
        "planning",
        "candidate-spool",
        "knowledge-spool",
    ):
        directory = manifest.run_root / name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    _write_private_text(manifest.run_root / "main-agent" / "store.json", "{}\n")


def _write_demo_maintenance_inventory(manifest: DemoManifest) -> None:
    """Record actual demo paths and explicit gaps without claiming a backup."""

    run_root = manifest.run_root
    private_directory = lambda name: {
        "component_id": name,
        "kind": "directory",
        "source_ref": f"file:{run_root / name}",
        "restore_relative_path": name,
        "schema_identity": f"demo-directory:{name}:v1",
        "required": True,
        "secret_policy": "private_data",
    }
    components = [
        private_directory(name)
        for name in (
            "state",
            "workspaces",
            "artifacts",
            "results",
            "planning",
            "candidate-spool",
            "knowledge-spool",
        )
    ]
    components.append(
        {
            "component_id": "main-agent-conversations",
            "kind": "metadata",
            "source_ref": f"file:{run_root / 'main-agent' / 'store.json'}",
            "restore_relative_path": "main-agent/store.json",
            "schema_identity": "demo-main-agent-conversations-v1",
            "required": True,
            "secret_policy": "private_data",
        }
    )
    components.extend(
        [
            {
                "component_id": "sample-repository",
                "kind": "git",
                "source_ref": f"file:{manifest.repository}",
                "restore_relative_path": "repository",
                "schema_identity": f"git:{manifest.base_commit}",
                "required": True,
                "secret_policy": "private_data",
            },
            {
                "component_id": "demo-tokens",
                "kind": "metadata",
                "source_ref": f"file:{run_root / 'tokens.json'}",
                "restore_relative_path": "tokens.json",
                "schema_identity": "demo-tokens-v1",
                "required": False,
                "secret_policy": "exclude",
            },
            {
                "component_id": "control-plane-postgres",
                "kind": "postgres",
                "source_ref": (
                    f"postgres:{manifest.database_name}@127.0.0.1:{manifest.pg_port}"
                ),
                "restore_relative_path": "databases/control-plane",
                "schema_identity": "alembic:control-plane",
                "required": True,
                "secret_policy": "private_data",
            },
            {
                "component_id": "knowledge-postgres",
                "kind": "postgres",
                "source_ref": (
                    f"postgres:{manifest.knowledge_database_name}"
                    f"@127.0.0.1:{manifest.pg_port}"
                ),
                "restore_relative_path": "databases/knowledge",
                "schema_identity": "alembic:knowledge-service",
                "required": True,
                "secret_policy": "private_data",
            },
        ]
    )
    write_inventory(
        run_root / "maintenance-inventory.json",
        run_id=str(manifest.run_id),
        workspace_id=str(manifest.workspace_id),
        code_commit=manifest.code_commit,
        implementation_fingerprint=manifest.implementation_fingerprint or "0" * 64,
        runtime_fingerprint="0" * 64,
        components=components,
        unsupported_component_ids=[
            "control-plane-postgres",
            "knowledge-postgres",
        ],
        source_identities={
            "database_ref": manifest.database_name,
            "knowledge_database_ref": manifest.knowledge_database_name,
        },
    )


def run_check_suite(
    suite: str,
    *,
    manifest: DemoManifest | None = None,
    pg_port: int | None = None,
) -> dict[str, object]:
    if suite == "unit":
        return _run_unit_suite()
    if suite == "safety":
        return _run_safety_suite()
    if manifest is None:
        raise DemoCliError("manifest_required_for_this_suite")
    if suite == "integration":
        return _run_integration_suite(manifest, pg_port=pg_port)
    if suite == "browser":
        return _run_browser_suite(manifest)
    if suite == "all":
        results = [
            _run_unit_suite(),
            _run_integration_suite(manifest, pg_port=pg_port),
            _run_browser_suite(manifest),
            _run_safety_suite(),
        ]
        return {
            "status": "passed" if all(item["status"] == "passed" for item in results) else "failed",
            "run_id": str(manifest.run_id),
            "suites": results,
        }
    raise DemoCliError("unknown_test_suite")


def _run_integration_suite(
    parent_manifest: DemoManifest, *, pg_port: int | None = None
) -> dict[str, object]:
    child_manifest = prepare_demo_run(
        parent_manifest.scenario,
        pg_port=parent_manifest.pg_port if pg_port is None else pg_port,
    )
    pgpassfile = _selected_pgpassfile(os.environ)
    environment = build_interview_test_environment(
        child_manifest,
        inherited=os.environ,
        pgpassfile=pgpassfile,
    )
    log_path = child_manifest.run_root / "logs" / "check-integration.log"
    try:
        result = run_interview_tests(environment)
    except subprocess.TimeoutExpired as exc:
        _write_process_log(log_path, exc.stdout, exc.stderr)
        write_run_phase(child_manifest, "failed")
        register_child_run(parent_manifest, child_manifest, suite="integration", status="failed")
        return {
            "status": "failed",
            "suite": "integration",
            "run_id": str(child_manifest.run_id),
            "error": "integration_test_timeout",
        }
    _write_process_log(log_path, result.stdout, result.stderr)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    register_child_run(
        parent_manifest,
        child_manifest,
        suite="integration",
        status="passed" if result.returncode == 0 else "failed",
    )
    return {
        "status": "passed" if result.returncode == 0 else "failed",
        "suite": "integration",
        "run_id": str(child_manifest.run_id),
        "exit_code": result.returncode,
    }


def _run_unit_suite() -> dict[str, object]:
    environment = _test_environment()
    commands = (
        (
            _python_for(_CONTROL_PLANE_ROOT),
            _CONTROL_PLANE_ROOT,
            ("-m", "pytest", "tests/unit", "-q"),
        ),
        (
            _python_for(_GATEWAY_ROOT),
            _GATEWAY_ROOT,
            ("-m", "pytest", "tests/unit", "-q"),
        ),
    )
    for executable, cwd, tail in commands:
        completed = subprocess.run(
            [str(executable), *tail],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if completed.stdout:
            sys.stdout.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        if completed.returncode:
            return {"status": "failed", "suite": "unit", "exit_code": completed.returncode}
    npm = shutil.which("npm")
    if npm is None:
        raise DemoCliError("npm_unavailable")
    completed = subprocess.run(
        [npm, "--prefix", str(_WEB_ROOT), "test"],
        cwd=_PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "suite": "unit",
        "exit_code": completed.returncode,
    }


def _run_safety_suite() -> dict[str, object]:
    commands = (
        (
            _CONTROL_PLANE_ROOT,
            _python_for(_CONTROL_PLANE_ROOT),
            (
                "tests/unit/test_interview_environment.py",
                "tests/unit/test_interview_manifest.py",
                "tests/unit/test_interview_safety.py",
                "tests/unit/test_interview_supervisor.py",
                "tests/unit/test_interview_test_runner.py",
            ),
        ),
        (
            _GATEWAY_ROOT,
            _python_for(_GATEWAY_ROOT),
            (
                "tests/unit/test_demo_safety.py",
                "tests/unit/test_demo_store.py",
            ),
        ),
    )
    for cwd, executable, files in commands:
        completed = subprocess.run(
            [str(executable), "-m", "pytest", "-q", *files],
            cwd=cwd,
            env=_test_environment(),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.stdout:
            sys.stdout.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        if completed.returncode:
            return {
                "status": "failed",
                "suite": "safety",
                "exit_code": completed.returncode,
                "project": cwd.name,
            }
    return {"status": "passed", "suite": "safety", "exit_code": 0}


def _run_browser_suite(parent_manifest: DemoManifest) -> dict[str, object]:
    config = _WEB_ROOT / "playwright.interview.config.ts"
    if not config.is_file():
        raise DemoCliError("browser_suite_not_implemented_yet")
    npm = shutil.which("npm")
    if npm is None:
        raise DemoCliError("npm_unavailable")
    environment = _test_environment(
        {
            "SIDERA_INTERVIEW_DEMO_MANIFEST": str(parent_manifest.run_root / "manifest.json"),
            "SIDERA_INTERVIEW_DEMO_PARENT_MANIFEST": str(parent_manifest.run_root / "manifest.json"),
        }
    )
    browser_files = tuple(
        sorted((_WEB_ROOT / "tests" / "interview").glob("*.spec.ts"))
    )
    if not browser_files:
        raise DemoCliError("browser_suite_has_no_spec_files")
    batch = _create_browser_batch(parent_manifest, browser_files)
    environment["SIDERA_INTERVIEW_BATCH_ID"] = str(batch["batch_id"])
    project_results: list[dict[str, object]] = []
    for project in ("desktop", "tablet", "mobile"):
        for browser_file in browser_files:
            selectors = (
                _COVERAGE_BROWSER_TESTS
                if browser_file.name == "coverage-gaps.spec.ts"
                else (None,)
            )
            for selector in selectors:
                command = [
                    npm,
                    "--prefix",
                    str(_WEB_ROOT),
                    "run",
                    "test:e2e",
                    "--",
                    "--config",
                    str(config),
                    str(browser_file),
                    "--project",
                    project,
                ]
                if selector is not None:
                    command.extend(("--grep", selector))
                try:
                    result = subprocess.run(
                        command,
                        cwd=_PROJECT_ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=300,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    return {
                        "status": "failed",
                        "suite": "browser",
                        "error": "browser_test_timeout",
                        "project": project,
                        "file": str(browser_file),
                        "grep": selector,
                        "projects": project_results,
                    }
                if result.stdout:
                    sys.stdout.write(result.stdout)
                if result.stderr:
                    sys.stderr.write(result.stderr)
                project_result = {
                    "status": "passed" if result.returncode == 0 else "failed",
                    "project": project,
                    "file": str(browser_file),
                    "grep": selector,
                    "exit_code": result.returncode,
                }
                project_results.append(project_result)
                if result.returncode:
                    return {
                        "status": "failed",
                        "suite": "browser",
                        "project": project,
                        "file": str(browser_file),
                        "grep": selector,
                        "projects": project_results,
                    }
    return {
        "status": "passed",
        "suite": "browser",
        "projects": project_results,
        "batch_id": batch["batch_id"],
    }


def _create_browser_batch(
    manifest: DemoManifest, browser_files: tuple[Path, ...]
) -> dict[str, object]:
    batch_id = uuid4()
    action_ids = action_catalog_ids()
    check_id = action_ids[0] if action_ids else "F01-01"
    expected: list[dict[str, object]] = []
    viewports = {
        "desktop": {"width": 1440, "height": 900},
        "tablet": {"width": 1024, "height": 768},
        "mobile": {"width": 390, "height": 844},
    }
    for project, viewport in viewports.items():
        for browser_file in browser_files:
            stable_key = (
                str(browser_file.relative_to(_WEB_ROOT)).replace("\\", "/")
                + "::"
                + project
            )
            expected.append(
                build_entry(
                    batch_id=batch_id,
                    suite="browser",
                    scenario=manifest.scenario,
                    stable_test_key=stable_key,
                    project=project,
                    viewport=viewport,
                    check_id=check_id,
                )
            )
    batch = build_batch(
        suite="browser",
        scenario=manifest.scenario,
        run_id=manifest.run_id,
        workspace_id=manifest.workspace_id,
        implementation_fingerprint=manifest.implementation_fingerprint,
        fingerprint_version=manifest.fingerprint_version,
        code_commit=manifest.code_commit,
        catalog_fingerprint=action_catalog_fingerprint(),
        expected=expected,
        batch_id=batch_id,
    )
    write_batch(
        manifest.run_root / "reports" / "batches" / f"{batch_id}.json",
        batch,
    )
    write_current_batch(manifest.run_root / "reports" / "current-batch.json", batch_id)
    return batch


def _migrate_databases(
    manifest: DemoManifest,
    tokens: DemoTokens,
    inherited: Mapping[str, str],
    pgpassfile: Path | None,
) -> None:
    migrations = (
        ("control-plane", _CONTROL_PLANE_ROOT, _python_for(_CONTROL_PLANE_ROOT)),
        ("knowledge-service", _KNOWLEDGE_ROOT, _python_for(_KNOWLEDGE_ROOT)),
    )
    for name, root, python in migrations:
        environment = component_environment(
            manifest,
            name,
            tokens,
            inherited,
            pgpassfile=pgpassfile,
        )
        completed = _run_logged_process(
            (
                str(python),
                "-m",
                "alembic",
                "-c",
                str(root / "alembic.ini"),
                "upgrade",
                "head",
            ),
            cwd=root,
            environment=environment,
            timeout_seconds=60,
            log_path=manifest.run_root / "logs" / f"migration-{name}.log",
        )
        if completed:
            raise DemoCliError(f"migration_failed:{name}")


def _create_demo_databases(
    manifest: DemoManifest, pgpassfile: Path | None
) -> None:
    log_path = manifest.run_root / "logs" / "database-create.log"
    try:
        with _postgres_authentication(pgpassfile):
            create_demo_databases(manifest)
    except (DemoDatabaseError, OSError, SQLAlchemyError) as exc:
        _write_process_log(
            log_path,
            "",
            f"database_creation_failed:{type(exc).__name__}\n",
        )
        raise DemoCliError("database_creation_failed") from exc
    _write_process_log(log_path, "", "database_creation_completed\n")


def _run_logged_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    log_path: Path,
) -> int:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(environment),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _write_process_log(log_path, exc.stdout, exc.stderr)
        raise DemoCliError(f"process_timeout:{log_path.stem}") from exc
    _write_process_log(log_path, completed.stdout, completed.stderr)
    return completed.returncode


def _require_database_head(database_url: str, config_path: Path) -> None:
    database = Database(database_url)
    try:
        with database.engine.connect() as connection:
            actual = set(
                connection.execute(text("SELECT version_num FROM alembic_version"))
                .scalars()
                .all()
            )
        expected = set(ScriptDirectory.from_config(Config(str(config_path))).get_heads())
    finally:
        database.dispose()
    if not expected or actual != expected:
        raise DemoCliError("database_migration_not_at_head")


def _load_validated_manifest(path: Path) -> DemoManifest:
    if not path.is_absolute():
        raise DemoCliError("manifest_path_must_be_absolute")
    manifest = load_manifest(path)
    validate_manifest(manifest, _PROJECT_ROOT)
    return manifest


def _selected_pgpassfile(environment: Mapping[str, str]) -> Path | None:
    raw_path = environment.get("SIDERA_INTERVIEW_DEMO_PGPASSFILE")
    if raw_path is None:
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise DemoCliError("selected_pgpassfile_invalid")
    return path


@contextmanager
def _postgres_authentication(pgpassfile: Path | None) -> Iterator[None]:
    original = {key: value for key, value in os.environ.items() if key.startswith("PG")}
    for key in original:
        os.environ.pop(key, None)
    if pgpassfile is not None:
        os.environ["PGPASSFILE"] = str(pgpassfile)
    try:
        yield
    finally:
        for key in tuple(os.environ):
            if key.startswith("PG"):
                os.environ.pop(key, None)
        os.environ.update(original)


def _python_for(project_root: Path) -> Path:
    for name in ("python", "python3"):
        candidate = project_root / ".venv" / "bin" / name
        if candidate.is_file():
            return candidate
    raise DemoCliError(f"project_environment_missing:{project_root.name}")


def _test_environment(explicit: Mapping[str, str] | None = None) -> dict[str, str]:
    from .environment import child_environment

    return child_environment(dict(os.environ), dict(explicit or {}))


def _get_json(client: httpx.Client, url: str) -> object:
    response = client.get(url)
    if not 200 <= response.status_code < 300:
        raise DemoCliError(f"local_http_status:{response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise DemoCliError("local_http_response_invalid") from exc


def _write_private_text(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write(value)
        target.flush()
        os.fsync(target.fileno())


def _write_process_log(
    path: Path,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = _decode_output(stdout) + _decode_output(stderr)
    path.write_text(output, encoding="utf-8")
    os.chmod(path, 0o600)


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _print_json(value: Mapping[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


AnyGate = Literal["parallel", "before-artifact", "handoff", "governance"]


if __name__ == "__main__":
    raise SystemExit(main())
