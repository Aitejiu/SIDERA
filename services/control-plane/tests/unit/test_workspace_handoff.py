from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sidera_control_plane.application.workspace_handoff import (
    WorkspaceHandoffError,
    WorkspaceHandoffService,
)
from sidera_control_plane.domain.gateway import handoff_step_operation_id
from sidera_control_plane.domain.handoff_evidence import (
    HandoffInspectionOutcome,
    HandoffWorkspaceInspection,
    HandoffWorkspaceTargetSpec,
    HandoffWorkspaceTransferStatus,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffProposal,
    HandoffProposalPayload,
    HandoffSourceChannel,
    HandoffTransferMode,
    HandoffTransition,
)
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
    WorkspaceLifecycle,
    workspace_manifest_digest,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _transition(*, stopped: bool = True) -> HandoffTransition:
    workspace_id = uuid4()
    source_execution_id = uuid4()
    source_session_id = uuid4()
    source_worker_id = uuid4()
    payload = HandoffProposalPayload(
        proposal_schema_version=1,
        workspace_id=workspace_id,
        root_task_id=uuid4(),
        subtask_id=uuid4(),
        worker_item_id=uuid4(),
        source_execution_id=source_execution_id,
        source_execution_version=1,
        source_session_id=source_session_id,
        source_session_version=1,
        source_session_last_sequence=3,
        source_worker_id=source_worker_id,
        source_worker_version=1,
        source_runtime_snapshot={"adapter": "fake"},
        source_workspace_manifest_id=uuid4(),
        source_workspace_manifest_version=1,
        source_context_package_id=uuid4(),
        source_context_package_hash="a" * 64,
        source_worker_context_version=1,
        observed_plan_snapshot_id=uuid4(),
        observed_plan_generation=1,
        observed_plan_head_version=1,
        target_worker_id=uuid4(),
        target_worker_version=1,
        target_runtime_snapshot={"adapter": "fake"},
        target_capability_snapshot_hash="b" * 64,
        target_policy_reason={"reason": "test"},
        target_policy_facts={"eligible": True},
        transfer_mode=HandoffTransferMode.REUSE_EXACT,
        handoff_reason="resume elsewhere",
        handoff_instruction="continue from the frozen workspace",
        input_plan={"mode": "safe"},
        impact_summary={"scope": "worker_item"},
    )
    proposal = HandoffProposal.create(
        id=uuid4(),
        payload=payload,
        created_by_type="user",
        created_by_id="unit",
        source_channel=HandoffSourceChannel.WEB,
        created_at=NOW,
        expires_at=NOW.replace(hour=13),
    )
    proposal.mark_confirmed(now=NOW)
    transition = HandoffTransition.create(
        id=uuid4(),
        proposal=proposal,
        confirmed_by_type="user",
        confirmed_by_id="unit",
        confirmed_at=NOW,
    )
    transition.start(now=NOW)
    transition.advance(HandoffPhase.STOP_SOURCE, now=NOW)
    transition.advance(HandoffPhase.FREEZE_SOURCE_WORKSPACE, now=NOW)
    checkpoint = {
        "operation_id": str(
            handoff_step_operation_id(
                transition.id,
                HandoffPhase.CHECKPOINT_SOURCE,
                logical_attempt=1,
                action="checkpoint",
            )
        ),
        "transition_id": str(transition.id),
        "source_session_id": str(source_session_id),
        "accepted_sequence": 3,
        "workspace_digest": "pending",
        "context_package_id": str(payload.source_context_package_id),
        "context_package_hash": payload.source_context_package_hash,
        "context_version": 1,
        "progress_summary_ref": "fake-progress://3",
        "artifact_refs": [],
        "evidence_refs": [],
        "quiescent": True,
        "produced_at": NOW.isoformat(),
        "schema_version": 1,
    }
    transition.record_checkpoint(sequence=3, facts=checkpoint)
    stop = {
        "operation_id": str(uuid4()),
        "source_session_id": str(source_session_id),
        "accepted_sequence": 3,
        "stopped": True,
        "produced_at": NOW.isoformat(),
    }
    transition.record_stop(sequence=3, facts=stop)
    return transition


def _source_manifest(transition: HandoffTransition) -> ExecutionWorkspaceManifest:
    manifest = ExecutionWorkspaceManifest.create(
        id=transition.source_workspace_manifest_id,
        workspace_id=transition.workspace_id,
        task_id=transition.worker_item_id,
        execution_id=transition.source_execution_id,
        checkouts=(
            WorkspaceCheckout(
                repository_ref="repo://sidera",
                base_revision="c" * 40,
                branch="sidera/execution/source",
                current_head="c" * 40,
            ),
        ),
        location_ref="fake-workspace://shared/source",
        retention_seconds=3600,
        fencing_token=7,
        created_at=NOW,
    )
    manifest.mark_preparing()
    manifest.mark_ready()
    manifest.mark_active()
    transition.source_checkpoint_facts = {
        **transition.source_checkpoint_facts,
        "workspace_digest": workspace_manifest_digest(manifest),
    }
    return manifest


def _target(transition: HandoffTransition) -> HandoffWorkspaceTargetSpec:
    return HandoffWorkspaceTargetSpec(
        workspace_id=transition.workspace_id,
        task_id=transition.worker_item_id,
        target_execution_id=uuid4(),
        target_worker_id=transition.target_worker_id,
        adapter_module_ref=uuid4(),
        adapter_config_version=1,
        workspace_module_ref=uuid4(),
        workspace_config_version=1,
        repositories=(
            {
                "repository_ref": "repo://sidera",
                "base_revision": "c" * 40,
                "branch": "sidera/execution/source",
                "current_head": "c" * 40,
            },
        ),
        anticipated_fencing_token=8,
    )


class RecordingProvider:
    def __init__(self) -> None:
        self.freeze_calls = 0
        self.transfer_calls = 0
        self.frozen: ExecutionWorkspaceManifest | None = None
        self.target: ExecutionWorkspaceManifest | None = None

    def freeze(self, manifest, *, operation_id):
        del operation_id
        self.freeze_calls += 1
        manifest.freeze(now=NOW)
        self.frozen = manifest
        return manifest

    def inspect_manifest(self, manifest):
        return self.frozen or manifest

    def transfer_handoff(
        self,
        source_manifest,
        target,
        *,
        transition_id,
        logical_attempt,
        operation_id,
    ):
        del transition_id, logical_attempt, operation_id
        self.transfer_calls += 1
        result = ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=target.workspace_id,
            task_id=target.task_id,
            execution_id=target.target_execution_id,
            checkouts=source_manifest.checkouts,
            location_ref=source_manifest.location_ref,
            retention_seconds=source_manifest.retention_seconds,
            fencing_token=target.anticipated_fencing_token,
            created_at=NOW,
        )
        result.mark_preparing()
        result.mark_ready()
        self.target = result
        return result

    def inspect_handoff_transfer(
        self, *, transition_id, logical_attempt, operation_id
    ):
        return HandoffWorkspaceInspection(
            transition_id=transition_id,
            logical_attempt=logical_attempt,
            operation_id=operation_id,
            outcome=(
                HandoffInspectionOutcome.SUCCEEDED
                if self.target is not None
                else HandoffInspectionOutcome.ABSENT
            ),
            source_manifest_id=self.frozen.id if self.frozen else None,
            target_manifest_id=self.target.id if self.target else None,
            source_lifecycle=self.frozen.lifecycle.value if self.frozen else None,
            target_lifecycle=self.target.lifecycle.value if self.target else None,
            observed_at=NOW,
            evidence_refs=("fake-evidence://handoff",),
        )


class UnknownTransferProvider(RecordingProvider):
    def transfer_handoff(self, *args, **kwargs):
        self.transfer_calls += 1
        raise TimeoutError("provider result is unknown")


def test_freeze_requires_exact_stop_and_uses_stable_operation_identity() -> None:
    transition = _transition(stopped=True)
    source = _source_manifest(transition)
    provider = RecordingProvider()
    service = WorkspaceHandoffService(provider)

    result = service.freeze_source(
        transition=transition,
        source_manifest=source,
        now=NOW,
    )

    expected = handoff_step_operation_id(
        transition.id,
        HandoffPhase.FREEZE_SOURCE_WORKSPACE,
        logical_attempt=transition.logical_attempt,
        action="freeze_workspace",
    )
    assert result is source
    assert result.lifecycle is WorkspaceLifecycle.FROZEN
    assert transition.source_workspace_freeze_operation_id == expected
    assert transition.source_workspace_freeze_facts is not None
    assert provider.freeze_calls == 1

    repeated = service.freeze_source(
        transition=transition,
        source_manifest=source,
        now=NOW,
    )
    assert repeated is source
    assert provider.freeze_calls == 1


def test_freeze_unknown_reconciles_exact_source_without_replay() -> None:
    transition = _transition(stopped=True)
    source = _source_manifest(transition)
    provider = RecordingProvider()
    provider.freeze = lambda manifest, operation_id: (_ for _ in ()).throw(  # type: ignore[method-assign]
        TimeoutError("unknown")
    )
    frozen_copy = source
    source.freeze(now=NOW)
    provider.frozen = frozen_copy
    service = WorkspaceHandoffService(provider)

    result = service.freeze_source(transition=transition, source_manifest=source, now=NOW)

    assert result.lifecycle is WorkspaceLifecycle.FROZEN
    assert transition.source_workspace_freeze_facts is not None
    assert provider.freeze_calls == 0


def test_transfer_creates_ready_target_and_is_idempotent() -> None:
    transition = _transition(stopped=True)
    source = _source_manifest(transition)
    provider = RecordingProvider()
    service = WorkspaceHandoffService(provider)
    service.freeze_source(transition=transition, source_manifest=source, now=NOW)
    target = _target(transition)

    transfer = service.transfer(
        transition=transition,
        source_manifest=source,
        target=target,
        now=NOW,
    )
    repeated = service.transfer(
        transition=transition,
        source_manifest=source,
        target=target,
        now=NOW,
    )

    assert transfer.status is HandoffWorkspaceTransferStatus.SUCCEEDED
    assert transfer.target_manifest_id is not None
    assert repeated == transfer
    assert provider.transfer_calls == 1
    assert provider.target is not None
    assert provider.target.lifecycle is WorkspaceLifecycle.READY
    assert provider.target.id != source.id
    assert provider.target.execution_id == target.target_execution_id
    assert provider.target.location_ref == source.location_ref
    assert provider.target.fencing_token == source.fencing_token + 1
    assert source.lifecycle is WorkspaceLifecycle.FROZEN


def test_unknown_transfer_is_retained_and_never_replayed() -> None:
    transition = _transition(stopped=True)
    source = _source_manifest(transition)
    provider = UnknownTransferProvider()
    service = WorkspaceHandoffService(provider)
    service.freeze_source(transition=transition, source_manifest=source, now=NOW)
    target = _target(transition)

    transfer = service.transfer(
        transition=transition,
        source_manifest=source,
        target=target,
        now=NOW,
    )
    repeated = service.transfer(
        transition=transition,
        source_manifest=source,
        target=target,
        now=NOW,
    )

    assert transfer.status is HandoffWorkspaceTransferStatus.UNKNOWN
    assert repeated == transfer
    assert provider.transfer_calls == 1


def test_inspect_is_bound_to_transition_attempt_and_operation() -> None:
    transition = _transition(stopped=True)
    source = _source_manifest(transition)
    provider = RecordingProvider()
    service = WorkspaceHandoffService(provider)
    service.freeze_source(transition=transition, source_manifest=source, now=NOW)
    target = _target(transition)
    transfer = service.transfer(
        transition=transition,
        source_manifest=source,
        target=target,
        now=NOW,
    )

    inspection = service.inspect(
        transition_id=transition.id,
        workspace_id=transition.workspace_id,
        logical_attempt=transition.logical_attempt,
        operation_id=transfer.operation_id,
        now=NOW,
    )

    assert inspection.outcome is HandoffInspectionOutcome.SUCCEEDED
    assert inspection.transition_id == transition.id
    assert inspection.logical_attempt == transition.logical_attempt
    assert inspection.operation_id == transfer.operation_id


def test_freeze_rejects_wrong_workspace_owner() -> None:
    transition = _transition(stopped=True)
    source = _source_manifest(transition)
    source.workspace_id = uuid4()

    with pytest.raises(WorkspaceHandoffError, match="workspace owner"):
        WorkspaceHandoffService(RecordingProvider()).freeze_source(
            transition=transition,
            source_manifest=source,
            now=NOW,
        )
