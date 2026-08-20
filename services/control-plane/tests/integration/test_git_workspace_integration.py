import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from sidera_control_plane.adapters.git_workspace import GitWorktreeWorkspaceManager
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    RepositoryCheckoutSpec,
    WorkspaceLifecycle,
)

pytestmark = pytest.mark.integration


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "SIDERA Test")
    _git(repository, "config", "user.email", "test@sidera.local")
    (repository / "README.md").write_text("base\n")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


def _spec(repository: Path, revision: str) -> ExecutionSpec:
    return ExecutionSpec(
        schema_version=1,
        workspace_id=uuid4(),
        task_id=uuid4(),
        execution_id=uuid4(),
        worker_id=uuid4(),
        lease_id=uuid4(),
        fencing_token=1,
        goal="Implement workspace provider",
        acceptance_criteria=("worktree is isolated",),
        context_package_ref=None,
        repositories=(
            RepositoryCheckoutSpec(
                repository_ref=str(repository),
                base_revision=revision,
                checkout_mode=CheckoutMode.WORKTREE,
                writable_paths=(".",),
                readable_paths=(".",),
                branch_policy=BranchPolicy.EXECUTION,
            ),
        ),
        tool_policy={},
        sandbox_policy={"mode": "filesystem"},
        budget={"max_seconds": 60},
        deadline=None,
        expected_outputs=("diff",),
        adapter_module_ref=uuid4(),
        adapter_config_version=1,
        workspace_module_ref=uuid4(),
        workspace_config_version=1,
    )


def test_two_executions_use_independent_worktrees_and_cleanup_after_freeze(
    tmp_path: Path,
) -> None:
    repository, revision = _repository(tmp_path)
    root = tmp_path / "workspaces"
    root.mkdir()
    manager = GitWorktreeWorkspaceManager(root)
    first_spec = _spec(repository, revision)
    second_spec = _spec(repository, revision)

    first = manager.prepare(first_spec, operation_id=uuid4())
    second = manager.prepare(second_spec, operation_id=uuid4())
    first_path = root / str(first_spec.execution_id)
    second_path = root / str(second_spec.execution_id)

    (first_path / "README.md").write_text("first execution\n")
    _git(first_path, "add", "README.md")
    _git(first_path, "commit", "-m", "first execution")

    assert first_path != second_path
    assert _git(first_path, "branch", "--show-current") != _git(
        second_path, "branch", "--show-current"
    )
    assert _git(second_path, "rev-parse", "HEAD") == revision

    for manifest in (first, second):
        manifest.mark_preparing()
        manifest.mark_ready()
        manifest.mark_active()
        manager.freeze(manifest, operation_id=uuid4())
        manager.retain(manifest, operation_id=uuid4())
        manifest.begin_cleanup()
        manager.release(manifest, operation_id=uuid4())
        assert manifest.lifecycle is WorkspaceLifecycle.RELEASED

    assert not first_path.exists()
    assert not second_path.exists()
