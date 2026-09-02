from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from sidera_control_plane.adapters.git_workspace import (
    GitWorktreeWorkspaceManager,
    WorkspaceProviderError,
)
from sidera_control_plane.domain.gateway import handoff_step_operation_id
from sidera_control_plane.domain.handoff_evidence import (
    HandoffInspectionOutcome,
    HandoffWorkspaceTargetSpec,
)
from sidera_control_plane.domain.handoffs import HandoffPhase
from sidera_control_plane.domain.workspace import WorkspaceLifecycle
from tests.integration.test_git_workspace_integration import _repository, _spec

pytestmark = pytest.mark.integration


def _target(source, spec, *, execution_id=None, fencing_token=2):
    return HandoffWorkspaceTargetSpec(
        workspace_id=source.workspace_id,
        task_id=source.task_id,
        target_execution_id=execution_id or uuid4(),
        target_worker_id=uuid4(),
        adapter_module_ref=spec.adapter_module_ref,
        adapter_config_version=spec.adapter_config_version,
        workspace_module_ref=spec.workspace_module_ref,
        workspace_config_version=spec.workspace_config_version,
        repositories=(
            {
                "repository_ref": source.checkouts[0].repository_ref,
                "revision": source.checkouts[0].base_revision,
                "branch": source.checkouts[0].branch,
                "current_head": source.checkouts[0].current_head,
            },
        ),
        anticipated_fencing_token=fencing_token,
    )


def _active_manifest(manager, spec):
    manifest = manager.prepare(spec, operation_id=uuid4())
    manifest.mark_preparing()
    manifest.mark_ready()
    manifest.mark_active()
    return manifest


def test_reuse_exact_transfers_one_physical_worktree_and_inspects_by_operation(
    tmp_path: Path,
) -> None:
    repository, revision = _repository(tmp_path)
    root = tmp_path / "workspaces"
    root.mkdir()
    spec = _spec(repository, revision)
    manager = GitWorktreeWorkspaceManager(root)
    source = _active_manifest(manager, spec)

    freeze_operation = uuid4()
    frozen = manager.freeze(source, operation_id=freeze_operation)
    repeated_frozen = manager.freeze(source, operation_id=freeze_operation)

    target = _target(source, spec)
    transition_id = uuid4()
    transfer_operation = handoff_step_operation_id(
        transition_id,
        HandoffPhase.TRANSFER_WORKSPACE,
        logical_attempt=1,
        action="transfer_workspace",
    )
    target_manifest = manager.transfer_handoff(
        frozen,
        target,
        transition_id=transition_id,
        logical_attempt=1,
        operation_id=transfer_operation,
    )
    repeated_target = manager.transfer_handoff(
        frozen,
        target,
        transition_id=transition_id,
        logical_attempt=1,
        operation_id=transfer_operation,
    )

    assert frozen is source
    assert repeated_frozen is source
    assert source.lifecycle is WorkspaceLifecycle.FROZEN
    assert target_manifest.id != source.id
    assert target_manifest.execution_id == target.target_execution_id
    assert target_manifest.location_ref == source.location_ref
    assert target_manifest.lifecycle is WorkspaceLifecycle.READY
    assert repeated_target == target_manifest
    assert len(tuple(root.iterdir())) == 1
    assert str(repository) not in source.checkouts[0].repository_ref
    assert str(root) not in source.location_ref

    assert manager.inspect(source.location_ref) == target_manifest
    assert manager.inspect_manifest(source) == source
    inspection = manager.inspect_handoff_transfer(
        transition_id=transition_id,
        logical_attempt=1,
        operation_id=transfer_operation,
    )
    assert inspection.outcome is HandoffInspectionOutcome.SUCCEEDED
    assert inspection.source_manifest_id == source.id
    assert inspection.target_manifest_id == target_manifest.id


def test_provider_rejects_a_second_active_writer_at_shared_location(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    root = tmp_path / "workspaces"
    root.mkdir()
    spec = _spec(repository, revision)
    manager = GitWorktreeWorkspaceManager(root)
    source = _active_manifest(manager, spec)
    manager.freeze(source, operation_id=uuid4())
    target = _target(source, spec)
    transition_id = uuid4()
    operation_id = handoff_step_operation_id(
        transition_id,
        HandoffPhase.TRANSFER_WORKSPACE,
        logical_attempt=1,
        action="transfer_workspace",
    )
    target_manifest = manager.transfer_handoff(
        source,
        target,
        transition_id=transition_id,
        logical_attempt=1,
        operation_id=operation_id,
    )
    target_manifest.mark_active()

    with pytest.raises(WorkspaceProviderError, match="active"):
        manager.transfer_handoff(
            source,
            _target(source, spec, fencing_token=2),
            transition_id=uuid4(),
            logical_attempt=1,
            operation_id=uuid4(),
        )


def test_provider_rejects_reusing_transfer_operation_for_a_new_target(
    tmp_path: Path,
) -> None:
    repository, revision = _repository(tmp_path)
    root = tmp_path / "workspaces"
    root.mkdir()
    spec = _spec(repository, revision)
    manager = GitWorktreeWorkspaceManager(root)
    source = _active_manifest(manager, spec)
    manager.freeze(source, operation_id=uuid4())
    target = _target(source, spec)
    transition_id = uuid4()
    operation_id = handoff_step_operation_id(
        transition_id,
        HandoffPhase.TRANSFER_WORKSPACE,
        logical_attempt=1,
        action="transfer_workspace",
    )
    manager.transfer_handoff(
        source,
        target,
        transition_id=transition_id,
        logical_attempt=1,
        operation_id=operation_id,
    )

    with pytest.raises(WorkspaceProviderError, match="identity"):
        manager.transfer_handoff(
            source,
            replace(target, target_worker_id=uuid4()),
            transition_id=transition_id,
            logical_attempt=1,
            operation_id=operation_id,
        )


def test_provider_rejects_reusing_execution_location_for_a_new_owner(
    tmp_path: Path,
) -> None:
    repository, revision = _repository(tmp_path)
    root = tmp_path / "workspaces"
    root.mkdir()
    spec = _spec(repository, revision)
    manager = GitWorktreeWorkspaceManager(root)
    manager.prepare(spec, operation_id=uuid4())

    with pytest.raises(WorkspaceProviderError, match="identity"):
        manager.prepare(
            replace(spec, task_id=uuid4()),
            operation_id=uuid4(),
        )


def test_unknown_handoff_operation_is_absent_without_ambiguous_location_lookup(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    root = tmp_path / "workspaces"
    root.mkdir()
    manager = GitWorktreeWorkspaceManager(root)

    inspection = manager.inspect_handoff_transfer(
        transition_id=uuid4(),
        logical_attempt=1,
        operation_id=uuid4(),
    )

    assert inspection.outcome is HandoffInspectionOutcome.ABSENT
    assert not manager._manifest_ids_by_location
