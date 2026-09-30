"""Independent, read-only recovery checks for accepted Git result trees."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class RestoredTreeObservation:
    status: Literal["passed", "failed", "incomplete"]
    restored_tree_hash: str | None
    failure_code: str | None = None


RestoreChecker = Callable[
    [Mapping[str, object], str, str], RestoredTreeObservation
]

_GIT_OID_LENGTH = 40
_COMMAND_TIMEOUT_SECONDS = 30


def restore_accepted_tree(
    manifest: Mapping[str, object],
    result_commit: str,
    expected_tree: str,
) -> RestoredTreeObservation:
    """Fetch and inspect a candidate in a new repository with no shared objects."""

    repository_value = manifest.get("repository")
    if not isinstance(repository_value, str) or not Path(repository_value).is_absolute():
        return RestoredTreeObservation(
            "incomplete", None, "restore_repository_unavailable"
        )
    if not _is_git_oid(result_commit) or not _is_git_oid(expected_tree):
        return RestoredTreeObservation("failed", None, "restore_identity_invalid")

    try:
        source = Path(repository_value).expanduser().resolve(strict=True)
    except OSError:
        return RestoredTreeObservation(
            "incomplete", None, "restore_repository_unavailable"
        )
    if not source.is_dir():
        return RestoredTreeObservation(
            "incomplete", None, "restore_repository_unavailable"
        )

    env = _git_environment()
    try:
        source_root_result = _run_git(
            ("-C", str(source), "rev-parse", "--show-toplevel"),
            cwd=source,
            env=env,
        )
        if source_root_result.returncode != 0:
            return RestoredTreeObservation(
                "incomplete", None, "restore_repository_unavailable"
            )
        source_root = Path(source_root_result.stdout.strip()).resolve(strict=True)
        if source_root != source:
            return RestoredTreeObservation(
                "incomplete", None, "restore_repository_unavailable"
            )

        with tempfile.TemporaryDirectory(prefix="sidera-d2-restore-") as temporary:
            target = Path(temporary) / "repository"
            target.mkdir()
            empty_template = Path(temporary) / "empty-template"
            empty_template.mkdir()
            empty_hooks = Path(temporary) / "empty-hooks"
            empty_hooks.mkdir()
            init = _run_git(
                ("init", "--quiet", f"--template={empty_template}"),
                cwd=target,
                env=env,
            )
            if init.returncode != 0:
                return RestoredTreeObservation(
                    "incomplete", None, "restore_repository_init_failed"
                )
            _configure_isolated_repository(target, env, empty_hooks)
            fetch = _run_git(
                (
                    "-c",
                    "protocol.file.allow=always",
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "--upload-pack=git-upload-pack",
                    str(source),
                    result_commit,
                ),
                cwd=target,
                env=env,
            )
            if fetch.returncode != 0:
                return RestoredTreeObservation(
                    "incomplete", None, "restore_object_unavailable"
                )
            checkout = _run_git(
                ("checkout", "--quiet", "--detach", "FETCH_HEAD"),
                cwd=target,
                env=env,
            )
            if checkout.returncode != 0:
                return RestoredTreeObservation(
                    "incomplete", None, "restore_checkout_unavailable"
                )

            actual_commit = _git_text(target, env, "rev-parse", "HEAD")
            actual_tree = _git_text(target, env, "rev-parse", "HEAD^{tree}")
            if actual_commit != result_commit:
                return RestoredTreeObservation(
                    "failed", actual_tree, "restore_commit_mismatch"
                )
            if actual_tree != expected_tree:
                return RestoredTreeObservation(
                    "failed", actual_tree, "accepted_tree_restore_mismatch"
                )

            git_dir = target / ".git"
            if (git_dir / "commondir").exists() or (
                git_dir / "objects" / "info" / "alternates"
            ).exists():
                return RestoredTreeObservation(
                    "failed", actual_tree, "restore_repository_not_independent"
                )
            diff = _run_git(
                ("diff", "--exit-code", "HEAD", "--"),
                cwd=target,
                env=env,
            )
            status = _run_git(
                ("status", "--porcelain", "--untracked-files=all"),
                cwd=target,
                env=env,
            )
            if diff.returncode != 0 or status.returncode != 0 or status.stdout.strip():
                return RestoredTreeObservation(
                    "failed", actual_tree, "restore_worktree_dirty"
                )
            return RestoredTreeObservation("passed", actual_tree)
    except subprocess.TimeoutExpired:
        return RestoredTreeObservation("incomplete", None, "restore_timeout")
    except (OSError, ValueError):
        return RestoredTreeObservation(
            "incomplete", None, "restore_repository_unavailable"
        )


def _configure_isolated_repository(
    repository: Path,
    env: Mapping[str, str],
    empty_hooks: Path,
) -> None:
    settings = (
        ("core.hooksPath", str(empty_hooks)),
        ("core.attributesFile", os.devnull),
        ("core.fsmonitor", "false"),
        ("credential.helper", ""),
    )
    for key, value in settings:
        result = _run_git(
            ("config", "--local", key, value),
            cwd=repository,
            env=env,
        )
        if result.returncode != 0:
            raise OSError("isolated Git configuration failed")


def _git_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
    )
    return env


def _run_git(
    arguments: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        check=False,
        text=True,
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )


def _git_text(repository: Path, env: Mapping[str, str], *arguments: str) -> str:
    result = _run_git(arguments, cwd=repository, env=env)
    if result.returncode != 0:
        raise OSError("isolated Git query failed")
    return result.stdout.strip()


def _is_git_oid(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _GIT_OID_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )
