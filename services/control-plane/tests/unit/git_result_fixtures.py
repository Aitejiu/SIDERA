from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from sidera_control_plane.adapters.git_results import GitResultStore
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


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


@dataclass(slots=True)
class GitResultCase:
    repository: Path
    store: GitResultStore
    manifest: ExecutionWorkspaceManifest
    input_commit: str

    def write(self, path: str, content: bytes) -> None:
        target = self.repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def commit_file(self, path: str, text: str) -> None:
        self.write(path, text.encode())
        git(self.repository, "add", "-A", "--", path)
        git(self.repository, "commit", "-m", f"change {path}")

    def read_commit(self, commit: str, path: str) -> bytes:
        return git_bytes(self.repository, "show", f"{commit}:{path}")


@pytest.fixture
def git_case(tmp_path: Path) -> GitResultCase:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "SIDERA Test")
    git(repository, "config", "user.email", "test@sidera.local")
    git(repository, "config", "core.filemode", "true")

    (repository / ".gitignore").write_text("ignored.log\nignored-dir/\n")
    (repository / "README.md").write_text("base\n")
    (repository / "src").mkdir()
    (repository / "src" / "keep.py").write_text("keep = True\n")
    (repository / "rename.txt").write_text("rename me\n")
    (repository / "delete.txt").write_text("delete me\n")
    (repository / "mode.sh").write_text("#!/bin/sh\necho base\n")
    (repository / "binary.bin").write_bytes(bytes(range(256)))
    git(repository, "add", "-A")
    git(repository, "commit", "-m", "base")
    input_commit = git(repository, "rev-parse", "HEAD")

    workspace_id = uuid4()
    manifest = ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=workspace_id,
        task_id=uuid4(),
        execution_id=uuid4(),
        checkouts=(
            WorkspaceCheckout(
                repository_ref="repo://fixture",
                base_revision=input_commit,
                branch="main",
                current_head=input_commit,
            ),
        ),
        location_ref=f"git-result-fixture://{workspace_id}",
        retention_seconds=300,
        fencing_token=1,
        created_at=NOW,
    )
    manifest.mark_preparing()
    manifest.mark_ready()
    manifest.mark_active()
    return GitResultCase(
        repository=repository,
        store=GitResultStore({"repo://fixture": repository}),
        manifest=manifest,
        input_commit=input_commit,
    )
