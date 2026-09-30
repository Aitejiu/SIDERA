"""Fixed, read-only collectors used by the real acceptance driver."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Literal

from .observations import GroupRunObservation, LiveObservationError, ensure_group_run


def run_closure_sample_check(
    candidate_checkout: Path,
    *,
    sidera_root: Path,
) -> dict[str, object]:
    """Run the checked-in checker with a fixed argv and no shell."""

    checker = (sidera_root / "scripts" / "check-closure-sample.py").resolve()
    if checker.parent.parent != sidera_root.resolve():
        raise LiveObservationError("checker_path_invalid")
    if not checker.is_file():
        raise LiveObservationError("checker_missing")
    candidate = candidate_checkout.expanduser().resolve()
    if not candidate.is_dir():
        raise LiveObservationError("candidate_checkout_invalid")
    tree_before = _git_hash(candidate)
    command = [sys.executable, str(checker), "--repository", str(candidate)]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    except OSError as exc:
        raise LiveObservationError("checker_unavailable") from exc
    tree_after = _git_hash(candidate)
    checker_hash = hashlib.sha256(checker.read_bytes()).hexdigest()
    tree_unchanged = tree_before == tree_after
    exit_code = completed.returncode
    return {
        "command": command,
        "command_sha256": hashlib.sha256(
            "\0".join(command).encode("utf-8")
        ).hexdigest(),
        "checker_sha256": checker_hash,
        "candidate_tree_hash": tree_before,
        "candidate_tree_hash_before": tree_before,
        "candidate_tree_hash_after": tree_after,
        "candidate_tree_unchanged": tree_unchanged,
        "checker_exit_code": exit_code,
        "exit_code": exit_code,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "status": "PASS" if exit_code == 0 and tree_unchanged else "FAIL",
        "failure_code": (
            None
            if exit_code == 0 and tree_unchanged
            else "candidate_tree_changed_during_checker"
            if not tree_unchanged
            else "checker_failed"
        ),
    }


def observe_group(
    group: Literal["A", "B", "C"],
    manifest: object,
) -> GroupRunObservation:
    return ensure_group_run(group, manifest)


def _git_hash(repository: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LiveObservationError("candidate_tree_hash_unavailable") from exc
    value = completed.stdout.strip()
    if len(value) != 40:
        raise LiveObservationError("candidate_tree_hash_invalid")
    return value


__all__ = ["observe_group", "run_closure_sample_check"]
