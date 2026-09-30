from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from sidera_control_plane.demo.bootstrap import _resources
from sidera_control_plane.demo.sample_repository import create_sample_repository
from sidera_control_plane.domain.gateway import (
    DeliveryOutcome,
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffInspectionOutcome,
    SessionContextBinding,
)
from sidera_control_plane.domain.handoffs import HandoffTransferMode
from sidera_control_plane.domain.modules import WorkspaceProviderCapabilityProfile
from sidera_control_plane.domain.workspace import workspace_manifest_digest
from tests.unit.interview_demo_support import manifest_for


def _adapter(tmp_path: Path, *, scenario: str = "handoff"):
    from sidera_control_plane.adapters.demo_worker import DemoWorkerAdapter

    manifest = manifest_for(tmp_path)
    manifest = manifest.model_copy(update={"scenario": scenario})
    repository_parent = tmp_path / "repositories"
    repository_parent.mkdir()
    repository, base_commit = create_sample_repository(repository_parent)
    manifest = manifest.model_copy(
        update={"repository": repository, "base_commit": base_commit, "code_commit": base_commit}
    )
    instance = _resources(manifest).instances["worker_primary"]
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    return manifest, DemoWorkerAdapter(manifest, instance, lambda _workspace: workspace_root)


def _session_inputs(manifest, tmp_path: Path, *, execution_id=None):
    from tests.unit.test_interview_worker import (
        _spec_and_workspace,
        _with_execution_context,
    )

    spec, workspace = _spec_and_workspace(manifest, tmp_path, execution_id=execution_id)
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir(exist_ok=True)
    return _with_execution_context(spec), workspace


def test_demo_workspace_declares_exact_handoff_capabilities(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    workspace = _resources(manifest).definitions["workspace"].capabilities

    assert isinstance(workspace, WorkspaceProviderCapabilityProfile)
    assert workspace.workspace_handoff_versions == (1,)
    assert workspace.workspace_transfer_modes == (HandoffTransferMode.REUSE_EXACT,)
    assert workspace.handoff_inspection is True


def test_demo_handoff_checkpoint_is_replayable_and_closes_source_writer(tmp_path: Path) -> None:
    manifest, adapter = _adapter(tmp_path)
    spec, workspace = _session_inputs(manifest, tmp_path)
    start_operation_id = uuid4()
    source = adapter.start_session(spec, workspace, operation_id=start_operation_id)
    tuple(adapter.read_events(source))

    transition_id = uuid4()
    operation_id = uuid4()
    checkpoint = adapter.capture_handoff_checkpoint(
        source,
        transition_id=transition_id,
        source_input_cutoff_sequence=1,
        operation_id=operation_id,
    )
    replay = adapter.capture_handoff_checkpoint(
        source,
        transition_id=transition_id,
        source_input_cutoff_sequence=1,
        operation_id=operation_id,
    )
    assert replay == checkpoint
    assert checkpoint.accepted_sequence >= 1
    assert checkpoint.context_package_id == spec.context_package.metadata.package_id
    assert checkpoint.context_package_hash == spec.context_package.metadata.package_hash
    assert checkpoint.context_version == spec.context_package.metadata.context_version
    assert checkpoint.workspace_digest == workspace_manifest_digest(workspace)
    assert checkpoint.evidence_refs

    inspection = adapter.inspect_handoff_operation(source, operation_id=operation_id)
    assert inspection.outcome is HandoffInspectionOutcome.SUCCEEDED
    assert inspection.accepted_sequence == checkpoint.accepted_sequence

    source_events = tuple(adapter.read_events(source))
    assert all(event.sequence <= checkpoint.accepted_sequence for event in source_events)
    blocked_input = adapter.send_input(
        source,
        "继续处理",
        (),
        operation_id=uuid4(),
    )
    assert blocked_input.outcome is DeliveryOutcome.BLOCKED
    assert blocked_input.failure_code == "handoff_checkpointed"

    from sidera_control_plane.adapters.demo_worker import DemoWorkerAdapter

    restarted_adapter = DemoWorkerAdapter(
        manifest,
        _resources(manifest).instances["worker_primary"],
        lambda _workspace: tmp_path / "workspace",
    )
    restarted = restarted_adapter.start_session(
        spec,
        workspace,
        operation_id=start_operation_id,
    )
    assert restarted.execution_session_id == source.execution_session_id
    replayed = restarted_adapter.capture_handoff_checkpoint(
        restarted,
        transition_id=transition_id,
        source_input_cutoff_sequence=1,
        operation_id=operation_id,
    )
    assert replayed == checkpoint

    stopped = adapter.stop_session(
        source,
        operation_id=gateway_operation_id(spec.execution_id, GatewayOperationType.STOP_SESSION),
    )
    assert stopped.outcome is DeliveryOutcome.DELIVERED
    assert stopped.accepted_sequence == checkpoint.accepted_sequence


def test_demo_handoff_target_requires_matching_fencing_writer_enable(tmp_path: Path) -> None:
    manifest, adapter = _adapter(tmp_path)
    source_spec, source_workspace = _session_inputs(manifest, tmp_path)
    source = adapter.start_session(source_spec, source_workspace, operation_id=uuid4())
    tuple(adapter.read_events(source))
    adapter.capture_handoff_checkpoint(
        source,
        transition_id=uuid4(),
        source_input_cutoff_sequence=1,
        operation_id=uuid4(),
    )

    target_execution_id = uuid4()
    target_spec, target_workspace = _session_inputs(
        manifest,
        tmp_path,
        execution_id=target_execution_id,
    )
    target_spec = replace(target_spec, worker_id=manifest.worker_ids["replacement"])
    target = adapter.start_session(
        target_spec,
        target_workspace,
        operation_id=uuid4(),
    )
    assert isinstance(target.context_binding, SessionContextBinding)
    assert target.context_binding.consumer_execution_id == target_execution_id

    bad = adapter.enable_handoff_writer(
        target,
        fencing_token=2,
        operation_id=uuid4(),
    )
    assert bad.outcome is DeliveryOutcome.BLOCKED
    assert bad.failure_code == "handoff_fencing_mismatch"

    enabled = adapter.enable_handoff_writer(
        target,
        fencing_token=1,
        operation_id=uuid4(),
    )
    assert enabled.outcome is DeliveryOutcome.DELIVERED
    assert enabled.accepted_sequence is not None

    target_events = tuple(adapter.read_events(target))
    assert any(event.event_type == "handoff.writer_enabled" for event in target_events)
