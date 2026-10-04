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
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.handoff_evidence import HandoffWorkspaceTargetSpec
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    RepositoryCheckoutSpec,
    WorkspaceLifecycle,
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


def test_native_directory_is_provider_verified_not_an_opaque_uri(
    tmp_path: Path,
) -> None:
    repository, revision = _repository(tmp_path)
    root = tmp_path / "native-workspaces"
    root.mkdir()
    manager = GitWorktreeWorkspaceManager(root)
    spec = _spec(repository, revision)
    manifest = manager.prepare(spec, operation_id=uuid4())
    manifest.mark_preparing()
    manifest.mark_ready()
    directory = getattr(manager, "physical_directory", None)
    assert callable(directory), "N12 requires a provider-verified physical directory"
    assert directory(manifest) == (root / str(spec.execution_id)).resolve()
    with pytest.raises(WorkspaceProviderError):
        directory(replace(manifest, workspace_id=uuid4()))
    _git(root / str(spec.execution_id), "checkout", "-b", "wrong-branch")
    with pytest.raises(WorkspaceProviderError):
        directory(manifest)


@pytest.fixture
def prepared_git_workspace(tmp_path):
    repository, revision = _repository(tmp_path)
    root = tmp_path / "workspaces"
    root.mkdir()
    spec = _spec(repository, revision)
    manager = GitWorktreeWorkspaceManager(root)
    manifest = manager.prepare(spec, operation_id=uuid4())
    manifest.mark_preparing()
    manifest.mark_ready()
    return manager, spec, manifest, root


@pytest.mark.parametrize("lifecycle", ("ready", "active", "frozen", "retained"))
@pytest.mark.parametrize("content", ("dirty", "committed"))
def test_cold_native_directory_recovers_original_without_prepare_or_git_writes(
    prepared_git_workspace,
    monkeypatch,
    lifecycle,
    content,
):
    warm, spec, manifest, root = prepared_git_workspace
    if lifecycle != "ready":
        manifest.mark_active()
    if lifecycle in {"frozen", "retained"}:
        warm.freeze(manifest, operation_id=uuid4())
    if lifecycle == "retained":
        warm.retain(manifest, operation_id=uuid4())
    location = root / str(spec.execution_id)
    (location / "README.md").write_text("real Worker contents\n")
    if content == "committed":
        _git(location, "add", "README.md")
        _git(location, "commit", "-m", "Worker materialization")
    before = (
        _git(location, "rev-parse", "HEAD"),
        _git(location, "status", "--porcelain"),
        (location / "README.md").read_bytes(),
    )
    cold = GitWorktreeWorkspaceManager(root)
    calls, run_git = [], cold._run_git

    def read_git(args, **kwargs):
        calls.append(tuple(args))
        return run_git(args, **kwargs)

    monkeypatch.setattr(cold, "_run_git", read_git)
    monkeypatch.setattr(
        cold,
        "prepare",
        lambda *args, **kwargs: pytest.fail("Cold resolution prepared a replacement"),
    )
    assert cold.physical_directory(replace(manifest)) == location.resolve()
    assert cold.inspect_manifest(replace(manifest)) == manifest
    assert calls and all(
        command[0] == "rev-parse" or command == ("branch", "--show-current")
        for command in calls
    )
    assert (
        _git(location, "rev-parse", "HEAD"),
        _git(location, "status", "--porcelain"),
        (location / "README.md").read_bytes(),
    ) == before


@pytest.mark.parametrize(
    "changed",
    (
        "repository",
        "nonopaque_repository",
        "branch",
        "location_path",
        "location_query",
        "location_fragment",
        "location_noncanonical",
        "missing",
        "symlink",
        "escape",
        "lifecycle",
        "wrong_git_branch",
        "detached",
        "checkouts",
    ),
)
def test_cold_native_directory_rejects_unverified_identity_without_remembering(
    prepared_git_workspace,
    changed,
    tmp_path,
):
    _, spec, original, root = prepared_git_workspace
    manifest = replace(original)
    location = root / str(spec.execution_id)
    checkout = manifest.checkouts[0]
    if changed == "repository":
        manifest.checkouts = (
            replace(checkout, repository_ref="git-repository://" + "a" * 64),
        )
    elif changed == "nonopaque_repository":
        manifest.checkouts = (
            replace(checkout, repository_ref=str(tmp_path / "repository")),
        )
    elif changed == "branch":
        manifest.checkouts = (replace(checkout, branch="main"),)
    elif changed.startswith("location_"):
        manifest.location_ref += {
            "location_path": "/extra",
            "location_query": "?extra=1",
            "location_fragment": "#extra",
            "location_noncanonical": "",
        }[changed]
        if changed == "location_noncanonical":
            manifest.location_ref = "git-worktree://" + spec.execution_id.hex
    elif changed == "missing":
        missing = uuid4()
        manifest.location_ref = f"git-worktree://{missing}"
        manifest.checkouts = (replace(checkout, branch=f"sidera/execution/{missing}"),)
    elif changed in {"symlink", "escape"}:
        moved = root / "moved" if changed == "symlink" else tmp_path / "outside-root"
        location.rename(moved)
        location.symlink_to(moved, target_is_directory=True)
    elif changed == "lifecycle":
        manifest.lifecycle = WorkspaceLifecycle.CLEANING
    elif changed == "wrong_git_branch":
        _git(location, "checkout", "-b", "wrong-branch")
    elif changed == "detached":
        _git(location, "checkout", "--detach", "HEAD")
    elif changed == "checkouts":
        manifest.checkouts = (checkout, checkout)
    cold = GitWorktreeWorkspaceManager(root)
    with pytest.raises(WorkspaceProviderError):
        cold.physical_directory(manifest)
    with pytest.raises(WorkspaceProviderError, match="identity is unavailable"):
        cold.inspect_manifest(manifest)


def test_cold_native_directory_uses_actual_transferred_original_location(
    prepared_git_workspace,
):
    warm, spec, source, root = prepared_git_workspace
    source.mark_active()
    warm.freeze(source, operation_id=uuid4())
    target = HandoffWorkspaceTargetSpec(
        workspace_id=source.workspace_id,
        task_id=source.task_id,
        target_execution_id=uuid4(),
        target_worker_id=uuid4(),
        adapter_module_ref=spec.adapter_module_ref,
        adapter_config_version=1,
        workspace_module_ref=spec.workspace_module_ref,
        workspace_config_version=1,
        repositories=(
            {
                "repository_ref": source.checkouts[0].repository_ref,
                "revision": source.checkouts[0].base_revision,
                "branch": source.checkouts[0].branch,
                "current_head": source.checkouts[0].current_head,
            },
        ),
        anticipated_fencing_token=2,
    )
    transferred = warm.transfer_handoff(
        source, target, transition_id=uuid4(), logical_attempt=1, operation_id=uuid4()
    )
    cold = GitWorktreeWorkspaceManager(root)
    assert (
        cold.physical_directory(transferred)
        == (root / str(source.execution_id)).resolve()
    )
    assert not (root / str(target.target_execution_id)).exists()


def test_native_refence_requires_retired_busy_evidence(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    root = tmp_path / "native-refence"
    root.mkdir()
    manager = GitWorktreeWorkspaceManager(root)
    manifest = manager.prepare(_spec(repository, revision), operation_id=uuid4())
    manifest.mark_preparing()
    manifest.mark_ready()
    refence = getattr(manager, "refence_not_admitted", None)
    assert callable(refence), "N12 refence must require archived pre-admission evidence"
    with pytest.raises((WorkspaceProviderError, DomainError, ValueError, TypeError)):
        refence(
            manifest, pin=None, binding=None, physical_input=None, operation_id=uuid4()
        )
    assert manager.physical_directory(manifest).is_dir()
    assert manifest.fencing_token == 1


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


def test_freeze_recognizes_exact_persisted_activation(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    root = tmp_path / "workspaces"
    root.mkdir()
    manager = GitWorktreeWorkspaceManager(root)
    prepared = manager.prepare(_spec(repository, revision), operation_id=uuid4())
    prepared.mark_preparing()
    prepared.mark_ready()
    persisted = replace(prepared)
    persisted.mark_active()

    frozen = manager.freeze(persisted, operation_id=uuid4())

    assert frozen is persisted
    assert frozen.lifecycle is WorkspaceLifecycle.FROZEN
    assert frozen.version == 5
    assert prepared.lifecycle is WorkspaceLifecycle.READY
    assert prepared.version == 3
    assert (
        manager.physical_directory(frozen)
        == (root / str(frozen.execution_id)).resolve()
    )
    assert manager.freeze(replace(frozen), operation_id=uuid4()).id == frozen.id


@pytest.mark.parametrize(
    "changed",
    [
        "stale_version",
        "jumped_version",
        "workspace",
        "fence",
        "location",
        "checkout",
        "branch",
        "lifecycle",
    ],
)
def test_freeze_rejects_other_persisted_activation_changes(
    tmp_path: Path, changed: str
) -> None:
    repository, revision = _repository(tmp_path)
    root = tmp_path / "workspaces"
    root.mkdir()
    manager = GitWorktreeWorkspaceManager(root)
    prepared = manager.prepare(_spec(repository, revision), operation_id=uuid4())
    prepared.mark_preparing()
    prepared.mark_ready()
    persisted = replace(prepared)
    persisted.mark_active()
    if changed == "stale_version":
        persisted.version = prepared.version
    elif changed == "jumped_version":
        persisted.version += 1
    elif changed == "workspace":
        persisted.workspace_id = uuid4()
    elif changed == "fence":
        persisted.fencing_token += 1
    elif changed == "location":
        persisted.location_ref = "git-worktree://wrong-location"
    elif changed == "checkout":
        persisted.checkouts = (replace(persisted.checkouts[0], branch="wrong-branch"),)
    elif changed == "branch":
        _git(root / str(prepared.execution_id), "checkout", "-b", "wrong-branch")
    else:
        persisted.lifecycle = WorkspaceLifecycle.PREPARING

    with pytest.raises(WorkspaceProviderError):
        manager.freeze(persisted, operation_id=uuid4())
    assert prepared.lifecycle is WorkspaceLifecycle.READY
    assert prepared.version == 3
