import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from sidera_control_plane.adapters.git_workspace import (
    GitWorktreeWorkspaceManager,
    WorkspaceProviderError,
)
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    RepositoryCheckoutSpec,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


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


def test_provider_rejects_missing_or_non_directory_root(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceProviderError, match="workspace root"):
        GitWorktreeWorkspaceManager(tmp_path / "missing")

    file_root = tmp_path / "file-root"
    file_root.write_text("not a directory")
    with pytest.raises(WorkspaceProviderError, match="workspace root"):
        GitWorktreeWorkspaceManager(file_root)


def test_prepare_is_pinned_and_returns_opaque_location(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    root = tmp_path / "workspaces"
    root.mkdir()
    spec = _spec(repository, revision)
    manager = GitWorktreeWorkspaceManager(root)

    manifest = manager.prepare(spec, operation_id=uuid4())

    checkout = manifest.checkouts[0]
    assert manifest.location_ref == f"git-worktree://{spec.execution_id}"
    assert str(root) not in manifest.location_ref
    assert checkout.base_revision == revision
    assert checkout.current_head == revision
    assert checkout.branch == f"sidera/execution/{spec.execution_id}"

    location = root / str(spec.execution_id)
    assert location.is_dir()
    assert _git(location, "branch", "--show-current") == checkout.branch
    assert _git(location, "rev-parse", "HEAD") == revision


def test_prepare_refuses_dirty_source_and_unsupported_checkout_policy(
    tmp_path: Path,
) -> None:
    repository, revision = _repository(tmp_path)
    root = tmp_path / "workspaces"
    root.mkdir()
    manager = GitWorktreeWorkspaceManager(root)

    (repository / "README.md").write_text("dirty\n")
    with pytest.raises(WorkspaceProviderError, match="clean"):
        manager.prepare(_spec(repository, revision), operation_id=uuid4())

    clean_root = tmp_path / "clean"
    clean_root.mkdir()
    clean_repository, clean_revision = _repository(clean_root)
    clone_spec = _spec(clean_repository, clean_revision)
    with pytest.raises(WorkspaceProviderError, match="worktree"):
        manager.prepare(
            replace(
                clone_spec,
                repositories=(
                    RepositoryCheckoutSpec(
                        repository_ref=str(clean_repository),
                        base_revision=clean_revision,
                        checkout_mode=CheckoutMode.CLONE,
                        writable_paths=(".",),
                        readable_paths=(".",),
                        branch_policy=BranchPolicy.EXECUTION,
                    ),
                ),
            ),
            operation_id=uuid4(),
        )


def test_release_refuses_active_workspace_and_quarantines_failed_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, revision = _repository(tmp_path)
    root = tmp_path / "workspaces"
    root.mkdir()
    manager = GitWorktreeWorkspaceManager(root)
    manifest = manager.prepare(_spec(repository, revision), operation_id=uuid4())

    with pytest.raises(WorkspaceProviderError, match="active"):
        manager.release(manifest, operation_id=uuid4())

    manifest.mark_preparing()
    manifest.mark_ready()
    manifest.mark_active()
    manager.freeze(manifest, operation_id=uuid4())
    manager.retain(manifest, operation_id=uuid4())
    manifest.begin_cleanup()

    def fail_cleanup(*_args: object, **_kwargs: object) -> object:
        raise subprocess.CalledProcessError(1, ["git", "worktree", "remove"])

    monkeypatch.setattr(manager, "_run_git", fail_cleanup)
    with pytest.raises(WorkspaceProviderError, match="quarantined"):
        manager.release(manifest, operation_id=uuid4())
    assert manifest.lifecycle.value == "quarantined"
