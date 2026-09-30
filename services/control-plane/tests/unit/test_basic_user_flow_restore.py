from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from tests.e2e.basic_acceptance import user_flow_restore
from tests.e2e.basic_acceptance.user_flow_restore import restore_accepted_tree


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str, str]:
    repository = tmp_path / "source-repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Sidera Test")
    _git(repository, "config", "user.email", "sidera-test@example.invalid")
    tracked = repository / "tracked.txt"
    tracked.write_text("old accepted content\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "--quiet", "-m", "accepted")
    accepted_commit = _git(repository, "rev-parse", "HEAD")
    accepted_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    tracked.write_text("later content\n", encoding="utf-8")
    _git(repository, "commit", "--quiet", "-am", "later")
    current_commit = _git(repository, "rev-parse", "HEAD")
    return repository, accepted_commit, accepted_tree, current_commit


def _source_state(repository: Path) -> tuple[object, ...]:
    index_path = Path(_git(repository, "rev-parse", "--git-path", "index"))
    if not index_path.is_absolute():
        index_path = repository / index_path
    worktree = tuple(
        sorted(
            (path.relative_to(repository).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in repository.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(repository).parts
        )
    )
    return (
        _git(repository, "rev-parse", "HEAD"),
        _git(repository, "for-each-ref", "--format=%(refname)=%(objectname)"),
        index_path.read_bytes(),
        _git(repository, "status", "--porcelain", "--untracked-files=all"),
        worktree,
    )


def test_restore_recovers_old_accepted_commit_without_changing_source(
    tmp_path: Path,
) -> None:
    repository, accepted_commit, accepted_tree, current_commit = _repository(tmp_path)
    before = _source_state(repository)

    restored = restore_accepted_tree(
        {"repository": str(repository)}, accepted_commit, accepted_tree
    )

    assert restored.status == "passed"
    assert restored.restored_tree_hash == accepted_tree
    assert current_commit != accepted_commit
    assert _source_state(repository) == before


def test_restore_reports_missing_source_object_as_incomplete(tmp_path: Path) -> None:
    repository, _accepted_commit, accepted_tree, _current_commit = _repository(tmp_path)

    restored = restore_accepted_tree(
        {"repository": str(repository)}, "f" * 40, accepted_tree
    )

    assert restored.status == "incomplete"
    assert restored.failure_code == "restore_object_unavailable"


def test_restore_reports_a_different_tree_as_failed(tmp_path: Path) -> None:
    repository, accepted_commit, accepted_tree, _current_commit = _repository(tmp_path)
    wrong_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    assert wrong_tree != accepted_tree

    restored = restore_accepted_tree(
        {"repository": str(repository)}, accepted_commit, wrong_tree
    )

    assert restored.status == "failed"
    assert restored.restored_tree_hash == accepted_tree
    assert restored.failure_code == "accepted_tree_restore_mismatch"


def test_restore_leaves_dirty_source_worktree_unchanged(tmp_path: Path) -> None:
    repository, accepted_commit, accepted_tree, _current_commit = _repository(tmp_path)
    (repository / "tracked.txt").write_text("user edit\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("keep me\n", encoding="utf-8")
    before = _source_state(repository)

    restored = restore_accepted_tree(
        {"repository": str(repository)}, accepted_commit, accepted_tree
    )

    assert restored.status == "passed"
    assert _source_state(repository) == before


def test_restore_classifies_timeout_as_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, accepted_commit, accepted_tree, _current_commit = _repository(tmp_path)

    def timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("git", 30)

    monkeypatch.setattr(user_flow_restore, "_run_git", timeout)
    restored = restore_accepted_tree(
        {"repository": str(repository)}, accepted_commit, accepted_tree
    )

    assert restored.status == "incomplete"
    assert restored.failure_code == "restore_timeout"
