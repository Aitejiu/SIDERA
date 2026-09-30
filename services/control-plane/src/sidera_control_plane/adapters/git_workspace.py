import hashlib
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from uuid import UUID, uuid4

from sidera_control_plane.application.result_ports import CodeInputReceipt
from sidera_control_plane.domain.code_results import CodeInputManifestV1
from sidera_control_plane.domain.handoff_evidence import (
    HandoffInspectionOutcome,
    HandoffWorkspaceInspection,
    HandoffWorkspaceTargetSpec,
)
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


def opaque_repository_ref(repository: Path) -> str:
    canonical = Path(repository).expanduser().resolve(strict=False)
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
    return f"git-repository://{digest}"


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
        self._manifests_by_id: dict[UUID, ExecutionWorkspaceManifest] = {}
        self._manifest_ids_by_location: dict[str, set[UUID]] = {}
        self._target_manifest_id_by_handoff_operation: dict[UUID, UUID] = {}
        self._handoff_target_identity_by_operation: dict[
            UUID, tuple[UUID, UUID, UUID, UUID, UUID, int, UUID, int]
        ] = {}
        self._handoff_inspection_by_operation: dict[
            UUID, HandoffWorkspaceInspection
        ] = {}
        self._physical_location_by_manifest_id: dict[UUID, Path] = {}
        self._workspace_binding_by_manifest_id: dict[UUID, tuple[UUID, int]] = {}
        self._repository_by_ref: dict[str, Path] = {}

    def prepare(
        self, spec: ExecutionSpec, *, operation_id: UUID
    ) -> ExecutionWorkspaceManifest:
        checkout_spec = spec.repositories[0]
        self._validate_checkout_spec(
            checkout_spec.checkout_mode, checkout_spec.branch_policy
        )
        repository = self._resolve_repository(checkout_spec.repository_ref)
        revision = self._resolve_revision(repository, checkout_spec.base_revision)

        branch = f"sidera/execution/{spec.execution_id}"
        location = self._location_for_execution(spec.execution_id)
        location_ref = f"git-worktree://{spec.execution_id}"
        repository_ref = self._opaque_repository_ref(repository)
        existing_ids = self._manifest_ids_by_location.get(location_ref, set())
        for existing_id in existing_ids:
            existing = self._manifests_by_id[existing_id]
            if existing.lifecycle in {
                    WorkspaceLifecycle.RELEASED,
                    WorkspaceLifecycle.QUARANTINED,
                }:
                continue
            if (
                existing.workspace_id != spec.workspace_id
                or existing.task_id != spec.task_id
                or existing.execution_id != spec.execution_id
            ):
                raise WorkspaceProviderError(
                    "workspace execution identity changed during prepare"
                )
            existing_checkout = self._require_checkout(existing)
            if (
                existing_checkout.repository_ref != repository_ref
                or existing_checkout.base_revision != revision
                or existing_checkout.branch != branch
            ):
                raise WorkspaceProviderError(
                    "workspace checkout identity changed during prepare"
                )
            binding = self._workspace_binding_by_manifest_id.get(existing.id)
            if binding is not None and binding != (
                spec.workspace_module_ref,
                spec.workspace_config_version,
            ):
                raise WorkspaceProviderError(
                    "workspace module binding changed during prepare"
                )
            if existing.fencing_token != spec.fencing_token:
                raise WorkspaceProviderError(
                    "workspace execution already has a different fencing token"
                )
            return existing
        if location.exists():
            raise WorkspaceProviderError(
                "workspace location exists without a manifest; reconcile before prepare"
            )
        self._assert_clean_source(repository)
        self._create_worktree(
            repository,
            location=location,
            branch=branch,
            revision=revision,
        )

        current_head = self._git_output(["rev-parse", "HEAD"], cwd=location)
        self._repository_by_ref[repository_ref] = repository
        manifest = ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=spec.workspace_id,
            task_id=spec.task_id,
            execution_id=spec.execution_id,
            checkouts=(
                WorkspaceCheckout(
                    repository_ref=repository_ref,
                    base_revision=revision,
                    branch=branch,
                    current_head=current_head,
                ),
            ),
            location_ref=location_ref,
            retention_seconds=self._retention_seconds,
            fencing_token=spec.fencing_token,
            created_at=datetime.now(UTC),
        )
        self._remember_manifest(
            manifest,
            physical_location=location,
            workspace_module_ref=spec.workspace_module_ref,
            workspace_config_version=spec.workspace_config_version,
        )
        return manifest

    def materialize(
        self,
        *,
        workspace_manifest: ExecutionWorkspaceManifest,
        code_inputs: CodeInputManifestV1,
        operation_id: UUID,
    ) -> CodeInputReceipt:
        """Delegate exact code-input installation to the Git result boundary."""

        if not isinstance(code_inputs, CodeInputManifestV1):
            raise WorkspaceProviderError("code input manifest is invalid")
        checkout = self._require_checkout(workspace_manifest)
        location = self._location_for_manifest(workspace_manifest)
        repository = self._resolve_repository_for_manifest(workspace_manifest, location)
        from sidera_control_plane.adapters.git_results import GitResultStore

        return GitResultStore(
            {checkout.repository_ref: repository},
            git_executable=self._git_executable,
        ).materialize(
            workspace_manifest=workspace_manifest,
            code_inputs=code_inputs,
            operation_id=operation_id,
        )

    def freeze(
        self,
        manifest: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
    ) -> ExecutionWorkspaceManifest:
        if not isinstance(operation_id, UUID):
            raise WorkspaceProviderError("freeze operation identity is required")
        stored = self._manifests_by_id.get(manifest.id)
        if stored is not None:
            _require_manifest_identity(stored, manifest)
            if stored.lifecycle is WorkspaceLifecycle.FROZEN:
                return self.inspect_manifest(stored)
            if stored.lifecycle is not manifest.lifecycle:
                raise WorkspaceProviderError("workspace manifest lifecycle changed")
            manifest = stored
        if manifest.lifecycle not in {
            WorkspaceLifecycle.ACTIVE,
            WorkspaceLifecycle.FROZEN,
        }:
            self._require_lifecycle(manifest, WorkspaceLifecycle.ACTIVE)
        checkout = self._require_checkout(manifest)
        location = self._location_for_manifest(manifest)
        repository = self._resolve_repository_for_manifest(manifest, location)
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
        if manifest.lifecycle is WorkspaceLifecycle.ACTIVE:
            manifest.freeze(now=datetime.now(UTC))
        self._remember_manifest(manifest, physical_location=location)
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
        self._remember_manifest(manifest)
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
        if any(
            candidate_id != manifest.id
            and self._manifests_by_id[candidate_id].lifecycle
            not in {WorkspaceLifecycle.RELEASED, WorkspaceLifecycle.QUARANTINED}
            for candidate_id in self._manifest_ids_by_location.get(
                manifest.location_ref, set()
            )
        ):
            raise WorkspaceProviderError(
                "workspace location still has another handoff manifest"
            )
        if location.exists():
            try:
                repository = self._resolve_repository_for_manifest(manifest, location)
                self._verify_worktree(
                    location,
                    repository=repository,
                    expected_branch=checkout.branch,
                )
                self._run_git(
                    ["worktree", "remove", "--force", str(location)], cwd=repository
                )
            except (
                OSError,
                WorkspaceProviderError,
                subprocess.CalledProcessError,
            ) as exc:
                manifest.quarantine()
                self._remember_manifest(manifest, physical_location=location)
                raise WorkspaceProviderError(
                    "workspace cleanup failed; workspace was quarantined"
                ) from exc
        manifest.release(now=datetime.now(UTC))
        self._forget_manifest(manifest)
        return manifest

    def inspect(self, location_ref: str) -> ExecutionWorkspaceManifest:
        ids = self._manifest_ids_by_location.get(location_ref, set())
        manifests = tuple(
            self._manifests_by_id[item_id]
            for item_id in ids
            if self._manifests_by_id[item_id].lifecycle
            not in {WorkspaceLifecycle.RELEASED, WorkspaceLifecycle.QUARANTINED}
        )
        if not manifests:
            raise WorkspaceProviderError(
                "workspace metadata is unavailable for this location reference"
            )
        active = tuple(
            item for item in manifests if item.lifecycle is WorkspaceLifecycle.ACTIVE
        )
        if len(active) > 1:
            raise WorkspaceProviderError(
                "workspace location has multiple active manifests"
            )
        ready = tuple(
            item for item in manifests if item.lifecycle is WorkspaceLifecycle.READY
        )
        if len(ready) == 1:
            manifest = ready[0]
        elif len(active) == 1:
            manifest = active[0]
        elif len(manifests) == 1:
            manifest = manifests[0]
        else:
            raise WorkspaceProviderError(
                "workspace location has ambiguous non-active manifests"
            )
        if manifest.lifecycle not in {
            WorkspaceLifecycle.RELEASED,
            WorkspaceLifecycle.QUARANTINED,
        }:
            checkout = self._require_checkout(manifest)
            location = self._location_for_manifest(manifest)
            repository = self._resolve_repository_for_manifest(manifest, location)
            self._verify_worktree(
                location,
                repository=repository,
                expected_branch=checkout.branch,
            )
        return manifest

    def inspect_manifest(
        self, manifest: ExecutionWorkspaceManifest
    ) -> ExecutionWorkspaceManifest:
        """Inspect one manifest identity even when its location is shared."""

        stored = self._manifests_by_id.get(manifest.id)
        if stored is None:
            raise WorkspaceProviderError("workspace manifest identity is unavailable")
        _require_manifest_identity(stored, manifest)
        return self.inspect_manifest_state(stored)

    def inspect_manifest_state(
        self, manifest: ExecutionWorkspaceManifest
    ) -> ExecutionWorkspaceManifest:
        if manifest.lifecycle not in {
            WorkspaceLifecycle.RELEASED,
            WorkspaceLifecycle.QUARANTINED,
        }:
            checkout = self._require_checkout(manifest)
            location = self._location_for_manifest(manifest)
            repository = self._resolve_repository_for_manifest(manifest, location)
            self._verify_worktree(
                location,
                repository=repository,
                expected_branch=checkout.branch,
            )
        return manifest

    def transfer_handoff(
        self,
        source_manifest: ExecutionWorkspaceManifest,
        target: HandoffWorkspaceTargetSpec,
        *,
        transition_id: UUID,
        logical_attempt: int,
        operation_id: UUID,
    ) -> ExecutionWorkspaceManifest:
        if not isinstance(operation_id, UUID):
            raise WorkspaceProviderError("handoff transfer operation identity is required")
        if logical_attempt < 1:
            raise WorkspaceProviderError("handoff logical attempt must be positive")
        stored_source = self._manifests_by_id.get(source_manifest.id)
        if stored_source is not None:
            _require_manifest_identity(stored_source, source_manifest)
            source_manifest = stored_source
        if source_manifest.lifecycle is not WorkspaceLifecycle.FROZEN:
            raise WorkspaceProviderError("handoff source workspace must be frozen")
        if source_manifest.fencing_token < 1:
            raise WorkspaceProviderError("source fencing token is invalid")
        self._validate_handoff_target(source_manifest, target)

        existing_id = self._target_manifest_id_by_handoff_operation.get(operation_id)
        if existing_id is not None:
            existing = self._manifests_by_id.get(existing_id)
            if existing is None:
                raise WorkspaceProviderError(
                    "handoff target identity exists without its manifest"
                )
            inspection = self._handoff_inspection_by_operation.get(operation_id)
            if inspection is None:
                raise WorkspaceProviderError(
                    "handoff target identity exists without inspection"
                )
            if (
                inspection.transition_id != transition_id
                or inspection.logical_attempt != logical_attempt
                or inspection.source_manifest_id != source_manifest.id
            ):
                raise WorkspaceProviderError("handoff operation identity changed")
            if self._handoff_target_identity_by_operation.get(operation_id) != (
                target.workspace_id,
                target.task_id,
                target.target_execution_id,
                target.target_worker_id,
                target.adapter_module_ref,
                target.adapter_config_version,
                target.workspace_module_ref,
                target.workspace_config_version,
            ):
                raise WorkspaceProviderError("handoff target identity changed")
            if (
                existing.workspace_id != target.workspace_id
                or existing.task_id != target.task_id
                or existing.execution_id != target.target_execution_id
                or existing.location_ref != source_manifest.location_ref
                or existing.checkouts != source_manifest.checkouts
                or existing.fencing_token != target.anticipated_fencing_token
            ):
                raise WorkspaceProviderError("handoff target manifest changed")
            return existing

        checkout = self._require_checkout(source_manifest)
        location = self._location_for_manifest(source_manifest)
        repository = self._resolve_repository_for_manifest(source_manifest, location)
        self._verify_worktree(
            location,
            repository=repository,
            expected_branch=checkout.branch,
        )
        for manifest_id in self._manifest_ids_by_location.get(
            source_manifest.location_ref, set()
        ):
            candidate = self._manifests_by_id[manifest_id]
            if (
                candidate.id != source_manifest.id
                and candidate.lifecycle
                in {WorkspaceLifecycle.READY, WorkspaceLifecycle.ACTIVE}
            ):
                raise WorkspaceProviderError(
                    "workspace location already has a reserved or active manifest"
                )

        target_manifest = ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=target.workspace_id,
            task_id=target.task_id,
            execution_id=target.target_execution_id,
            checkouts=source_manifest.checkouts,
            location_ref=source_manifest.location_ref,
            retention_seconds=source_manifest.retention_seconds,
            fencing_token=target.anticipated_fencing_token,
            created_at=datetime.now(UTC),
        )
        target_manifest.mark_preparing()
        target_manifest.mark_ready()
        self._remember_manifest(
            target_manifest,
            physical_location=location,
            workspace_binding=self._workspace_binding_by_manifest_id.get(
                source_manifest.id
            ),
        )
        self._target_manifest_id_by_handoff_operation[operation_id] = target_manifest.id
        self._handoff_target_identity_by_operation[operation_id] = (
            target.workspace_id,
            target.task_id,
            target.target_execution_id,
            target.target_worker_id,
            target.adapter_module_ref,
            target.adapter_config_version,
            target.workspace_module_ref,
            target.workspace_config_version,
        )
        self._handoff_inspection_by_operation[operation_id] = HandoffWorkspaceInspection(
            transition_id=transition_id,
            logical_attempt=logical_attempt,
            operation_id=operation_id,
            outcome=HandoffInspectionOutcome.SUCCEEDED,
            source_manifest_id=source_manifest.id,
            target_manifest_id=target_manifest.id,
            source_lifecycle=source_manifest.lifecycle.value,
            target_lifecycle=target_manifest.lifecycle.value,
            observed_at=datetime.now(UTC),
            evidence_refs=(f"git-worktree://handoff/{operation_id}",),
        )
        return target_manifest

    def inspect_handoff_transfer(
        self,
        *,
        transition_id: UUID,
        logical_attempt: int,
        operation_id: UUID,
    ) -> HandoffWorkspaceInspection:
        if not isinstance(operation_id, UUID):
            raise WorkspaceProviderError("handoff inspection operation identity is required")
        if logical_attempt < 1:
            raise WorkspaceProviderError("handoff logical attempt must be positive")
        inspection = self._handoff_inspection_by_operation.get(operation_id)
        if inspection is not None:
            if (
                inspection.transition_id != transition_id
                or inspection.logical_attempt != logical_attempt
            ):
                raise WorkspaceProviderError("handoff inspection identity changed")
            return inspection
        return HandoffWorkspaceInspection(
            transition_id=transition_id,
            logical_attempt=logical_attempt,
            operation_id=operation_id,
            outcome=HandoffInspectionOutcome.ABSENT,
            source_manifest_id=None,
            target_manifest_id=None,
            source_lifecycle=None,
            target_lifecycle=None,
            observed_at=datetime.now(UTC),
            evidence_refs=(f"git-worktree://handoff/{operation_id}/absent",),
        )

    def manifest_by_id(self, manifest_id: UUID) -> ExecutionWorkspaceManifest | None:
        return self._manifests_by_id.get(manifest_id)

    def configured_repository_resolver(self) -> dict[str, Path]:
        """Return the configured opaque repository refs for result adapters."""

        return dict(self._repository_by_ref)

    def _validate_handoff_target(
        self,
        source_manifest: ExecutionWorkspaceManifest,
        target: HandoffWorkspaceTargetSpec,
    ) -> None:
        if target.workspace_id != source_manifest.workspace_id:
            raise WorkspaceProviderError("handoff target workspace owner changed")
        if target.task_id != source_manifest.task_id:
            raise WorkspaceProviderError("handoff target task owner changed")
        if target.target_execution_id == source_manifest.execution_id:
            raise WorkspaceProviderError("handoff target execution must be new")
        if target.anticipated_fencing_token != source_manifest.fencing_token + 1:
            raise WorkspaceProviderError("handoff target fence must increment exactly")
        binding = self._workspace_binding_by_manifest_id.get(source_manifest.id)
        if binding is not None and binding != (
            target.workspace_module_ref,
            target.workspace_config_version,
        ):
            raise WorkspaceProviderError("handoff workspace module binding changed")
        if len(target.repositories) != len(source_manifest.checkouts):
            raise WorkspaceProviderError("handoff checkout count changed")
        for checkout, repository_spec in zip(
            source_manifest.checkouts,
            target.repositories,
            strict=True,
        ):
            repository_ref = repository_spec.get("repository_ref")
            if repository_ref != checkout.repository_ref:
                raise WorkspaceProviderError("handoff repository identity changed")
            revision = repository_spec.get("base_revision", repository_spec.get("revision"))
            if revision is not None and revision != checkout.base_revision:
                raise WorkspaceProviderError("handoff repository revision changed")
            for key, expected in (
                ("branch", checkout.branch),
                ("current_head", checkout.current_head),
            ):
                actual = repository_spec.get(key)
                if actual is not None and actual != expected:
                    raise WorkspaceProviderError(f"handoff {key} changed")

    def _create_worktree(
        self,
        repository: Path,
        *,
        location: Path,
        branch: str,
        revision: str,
    ) -> None:
        if self._branch_exists(repository, branch):
            self._run_git(["worktree", "add", str(location), branch], cwd=repository)
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

    def _remember_manifest(
        self,
        manifest: ExecutionWorkspaceManifest,
        *,
        physical_location: Path | None = None,
        workspace_module_ref: UUID | None = None,
        workspace_config_version: int | None = None,
        workspace_binding: tuple[UUID, int] | None = None,
    ) -> None:
        self._manifests_by_id[manifest.id] = manifest
        self._manifest_ids_by_location.setdefault(manifest.location_ref, set()).add(
            manifest.id
        )
        if physical_location is not None:
            self._physical_location_by_manifest_id[manifest.id] = physical_location
        if workspace_binding is None and workspace_module_ref is not None:
            if workspace_config_version is None:
                raise WorkspaceProviderError("workspace config version is required")
            workspace_binding = (workspace_module_ref, workspace_config_version)
        if workspace_binding is not None:
            self._workspace_binding_by_manifest_id[manifest.id] = workspace_binding

    def _forget_manifest(self, manifest: ExecutionWorkspaceManifest) -> None:
        self._manifests_by_id.pop(manifest.id, None)
        ids = self._manifest_ids_by_location.get(manifest.location_ref)
        if ids is not None:
            ids.discard(manifest.id)
            if not ids:
                self._manifest_ids_by_location.pop(manifest.location_ref, None)
        self._physical_location_by_manifest_id.pop(manifest.id, None)
        self._workspace_binding_by_manifest_id.pop(manifest.id, None)

    @staticmethod
    def _opaque_repository_ref(repository: Path) -> str:
        return opaque_repository_ref(repository)

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

    def _resolve_repository_for_manifest(
        self,
        manifest: ExecutionWorkspaceManifest,
        location: Path,
    ) -> Path:
        checkout = self._require_checkout(manifest)
        known = self._repository_by_ref.get(checkout.repository_ref)
        if known is not None:
            return known
        if not checkout.repository_ref.startswith("git-repository://"):
            return self._resolve_repository(checkout.repository_ref)
        common_dir = Path(
            self._git_output(["rev-parse", "--git-common-dir"], cwd=location)
        )
        if not common_dir.is_absolute():
            common_dir = location / common_dir
        common_dir = common_dir.resolve()
        repository = common_dir.parent if common_dir.name == ".git" else common_dir
        if not repository.is_dir():
            raise WorkspaceProviderError("Git repository for workspace is unavailable")
        self._repository_by_ref[checkout.repository_ref] = repository
        return repository

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
            raise WorkspaceProviderError(
                "workspace location is not the expected worktree"
            )
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
        known = self._physical_location_by_manifest_id.get(manifest.id)
        if known is not None:
            return known
        return self._location_for_execution(manifest.execution_id)

    @staticmethod
    def _validate_checkout_spec(
        checkout_mode: CheckoutMode,
        branch_policy: BranchPolicy,
    ) -> None:
        if checkout_mode is not CheckoutMode.WORKTREE:
            raise WorkspaceProviderError(
                "R3 Git provider supports worktree checkout only"
            )
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


def _require_manifest_identity(
    stored: ExecutionWorkspaceManifest,
    candidate: ExecutionWorkspaceManifest,
) -> None:
    if (
        stored.id != candidate.id
        or stored.workspace_id != candidate.workspace_id
        or stored.task_id != candidate.task_id
        or stored.execution_id != candidate.execution_id
        or stored.checkouts != candidate.checkouts
        or stored.location_ref != candidate.location_ref
        or stored.retention_seconds != candidate.retention_seconds
        or stored.fencing_token != candidate.fencing_token
        or stored.created_at != candidate.created_at
    ):
        raise WorkspaceProviderError("workspace manifest identity changed")
