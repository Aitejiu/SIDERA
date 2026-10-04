from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from sidera_control_plane.adapters.git_results import GitResultStore
from sidera_control_plane.application.result_ports import ResultError

if TYPE_CHECKING:
    from tests.unit.git_result_fixtures import GitResultCase

pytest_plugins = ("tests.unit.git_result_fixtures",)


def git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def git_bytes(cwd: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
    )
    return result.stdout


def git_exists(cwd: Path, *args: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _capture(case: GitResultCase, *, operation_id=None, writable_paths=(".",)):
    return case.store.capture(
        manifest=case.manifest,
        input_commit=case.input_commit,
        operation_id=operation_id or uuid4(),
        writable_paths=writable_paths,
    )


def test_committed_work_is_in_captured_result(git_case: GitResultCase) -> None:
    git_case.commit_file("login.py", "ALLOW_EXPIRED = False\n")

    captured = _capture(git_case)

    assert git_case.read_commit(captured.result_commit, "login.py") == (
        b"ALLOW_EXPIRED = False\n"
    )
    assert captured.result_tree != captured.input_tree
    assert git(git_case.repository, "show", "-s", "--format=%P", captured.result_commit) == (
        captured.input_commit
    )
    assert captured.observed_agent_head == git(
        git_case.repository, "rev-parse", "HEAD"
    )


def test_capture_uses_manifest_worktree_instead_of_repository_head(
    git_case: GitResultCase, tmp_path: Path
) -> None:
    worker_path = tmp_path / "worker"
    branch = "sidera/result-worker"
    git(
        git_case.repository,
        "worktree",
        "add",
        "-b",
        branch,
        str(worker_path),
        git_case.input_commit,
    )
    checkout = git_case.manifest.checkouts[0]
    git_case.manifest.checkouts = (replace(checkout, branch=branch),)
    git_case.write("ignored.log", b"source remains untouched\n")
    (worker_path / "worker.py").write_text("worker = True\n")

    captured = _capture(git_case)

    assert git_case.read_commit(captured.result_commit, "worker.py") == (
        b"worker = True\n"
    )
    assert not (git_case.repository / "worker.py").exists()


def test_capture_includes_staged_unstaged_and_untracked_without_touching_agent_index(
    git_case: GitResultCase,
) -> None:
    branch_before = git(git_case.repository, "branch", "--show-current")
    git_case.write("README.md", b"unstaged\n")
    git_case.write("staged.py", b"staged = True\n")
    git(git_case.repository, "add", "staged.py")
    index_before = git(git_case.repository, "write-tree")
    git_case.write("new.py", b"untracked = True\n")

    captured = _capture(git_case)

    assert git_case.read_commit(captured.result_commit, "README.md") == b"unstaged\n"
    assert git_case.read_commit(captured.result_commit, "staged.py") == b"staged = True\n"
    assert git_case.read_commit(captured.result_commit, "new.py") == b"untracked = True\n"
    assert git(git_case.repository, "write-tree") == index_before
    assert git(git_case.repository, "branch", "--show-current") == branch_before


def test_capture_preserves_delete_rename_mode_and_binary_changes(
    git_case: GitResultCase,
) -> None:
    (git_case.repository / "delete.txt").unlink()
    (git_case.repository / "rename.txt").rename(git_case.repository / "renamed.txt")
    os.chmod(git_case.repository / "mode.sh", 0o755)
    git_case.write("binary.bin", bytes(reversed(range(256))))

    captured = _capture(git_case)

    assert not git_exists(
        git_case.repository,
        "cat-file",
        "-e",
        f"{captured.result_commit}:delete.txt",
    )
    assert git_case.read_commit(captured.result_commit, "renamed.txt") == b"rename me\n"
    assert git_case.read_commit(captured.result_commit, "binary.bin") == bytes(
        reversed(range(256))
    )
    mode = git(git_case.repository, "ls-tree", captured.result_commit, "mode.sh").split()[0]
    assert mode == "100755"


def test_ignored_files_are_not_promoted_to_result(git_case: GitResultCase) -> None:
    git_case.write("ignored.log", b"must stay ignored\n")
    git_case.write("ignored-dir/child.txt", b"must stay ignored\n")

    captured = _capture(git_case)

    for path in ("ignored.log", "ignored-dir/child.txt"):
        assert not git_exists(
            git_case.repository,
            "cat-file",
            "-e",
            f"{captured.result_commit}:{path}",
        )


def test_scope_violation_is_rejected_before_result_ref_is_written(
    git_case: GitResultCase,
) -> None:
    operation_id = uuid4()
    git_case.write("outside.txt", b"outside scope\n")

    with pytest.raises(ResultError, match="result_scope_violation"):
        _capture(git_case, operation_id=operation_id, writable_paths=("src",))

    assert git(
        git_case.repository,
        "show-ref",
        "--verify",
        f"refs/sidera/results/{operation_id}",
        check=False,
    ) == ""


def test_submodule_result_is_explicitly_unsupported(
    git_case: GitResultCase, tmp_path: Path
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    git(nested, "init", "--initial-branch=main")
    git(nested, "config", "user.name", "Nested")
    git(nested, "config", "user.email", "nested@sidera.local")
    (nested / "module.py").write_text("module = True\n")
    git(nested, "add", "module.py")
    git(nested, "commit", "-m", "nested")
    nested_commit = git(nested, "rev-parse", "HEAD")
    git(
        git_case.repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{nested_commit},vendor/nested",
    )
    git(git_case.repository, "commit", "-m", "add nested repository")

    with pytest.raises(ResultError, match="result_submodule_unsupported"):
        _capture(git_case)


def test_lfs_result_is_explicitly_unsupported(git_case: GitResultCase) -> None:
    (git_case.repository / ".gitattributes").write_text(
        "lfs.bin filter=lfs diff=lfs merge=lfs -text\n"
    )
    git_case.write("lfs.bin", b"version https://git-lfs.github.com/spec/v1\n")
    git(git_case.repository, "add", ".gitattributes", "lfs.bin")
    git(git_case.repository, "commit", "-m", "add lfs pointer")

    with pytest.raises(ResultError, match="result_lfs_unsupported"):
        _capture(git_case)


def test_capture_replay_returns_original_result_and_inspect_does_not_read_worktree(
    git_case: GitResultCase,
) -> None:
    operation_id = uuid4()
    git_case.commit_file("first.py", "first = True\n")
    first = _capture(git_case, operation_id=operation_id)
    git_case.commit_file("second.py", "second = True\n")

    replay = _capture(git_case, operation_id=operation_id)
    reopened = GitResultStore({"repo://fixture": git_case.repository}).inspect_capture(
        operation_id
    )

    assert replay == first
    assert reopened == first
    assert reopened is not None
    assert not git_exists(
        git_case.repository,
        "cat-file",
        "-e",
        f"{reopened.result_commit}:second.py",
    )


def test_cold_capture_enumerates_saved_refs_and_replays_original_after_worktree_removed(
    git_case, tmp_path
):
    worker = tmp_path / "removed-worker"
    branch = "sidera/cold-result-worker"
    git(
        git_case.repository,
        "worktree",
        "add",
        "-b",
        branch,
        str(worker),
        git_case.input_commit,
    )
    git_case.manifest.checkouts = (
        replace(git_case.manifest.checkouts[0], branch=branch),
    )
    (worker / "original.py").write_text("original = True\n")
    first = _capture(git_case)
    git(git_case.repository, "worktree", "remove", "--force", str(worker))
    assert not worker.exists()
    calls = []

    def refs():
        calls.append("refs")
        return ("repo://fixture", "repo://fixture")

    def resolve(ref):
        assert ref == "repo://fixture"
        calls.append("resolve")
        return git_case.repository

    cold = GitResultStore(resolve, repository_refs=refs)
    assert not calls
    assert cold.inspect_capture(first.operation_id) == first
    assert (
        cold.capture(
            manifest=git_case.manifest,
            input_commit=git_case.input_commit,
            operation_id=first.operation_id,
            writable_paths=(".",),
        )
        == first
    )
    assert calls == ["refs", "resolve", "refs"]
    assert (
        git_case.read_commit(first.result_commit, "original.py") == b"original = True\n"
    )


def test_capture_persists_result_and_metadata_refs(git_case: GitResultCase) -> None:
    captured = _capture(git_case)
    metadata_ref = captured.receipt_ref
    metadata = json.loads(
        git_bytes(git_case.repository, "cat-file", "-p", metadata_ref)
    )

    assert git(
        git_case.repository,
        "rev-parse",
        f"refs/sidera/results/{captured.operation_id}",
    ) == captured.result_commit
    assert metadata["operation_id"] == str(captured.operation_id)
    assert metadata["result_commit"] == captured.result_commit
    assert metadata["observed_agent_head"] == captured.observed_agent_head
    assert captured.receipt_ref == metadata_ref


def test_export_patch_is_replayable_and_empty_changes_are_valid(
    git_case: GitResultCase, tmp_path: Path
) -> None:
    git_case.commit_file("restore.py", "value = 1\n")
    captured = _capture(git_case)
    patch = git_case.store.export_patch(captured)
    restored = tmp_path / "restored"
    subprocess.run(
        ["git", "clone", str(git_case.repository), str(restored)],
        check=True,
        capture_output=True,
        text=True,
    )
    git(restored, "checkout", "--detach", git_case.input_commit)
    (restored / "restore.py").unlink(missing_ok=True)
    restored_patch = tmp_path / "result.patch"
    restored_patch.write_bytes(patch)
    git(restored, "apply", "--binary", str(restored_patch))
    git(restored, "add", "-A")
    assert git(restored, "write-tree") == captured.result_tree

    clean_case = GitResultStore({"repo://fixture": git_case.repository})
    clean = clean_case.capture(
        manifest=git_case.manifest,
        input_commit=git(git_case.repository, "rev-parse", "HEAD"),
        operation_id=uuid4(),
        writable_paths=(".",),
    )
    assert clean_case.export_patch(clean) == b""


def test_export_patch_rejects_oversized_output(git_case: GitResultCase) -> None:
    git_case.write("large.bin", b"x" * (10 * 1024 * 1024 + 1))
    captured = _capture(git_case)

    with pytest.raises(ResultError, match="result_export_too_large"):
        git_case.store.export_patch(captured)
