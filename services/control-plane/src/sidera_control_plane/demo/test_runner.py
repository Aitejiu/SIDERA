from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from .contracts import DemoManifest
from .database import (
    DemoDatabaseError,
    assert_owned_database,
    create_demo_databases,
    database_url_for,
    validate_database_url,
)
from .environment import child_environment
from .manifest import DemoConfigurationError, load_manifest, validate_manifest

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_CONTROL_PLANE_ROOT = _PROJECT_ROOT / "services" / "control-plane"


class DemoTestIsolationError(RuntimeError):
    """An interview integration test is not pointed at its owned child run."""


def bootstrap_test_command() -> tuple[str, ...]:
    return ("pytest", "tests/integration/test_interview_bootstrap.py", "-q")


def build_bootstrap_environment(manifest: DemoManifest) -> dict[str, str]:
    return build_interview_test_environment(
        manifest,
        inherited=os.environ,
    )


def interview_test_files(
    project_root: Path = _CONTROL_PLANE_ROOT,
) -> tuple[Path, ...]:
    integration_root = project_root / "tests" / "integration"
    return tuple(
        sorted(
            path
            for path in integration_root.glob("test_interview_*.py")
            if path.is_file()
        )
    )


def build_interview_test_environment(
    manifest: DemoManifest,
    *,
    inherited: Mapping[str, str] | None = None,
    pgpassfile: Path | None = None,
) -> dict[str, str]:
    source = dict(os.environ if inherited is None else inherited)
    explicit = {
        "SIDERA_DATABASE_URL": database_url_for(manifest),
        "SIDERA_INTERVIEW_DEMO_TEST_RUN": "1",
        "SIDERA_INTERVIEW_DEMO_MANIFEST": str(
            manifest.run_root / "manifest.json"
        ),
    }
    admin_url = source.get("SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL")
    if admin_url is not None:
        explicit["SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL"] = admin_url
    selected_passfile = pgpassfile
    if selected_passfile is None:
        raw_passfile = source.get("SIDERA_INTERVIEW_DEMO_PGPASSFILE")
        selected_passfile = Path(raw_passfile).expanduser() if raw_passfile else None
    if selected_passfile is not None:
        selected_passfile = selected_passfile.expanduser()
        if not selected_passfile.is_absolute() or not selected_passfile.is_file():
            raise DemoTestIsolationError("selected_pgpassfile_invalid")
        explicit["PGPASSFILE"] = str(selected_passfile)
        explicit["SIDERA_INTERVIEW_DEMO_PGPASSFILE"] = str(selected_passfile)
    return child_environment(source, explicit)


def validate_interview_test_context(
    environment: Mapping[str, str],
) -> DemoManifest:
    if environment.get("SIDERA_INTERVIEW_DEMO_TEST_RUN") != "1":
        raise DemoTestIsolationError("child_run_required")
    raw_manifest_path = environment.get("SIDERA_INTERVIEW_DEMO_MANIFEST")
    if raw_manifest_path is None:
        raise DemoTestIsolationError("manifest_required")
    manifest_path = Path(raw_manifest_path).expanduser()
    if not manifest_path.is_absolute():
        raise DemoTestIsolationError("manifest_must_be_absolute")
    try:
        manifest = load_manifest(manifest_path)
        validate_manifest(manifest, _PROJECT_ROOT)
        database_url = environment["SIDERA_DATABASE_URL"]
        target = validate_database_url(manifest, database_url)
        if target.database_name != manifest.database_name:
            raise DemoTestIsolationError("database_target_mismatch")
    except DemoTestIsolationError:
        raise
    except (KeyError, DemoDatabaseError, DemoConfigurationError, ValueError) as exc:
        raise DemoTestIsolationError("database_target_mismatch") from exc

    explicit_passfile = environment.get("SIDERA_INTERVIEW_DEMO_PGPASSFILE")
    active_passfile = environment.get("PGPASSFILE")
    if active_passfile != explicit_passfile:
        raise DemoTestIsolationError("pgpassfile_selection_mismatch")
    if explicit_passfile is not None:
        passfile = Path(explicit_passfile).expanduser()
        if not passfile.is_absolute() or not passfile.is_file():
            raise DemoTestIsolationError("selected_pgpassfile_invalid")
    try:
        assert_owned_database(manifest, manifest.database_name)
    except Exception as exc:
        raise DemoTestIsolationError("database_ownership_check_failed") from exc
    return manifest


def run_interview_tests(
    environment: Mapping[str, str],
    *,
    project_root: Path = _CONTROL_PLANE_ROOT,
    python_executable: Path | None = None,
    timeout_seconds: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    if not 0 < timeout_seconds <= 60:
        raise ValueError("interview_test_timeout_must_not_exceed_60_seconds")
    files = interview_test_files(project_root)
    if not files:
        raise FileNotFoundError("no_interview_integration_tests")
    interpreter = python_executable or Path(sys.executable)
    command = [
        str(interpreter),
        "-m",
        "pytest",
        "-q",
        *(str(path.relative_to(project_root)) for path in files),
    ]
    return subprocess.run(
        command,
        cwd=project_root,
        env=dict(environment),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )


def run_bootstrap_tests(manifest: DemoManifest) -> int:
    create_demo_databases(manifest)
    completed = subprocess.run(
        [sys.executable, "-m", *bootstrap_test_command()],
        cwd=Path(__file__).resolve().parents[3],
        env=build_bootstrap_environment(manifest),
        check=False,
    )
    return completed.returncode
