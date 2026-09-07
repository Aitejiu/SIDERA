from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.adapters.git_results import GitResultStore
from sidera_control_plane.domain.code_results import (
    CodeInputManifestV1,
    CodeInputSourceV1,
)
from sidera_control_plane.domain.result_operations import (
    ResultApplyOperationV1,
    ResultOperationStatus,
)
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


@dataclass(slots=True)
class GitApplyCase:
    repository: Path
    store: GitResultStore
    operation: ResultApplyOperationV1
    base_commit: str
    accepted_commit: str

    def target_commit(self) -> str:
        return git(self.repository, "rev-parse", self.operation.target_ref)

    def advance_target_after_acceptance(self) -> str:
        git(self.repository, "switch", "target")
        self.write("after-acceptance.txt", "normal follow-up\n")
        git(self.repository, "add", "after-acceptance.txt")
        git(self.repository, "commit", "-m", "follow-up target change")
        git(self.repository, "switch", "main")
        return self.target_commit()

    def write(self, path: str, content: str) -> None:
        target = self.repository / path
        target.write_text(content)

    def checked_out_target(self) -> Path:
        worktree = self.repository.parent / "target-worktree"
        git(self.repository, "worktree", "add", str(worktree), "target")
        return worktree

    def operation_with_unrelated_commit(self) -> ResultApplyOperationV1:
        tree = git(self.repository, "rev-parse", f"{self.base_commit}^{{tree}}")
        result = subprocess.run(
            ["git", "-C", str(self.repository), "commit-tree", tree, "-m", "unrelated"],
            check=True,
            capture_output=True,
            text=True,
        )
        unrelated = result.stdout.strip()
        git(self.repository, "update-ref", "refs/heads/unrelated", unrelated)
        return replace(self.operation, accepted_commit=unrelated)

    def set_target_to_accepted_without_receipt(self) -> None:
        git(
            self.repository,
            "update-ref",
            self.operation.target_ref,
            self.accepted_commit,
            self.base_commit,
        )

    def corrupt_receipt(self) -> None:
        result = subprocess.run(
            ["git", "-C", str(self.repository), "hash-object", "-w", "--stdin"],
            check=True,
            capture_output=True,
            input=b"{\"corrupt\":true}\n",
        )
        blob = result.stdout.decode().strip()
        git(
            self.repository,
            "update-ref",
            f"refs/sidera/applications/{self.operation.id}",
            blob,
        )


@dataclass(slots=True)
class CodeInputMaterializationCase:
    repository: Path
    store: GitResultStore
    base_commit: str
    workspace_manifest: ExecutionWorkspaceManifest
    workspace_path: Path
    sources: dict[str, CodeInputSourceV1]
    operation_id: UUID

    def manifest_for(
        self,
        order: tuple[str, ...],
        *,
        repository_ref: str = "repo://fixture",
        sources: tuple[CodeInputSourceV1, ...] | None = None,
    ) -> CodeInputManifestV1:
        selected = sources or tuple(self.sources[key] for key in order)
        content = {
            "schema_version": 1,
            "workspace_id": str(self.workspace_manifest.workspace_id),
            "root_task_id": str(self.workspace_manifest.task_id),
            "request_id": "00000000-0000-0000-0000-000000000001",
            "execution_id": str(self.workspace_manifest.execution_id),
            "selection_id": "00000000-0000-0000-0000-000000000002",
            "selection_hash": hashlib.sha256(b"selection").hexdigest(),
            "repository_ref": repository_ref,
            "base_commit": self.base_commit,
            "sources": [item.to_mapping() for item in selected],
        }
        input_hash = hashlib.sha256(
            json.dumps(
                content,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        draft = CodeInputManifestV1(
            schema_version=1,
            workspace_id=self.workspace_manifest.workspace_id,
            root_task_id=self.workspace_manifest.task_id,
            request_id=UUID("00000000-0000-0000-0000-000000000001"),
            execution_id=self.workspace_manifest.execution_id,
            selection_id=UUID("00000000-0000-0000-0000-000000000002"),
            selection_hash=hashlib.sha256(b"selection").hexdigest(),
            repository_ref=repository_ref,
            base_commit=self.base_commit,
            sources=selected,
            input_hash=input_hash,
        )
        return draft

    def materialize_order(self, order: tuple[str, ...]):
        manifest = self.manifest_for(order)
        return self.store.materialize(
            workspace_manifest=self.workspace_manifest,
            code_inputs=manifest,
            operation_id=self.operation_id,
        )

    def read(self, path: str) -> bytes:
        return (self.workspace_path / path).read_bytes()

    def add_worktree(self, path: Path, branch: str, revision: str) -> None:
        git(
            self.repository,
            "worktree",
            "add",
            "-b",
            branch,
            str(path),
            revision,
        )


def _active_manifest(
    *,
    repository_ref: str,
    workspace_id,
    task_id,
    execution_id,
    base_commit: str,
    branch: str,
    location_ref: str,
) -> ExecutionWorkspaceManifest:
    manifest = ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=workspace_id,
        task_id=task_id,
        execution_id=execution_id,
        checkouts=(
            WorkspaceCheckout(
                repository_ref=repository_ref,
                base_revision=base_commit,
                branch=branch,
                current_head=base_commit,
            ),
        ),
        location_ref=location_ref,
        retention_seconds=300,
        fencing_token=1,
        created_at=NOW,
    )
    manifest.mark_preparing()
    manifest.mark_ready()
    manifest.mark_active()
    return manifest


@pytest.fixture
def code_input_case(tmp_path: Path) -> CodeInputMaterializationCase:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "SIDERA Test")
    git(repository, "config", "user.email", "test@sidera.local")
    (repository / "README.md").write_text("base\n")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "base")
    base_commit = git(repository, "rev-parse", "HEAD")
    store = GitResultStore({"repo://fixture": repository})
    workspace_id = uuid4()
    root_task_id = uuid4()
    integration_execution_id = uuid4()
    integration_path = tmp_path / "integration"
    git(
        repository,
        "worktree",
        "add",
        "-b",
        "integration",
        str(integration_path),
        base_commit,
    )
    integration_manifest = _active_manifest(
        repository_ref="repo://fixture",
        workspace_id=workspace_id,
        task_id=root_task_id,
        execution_id=integration_execution_id,
        base_commit=base_commit,
        branch="integration",
        location_ref=f"git-result-fixture://{workspace_id}/integration",
    )

    def capture_source(
        label: str,
        *,
        input_commit: str,
        path: str,
        content: bytes,
    ) -> CodeInputSourceV1:
        source_path = tmp_path / f"source-{label.lower()}"
        branch = f"source-{label.lower()}"
        git(
            repository,
            "worktree",
            "add",
            "-b",
            branch,
            str(source_path),
            input_commit,
        )
        target = source_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        source_manifest = _active_manifest(
            repository_ref="repo://fixture",
            workspace_id=workspace_id,
            task_id=root_task_id,
            execution_id=uuid4(),
            base_commit=input_commit,
            branch=branch,
            location_ref=f"git-result-fixture://{workspace_id}/{label}",
        )
        captured = store.capture(
            manifest=source_manifest,
            input_commit=input_commit,
            operation_id=uuid4(),
            writable_paths=(".",),
        )
        return CodeInputSourceV1(
            result_id=uuid4(),
            result_hash=hashlib.sha256(label.encode()).hexdigest(),
            input_commit=captured.input_commit,
            input_tree=captured.input_tree,
            result_commit=captured.result_commit,
            result_tree=captured.result_tree,
        )

    source_a = capture_source(
        "A",
        input_commit=base_commit,
        path="values.txt",
        content=b"A\n",
    )
    source_b = capture_source(
        "B",
        input_commit=source_a.result_commit,
        path="values.txt",
        content=b"A\nB\n",
    )
    source_c = capture_source(
        "C",
        input_commit=base_commit,
        path="extra.txt",
        content=b"C\n",
    )
    source_d = capture_source(
        "D",
        input_commit=base_commit,
        path="values.txt",
        content=b"D\n",
    )
    return CodeInputMaterializationCase(
        repository=repository,
        store=store,
        base_commit=base_commit,
        workspace_manifest=integration_manifest,
        workspace_path=integration_path,
        sources={"A": source_a, "B": source_b, "C": source_c, "D": source_d},
        operation_id=uuid4(),
    )


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


@pytest.fixture
def apply_case(tmp_path: Path) -> GitApplyCase:
    repository = tmp_path / "apply-repository"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "SIDERA Apply Test")
    git(repository, "config", "user.email", "apply@sidera.local")

    (repository / "README.md").write_text("base\n")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "base")
    base_commit = git(repository, "rev-parse", "HEAD")

    (repository / "accepted.txt").write_text("accepted\n")
    git(repository, "add", "accepted.txt")
    git(repository, "commit", "-m", "accepted result")
    accepted_commit = git(repository, "rev-parse", "HEAD")
    git(repository, "branch", "target", base_commit)

    operation = ResultApplyOperationV1(
        id=uuid4(),
        workspace_id=uuid4(),
        root_task_id=uuid4(),
        acceptance_id=uuid4(),
        candidate_id=uuid4(),
        repository_ref="repo://apply",
        target_ref="refs/heads/target",
        expected_old_commit=base_commit,
        accepted_commit=accepted_commit,
        status=ResultOperationStatus.PENDING,
        attempt=1,
        lease_owner=None,
        lease_token=None,
        lease_expiry=None,
        receipt_ref=None,
        failure_code=None,
        version=1,
    )
    return GitApplyCase(
        repository=repository,
        store=GitResultStore({"repo://apply": repository}, clock=lambda: NOW),
        operation=operation,
        base_commit=base_commit,
        accepted_commit=accepted_commit,
    )
