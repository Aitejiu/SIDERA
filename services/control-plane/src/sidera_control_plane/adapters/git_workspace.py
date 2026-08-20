import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from uuid import UUID, uuid4

from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
    WorkspaceLifecycle,
)


class WorkspaceProviderError(RuntimeError):
    """The workspace provider cannot safely perform the requested operation."""


class GitWorktreeWorkspaceManager:
    """Prepare one deterministic, isolated Git worktree per Execution."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        retention_seconds: int = 86_400,
        git_executable: str = "git",
    ) -> None:
        root = Path(workspace_root).expanduser()
        if not root.is_dir():
            raise WorkspaceProviderError(
                f"workspace root must be an existing directory: {root}"
            )
        if retention_seconds <= 0:
            raise WorkspaceProviderError("workspace retention must be positive")
        if not git_executable.strip():
            raise WorkspaceProviderError("git executable is required")
        self._workspace_root = root.resolve()
        self._retention_seconds = retention_seconds
        self._git_executable = git_executable.strip()
        self._manifests: dict[str, ExecutionWorkspaceManifest] = {}

    def prepare(
        self, spec: ExecutionSpec, *, operation_id: UUID
    ) -> ExecutionWorkspaceManifest:
        checkout_spec = spec.repositories[0]
        self._validate_checkout_spec(checkout_spec.checkout_mode, checkout_spec.branch_policy)
        repository = self._resolve_repository(checkout_spec.repository_ref)
        self._assert_clean_source(repository)
        revision = self._resolve_revision(repository, checkout_spec.base_revision)

        branch = f"sidera/execution/{spec.execution_id}"
        location = self._location_for_execution(spec.execution_id)
        if location.exists():
            self._verify_worktree(
                location,
                repository=repository,
                expected_branch=branch,
            )
        else:
            self._create_worktree(
                repository,
                location=location,
                branch=branch,
                revision=revision,
            )

        current_head = self._git_output(["rev-parse", "HEAD"], cwd=location)
        manifest = ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=spec.workspace_id,
            task_id=spec.task_id,
            execution_id=spec.execution_id,
            checkouts=(
                WorkspaceCheckout(
                    repository_ref=str(repository),
                    base_revision=revision,
                    branch=branch,
                    current_head=current_head,
                ),
            ),
            location_ref=f"git-worktree://{spec.execution_id}",
            retention_seconds=self._retention_seconds,
            fencing_token=spec.fencing_token,
            created_at=datetime.now(UTC),
        )
        self._manifests[manifest.location_ref] = manifest
        return manifest

    def freeze(
        self,
        manifest: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
    ) -> ExecutionWorkspaceManifest:
        del operation_id
        self._require_lifecycle(manifest, WorkspaceLifecycle.ACTIVE)
        checkout = self._require_checkout(manifest)
        location = self._location_for_manifest(manifest)
        repository = self._resolve_repository(checkout.repository_ref)
        self._verify_worktree(
            location,
            repository=repository,
            expected_branch=checkout.branch,
        )
        current_head = self._git_output(["rev-parse", "HEAD"], cwd=location)
        manifest.checkouts = (
            WorkspaceCheckout(
                repository_ref=checkout.repository_ref,
                base_revision=checkout.base_revision,
                branch=checkout.branch,
                current_head=current_head,
            ),
        )
        manifest.freeze(now=datetime.now(UTC))
        self._manifests[manifest.location_ref] = manifest
        return manifest

    def retain(
        self,
        manifest: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
    ) -> ExecutionWorkspaceManifest:
        del operation_id
        self._require_lifecycle(manifest, WorkspaceLifecycle.FROZEN)
        manifest.retain(
            until=datetime.now(UTC) + timedelta(seconds=manifest.retention_seconds)
        )
        self._manifests[manifest.location_ref] = manifest
        return manifest

    def release(
        self,
        manifest: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
    ) -> ExecutionWorkspaceManifest:
        del operation_id
        if manifest.lifecycle is not WorkspaceLifecycle.CLEANING:
            raise WorkspaceProviderError(
                "workspace must be retained and no longer active before release"
            )
        checkout = self._require_checkout(manifest)
        location = self._location_for_manifest(manifest)
        if location.exists():
            try:
                repository = self._resolve_repository(checkout.repository_ref)
                self._verify_worktree(
                    location,
                    repository=repository,
                    expected_branch=checkout.branch,
                )
                self._run_git(
                    ["worktree", "remove", "--force", str(location)], cwd=repository
                )
            except (OSError, WorkspaceProviderError, subprocess.CalledProcessError) as exc:
                manifest.quarantine()
                self._manifests[manifest.location_ref] = manifest
                raise WorkspaceProviderError(
                    "workspace cleanup failed; workspace was quarantined"
                ) from exc
        manifest.release(now=datetime.now(UTC))
        self._manifests.pop(manifest.location_ref, None)
        return manifest

    def inspect(self, location_ref: str) -> ExecutionWorkspaceManifest:
        manifest = self._manifests.get(location_ref)
        if manifest is None:
            raise WorkspaceProviderError(
                "workspace metadata is unavailable for this location reference"
            )
        if manifest.lifecycle not in {
            WorkspaceLifecycle.RELEASED,
            WorkspaceLifecycle.QUARANTINED,
        }:
            checkout = self._require_checkout(manifest)
            location = self._location_for_manifest(manifest)
            repository = self._resolve_repository(checkout.repository_ref)
            self._verify_worktree(
                location,
                repository=repository,
                expected_branch=checkout.branch,
            )
        return manifest

    def _create_worktree(
        self,
        repository: Path,
        *,
        location: Path,
        branch: str,
        revision: str,
    ) -> None:
        if self._branch_exists(repository, branch):
            self._run_git(
                ["worktree", "add", str(location), branch], cwd=repository
            )
        else:
            self._run_git(
                ["worktree", "add", "-b", branch, str(location), revision],
                cwd=repository,
            )
        self._verify_worktree(
            location,
            repository=repository,
            expected_branch=branch,
        )

    def _resolve_repository(self, repository_ref: str) -> Path:
        repository = Path(repository_ref).expanduser()
        try:
            resolved = repository.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceProviderError(
                f"repository does not exist: {repository_ref}"
            ) from exc
        if not resolved.is_dir():
            raise WorkspaceProviderError(
                f"repository must be a directory: {repository_ref}"
            )
        top_level = Path(
            self._git_output(["rev-parse", "--show-toplevel"], cwd=resolved)
        ).resolve()
        if top_level != resolved:
            resolved = top_level
        return resolved

    def _assert_clean_source(self, repository: Path) -> None:
        status = self._git_output(
            ["status", "--porcelain", "--untracked-files=all"], cwd=repository
        )
        if status:
            raise WorkspaceProviderError(
                "repository must be clean before preparing a worktree"
            )

    def _resolve_revision(self, repository: Path, revision: str) -> str:
        resolved = self._git_output(
            ["rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=repository
        )
        if resolved.lower() != revision.lower():
            raise WorkspaceProviderError(
                "base revision must identify the exact requested commit"
            )
        return resolved

    def _verify_worktree(
        self,
        location: Path,
        *,
        repository: Path,
        expected_branch: str,
    ) -> None:
        if not location.is_dir() or location.is_symlink():
            raise WorkspaceProviderError("workspace location is not a directory")
        top_level = Path(
            self._git_output(["rev-parse", "--show-toplevel"], cwd=location)
        ).resolve()
        if top_level != location.resolve():
            raise WorkspaceProviderError("workspace location is not the expected worktree")
        branch = self._git_output(["branch", "--show-current"], cwd=location)
        if branch != expected_branch:
            raise WorkspaceProviderError("workspace branch identity does not match")
        source_common_dir = self._canonical_git_path(
            self._git_output(["rev-parse", "--git-common-dir"], cwd=repository),
            repository,
        )
        worktree_common_dir = self._canonical_git_path(
            self._git_output(["rev-parse", "--git-common-dir"], cwd=location),
            location,
        )
        if source_common_dir != worktree_common_dir:
            raise WorkspaceProviderError("workspace belongs to a different repository")

    def _branch_exists(self, repository: Path, branch: str) -> bool:
        result = self._run_git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repository,
            check=False,
        )
        return result.returncode == 0

    def _location_for_execution(self, execution_id: UUID) -> Path:
        location = (self._workspace_root / str(execution_id)).resolve()
        if self._workspace_root not in location.parents:
            raise WorkspaceProviderError("execution location escapes workspace root")
        return location

    def _location_for_manifest(self, manifest: ExecutionWorkspaceManifest) -> Path:
        return self._location_for_execution(manifest.execution_id)

    @staticmethod
    def _validate_checkout_spec(
        checkout_mode: CheckoutMode,
        branch_policy: BranchPolicy,
    ) -> None:
        if checkout_mode is not CheckoutMode.WORKTREE:
            raise WorkspaceProviderError("R3 Git provider supports worktree checkout only")
        if branch_policy is not BranchPolicy.EXECUTION:
            raise WorkspaceProviderError("R3 Git provider owns execution branch names")

    @staticmethod
    def _require_checkout(
        manifest: ExecutionWorkspaceManifest,
    ) -> WorkspaceCheckout:
        if len(manifest.checkouts) != 1:
            raise WorkspaceProviderError("R3 Git provider requires one checkout")
        return manifest.checkouts[0]

    @staticmethod
    def _require_lifecycle(
        manifest: ExecutionWorkspaceManifest,
        expected: WorkspaceLifecycle,
    ) -> None:
        if manifest.lifecycle is not expected:
            raise WorkspaceProviderError(
                f"workspace operation requires lifecycle {expected.value}"
            )

    def _canonical_git_path(self, value: str, cwd: Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = cwd / path
        return path.resolve()

    def _git_output(self, args: Sequence[str], *, cwd: Path) -> str:
        return self._run_git(args, cwd=cwd).stdout.strip()

    def _run_git(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
    ) -> CompletedProcess[str]:
        command = [self._git_executable, *args]
        try:
            return subprocess.run(
                command,
                cwd=cwd,
                check=check,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise WorkspaceProviderError(
                f"git executable is unavailable: {self._git_executable}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "git command failed").strip()
            raise WorkspaceProviderError(detail) from exc
