from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from sidera_control_plane.adapters.git_results import GitResultStore
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.result_acceptance import (
    build_acceptance_basis_hash,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.demo.contracts import DemoManifest
from sidera_control_plane.domain.code_results import (
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.result_integration import (
    CheckSpecV1,
    IntegrationSpecV1,
)
from sidera_control_plane.domain.result_verification import (
    TaskAcceptanceDecisionV1,
    VerificationResultV1,
    VerificationStatus,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_GIT_OID_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class DemoResultError(RuntimeError):
    """A demo candidate cannot be restored without verified ownership."""


def demo_check_argv() -> tuple[str, ...]:
    return ("python3", "-m", "unittest", "discover", "-s", "tests", "-v")


def demo_check_spec() -> CheckSpecV1:
    return CheckSpecV1.from_mapping(
        {
            "key": "unit",
            "version": 1,
            "argv": list(demo_check_argv()),
            "cwd": ".",
            "environment_ref": "env://interview-demo",
            "prepare_argvs": [],
            "timeout_seconds": 60,
            "success_exit_codes": [0],
            "report_paths": [],
            "required": True,
        }
    )


def closure_check_argv() -> tuple[str, ...]:
    return (
        "python3",
        str(_PROJECT_ROOT / "scripts" / "check-closure-sample.py"),
        "--repository-cwd",
    )


def closure_check_spec() -> CheckSpecV1:
    return CheckSpecV1.from_mapping(
        {
            "key": "closure",
            "version": 1,
            "argv": list(closure_check_argv()),
            "cwd": ".",
            "environment_ref": "env://interview-demo",
            "prepare_argvs": [],
            "timeout_seconds": 60,
            "success_exit_codes": [0],
            "report_paths": [],
            "required": True,
        }
    )


def summary_json_check_argv() -> tuple[str, ...]:
    return ("python3", "-m", "json.tool", "out/summary.json")


def summary_json_check_spec() -> CheckSpecV1:
    return CheckSpecV1.from_mapping(
        {
            "key": "summary-json",
            "version": 1,
            "argv": list(summary_json_check_argv()),
            "cwd": ".",
            "environment_ref": "env://interview-demo",
            "prepare_argvs": [],
            "timeout_seconds": 60,
            "success_exit_codes": [0],
            "report_paths": [],
            "required": True,
        }
    )


def patch_integrity_check_argv(base_commit: str) -> tuple[str, ...]:
    if not isinstance(base_commit, str) or _GIT_OID_PATTERN.fullmatch(base_commit) is None:
        raise ValueError("base_commit must be a lowercase Git object ID")
    return ("git", "diff", "--check", base_commit, "HEAD")


def patch_integrity_check_spec(base_commit: str) -> CheckSpecV1:
    return CheckSpecV1.from_mapping(
        {
            "key": "patch-integrity",
            "version": 1,
            "argv": list(patch_integrity_check_argv(base_commit)),
            "cwd": ".",
            "environment_ref": "env://interview-demo",
            "prepare_argvs": [],
            "timeout_seconds": 60,
            "success_exit_codes": [0],
            "report_paths": [],
            "required": True,
        }
    )


def restore_demo_candidate(
    manifest: DemoManifest,
    candidate_id: UUID,
    destination: Path,
) -> str:
    """Restore one verified, accepted candidate into an empty run-owned worktree."""

    if not isinstance(candidate_id, UUID):
        raise TypeError("candidate_id must be a UUID")
    destination = _validate_restore_destination(manifest, destination)

    settings = Settings()  # pyright: ignore[reportCallIssue]
    database = Database(settings.database_url)
    try:
        factory: Callable[[], ControlPlaneUnitOfWork] = lambda: SqlAlchemyUnitOfWork(
            database,
            projection_change_retention_seconds=settings.projection_change_retention_seconds,
        )
        candidate, snapshot, integration_spec = _load_accepted_candidate(
            factory,
            manifest,
            candidate_id,
        )
    finally:
        database.dispose()

    store = GitResultStore(
        {snapshot.repository_ref: manifest.repository},
        git_executable=settings.git_executable,
    )
    captured = store.inspect_capture(snapshot.capture_operation_id)
    if captured is None:
        raise DemoResultError("candidate_capture_missing")
    if (
        captured.repository_ref != snapshot.repository_ref
        or captured.input_commit != snapshot.input_commit
        or captured.input_tree != snapshot.input_tree
        or captured.result_commit != snapshot.result_commit
        or captured.result_tree != snapshot.result_tree
        or snapshot.repository_ref != integration_spec.repository_ref
        or candidate.base_commit != manifest.base_commit
    ):
        raise DemoResultError("candidate_repository_identity_mismatch")

    repository = manifest.repository.expanduser().resolve(strict=True)
    branches_before = _git(
        repository,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/heads",
        git_executable=settings.git_executable,
    )
    patch = store.export_patch(captured)
    if not patch:
        raise DemoResultError("candidate_result_empty")
    _git(
        repository,
        "worktree",
        "add",
        "--detach",
        str(destination),
        snapshot.input_commit,
        git_executable=settings.git_executable,
    )
    _git_input(
        destination,
        ("apply", "--check", "--binary", "-"),
        patch,
        git_executable=settings.git_executable,
    )
    _git_input(
        destination,
        ("apply", "--binary", "-"),
        patch,
        git_executable=settings.git_executable,
    )
    _git(destination, "add", "-A", git_executable=settings.git_executable)
    restored_tree = (
        _git(
            destination,
            "write-tree",
            git_executable=settings.git_executable,
        )
        .decode("ascii")
        .strip()
    )
    if restored_tree != snapshot.result_tree:
        raise DemoResultError("candidate_restore_tree_mismatch")

    try:
        completed = subprocess.run(
            demo_check_argv(),
            cwd=destination,
            env=_check_environment(),
            capture_output=True,
            check=False,
            timeout=60,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise DemoResultError("candidate_restore_check_timeout") from exc
    if completed.returncode != 0:
        raise DemoResultError("candidate_restore_check_failed")
    branches_after = _git(
        repository,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/heads",
        git_executable=settings.git_executable,
    )
    if branches_before != branches_after:
        raise DemoResultError("target_branch_changed_during_restore")
    return restored_tree


def _load_accepted_candidate(
    uow_factory: Callable[[], ControlPlaneUnitOfWork],
    manifest: DemoManifest,
    candidate_id: UUID,
) -> tuple[IntegrationCandidateV1, CodeResultSnapshotV1, IntegrationSpecV1]:
    with uow_factory() as uow:
        candidate = uow.results.get_record(
            "candidate", candidate_id, manifest.workspace_id
        )
        if not isinstance(candidate, IntegrationCandidateV1):
            raise DemoResultError("candidate_not_found")
        head = uow.results.get_head(manifest.workspace_id, candidate.root_task_id)
        if (
            head is None
            or head.current_candidate_id != candidate.id
            or head.current_acceptance_id is None
        ):
            raise DemoResultError("candidate_not_accepted")
        acceptance = uow.results.get_record(
            "acceptance", head.current_acceptance_id, manifest.workspace_id
        )
        if (
            not isinstance(acceptance, TaskAcceptanceDecisionV1)
            or acceptance.candidate_id != candidate.id
            or acceptance.candidate_hash != candidate.content_hash
            or acceptance.result_version >= head.result_version
        ):
            raise DemoResultError("candidate_acceptance_identity_mismatch")

        plan_head = uow.plans.get_head(candidate.root_task_id, manifest.workspace_id)
        if (
            plan_head is None
            or plan_head.active_snapshot_id != candidate.plan_snapshot_id
        ):
            raise DemoResultError("candidate_plan_identity_mismatch")
        plan = uow.plans.get(candidate.plan_snapshot_id, manifest.workspace_id)
        if plan is None or not isinstance(plan.integration_spec, IntegrationSpecV1):
            raise DemoResultError("candidate_integration_spec_missing")
        integration_spec = plan.integration_spec

        snapshot = uow.results.get_record(
            "code_snapshot", candidate.code_result_id, manifest.workspace_id
        )
        if not isinstance(snapshot, CodeResultSnapshotV1):
            raise DemoResultError("candidate_code_snapshot_missing")
        if (
            candidate.workspace_id != manifest.workspace_id
            or candidate.root_task_id != head.root_task_id
            or candidate.plan_snapshot_id != plan.id
            or candidate.plan_content_hash != plan_snapshot_content_hash(plan)
            or candidate.content_hash != candidate.compute_content_hash()
            or snapshot.workspace_id != candidate.workspace_id
            or snapshot.root_task_id != candidate.root_task_id
            or snapshot.execution_id != candidate.integration_execution_id
            or snapshot.id != candidate.code_result_id
        ):
            raise DemoResultError("candidate_identity_mismatch")

        verification_results = uow.results.list_verification_results(
            candidate.id, manifest.workspace_id
        )
        if acceptance.acceptance_basis_hash != build_acceptance_basis_hash(
            candidate,
            verification_results,
            acceptance.criteria,
            result_version=acceptance.result_version,
            integration_spec=integration_spec,
        ):
            raise DemoResultError("candidate_acceptance_identity_mismatch")
        latest: dict[str, VerificationResultV1] = {}
        for result in verification_results:
            previous = latest.get(result.check_key)
            if previous is None or result.attempt > previous.attempt:
                latest[result.check_key] = result
        required_checks = tuple(
            check for check in integration_spec.checks if check.required
        )
        if not required_checks:
            raise DemoResultError("candidate_required_check_missing")
        for check in required_checks:
            result = latest.get(check.key)
            if (
                result is None
                or result.check_version != check.version
                or result.status is not VerificationStatus.PASSED
                or result.actual_commit != snapshot.result_commit
                or result.actual_tree != snapshot.result_tree
            ):
                raise DemoResultError("candidate_verification_incomplete")
    return candidate, snapshot, integration_spec


def _validate_restore_destination(
    manifest: DemoManifest,
    destination: Path,
) -> Path:
    raw_run_root = manifest.run_root.expanduser()
    if raw_run_root.is_symlink():
        raise DemoResultError("restore_run_identity_symlink")
    try:
        run_root = raw_run_root.resolve(strict=True)
    except OSError as exc:
        raise DemoResultError("restore_run_identity_unavailable") from exc
    if not run_root.is_dir():
        raise DemoResultError("restore_run_identity_unavailable")

    raw_destination = destination.expanduser()
    if not raw_destination.is_absolute():
        raise DemoResultError("restore_destination_must_be_absolute")
    if raw_destination.is_symlink():
        raise DemoResultError("restore_destination_symlink")
    try:
        lexical_destination = Path(os.path.abspath(raw_destination))
        relative = lexical_destination.relative_to(run_root)
    except (OSError, ValueError) as exc:
        raise DemoResultError("restore_destination_outside_run") from exc
    if not relative.parts:
        raise DemoResultError("restore_destination_outside_run")
    current = run_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise DemoResultError("restore_destination_symlink")
    try:
        resolved = lexical_destination.resolve(strict=True)
    except OSError as exc:
        raise DemoResultError("restore_destination_unavailable") from exc
    if resolved != lexical_destination or not resolved.is_dir():
        raise DemoResultError("restore_destination_not_directory")
    try:
        next(resolved.iterdir())
    except StopIteration:
        pass
    else:
        raise DemoResultError("restore_destination_not_empty")

    raw_repository = manifest.repository.expanduser()
    if raw_repository.is_symlink():
        raise DemoResultError("restore_run_identity_symlink")
    try:
        repository = raw_repository.resolve(strict=True)
    except OSError as exc:
        raise DemoResultError("restore_run_identity_unavailable") from exc
    if not repository.is_dir():
        raise DemoResultError("restore_run_identity_unavailable")
    if (
        resolved == repository
        or run_root == repository
        or run_root in repository.parents
        or repository in run_root.parents
    ):
        raise DemoResultError("restore_run_repository_overlap")
    return resolved


def _check_environment() -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    environment.setdefault("PATH", os.defpath)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _git(
    repository: Path,
    *arguments: str,
    git_executable: str = "git",
) -> bytes:
    return _run_git(
        ("-C", str(repository), *arguments),
        git_executable=git_executable,
    )


def _git_input(
    repository: Path,
    arguments: tuple[str, ...],
    content: bytes,
    *,
    git_executable: str = "git",
) -> None:
    _run_git(
        ("-C", str(repository), *arguments),
        input=content,
        git_executable=git_executable,
    )


def _run_git(
    arguments: tuple[str, ...],
    *,
    input: bytes | None = None,
    git_executable: str = "git",
) -> bytes:
    try:
        completed = subprocess.run(
            (git_executable, *arguments),
            input=input,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DemoResultError("candidate_restore_git_unavailable") from exc
    if completed.returncode != 0:
        raise DemoResultError("candidate_restore_git_failed")
    return completed.stdout


__all__ = [
    "DemoResultError",
    "demo_check_argv",
    "demo_check_spec",
    "patch_integrity_check_argv",
    "patch_integrity_check_spec",
    "restore_demo_candidate",
    "summary_json_check_argv",
    "summary_json_check_spec",
]
