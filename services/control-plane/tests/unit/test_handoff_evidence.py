from dataclasses import fields, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.gateway import GatewayOperationType
from sidera_control_plane.domain.handoff_evidence import (
    HandoffCheckpointEvidence,
    HandoffContextBundle,
    HandoffInputAction,
    HandoffInspectionOutcome,
    HandoffOperationInspection,
    HandoffStopEvidence,
    HandoffWorkspaceTargetSpec,
    HandoffWorkspaceTransfer,
    HandoffWorkspaceTransferStatus,
    SessionContextBinding,
)
from sidera_control_plane.domain.handoff_recovery import (
    HandoffRecoveryDecision,
    HandoffRecoveryStrategy,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
HASH = "a" * 64
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("40000000-0000-0000-0000-000000000001")
TRANSITION_ID = UUID("50000000-0000-0000-0000-000000000001")
SOURCE_EXECUTION_ID = UUID("60000000-0000-0000-0000-000000000001")
SOURCE_SESSION_ID = UUID("70000000-0000-0000-0000-000000000001")
SOURCE_MANIFEST_ID = UUID("80000000-0000-0000-0000-000000000001")
TARGET_EXECUTION_ID = UUID("90000000-0000-0000-0000-000000000001")
TARGET_WORKER_ID = UUID("91000000-0000-0000-0000-000000000001")
TARGET_SESSION_ID = UUID("92000000-0000-0000-0000-000000000001")
PACKAGE_ID = UUID("93000000-0000-0000-0000-000000000001")


def checkpoint() -> HandoffCheckpointEvidence:
    return HandoffCheckpointEvidence(
        operation_id=uuid4(),
        transition_id=TRANSITION_ID,
        source_session_id=SOURCE_SESSION_ID,
        accepted_sequence=0,
        workspace_digest=HASH,
        context_package_id=PACKAGE_ID,
        context_package_hash=HASH,
        context_version=1,
        progress_summary_ref="artifact://progress/1",
        artifact_refs=("artifact://output/1",),
        evidence_refs=("evidence://checkpoint/1",),
        quiescent=True,
        produced_at=NOW,
    )


def target_spec(*, anticipated_fencing_token: int = 8) -> HandoffWorkspaceTargetSpec:
    return HandoffWorkspaceTargetSpec(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        target_execution_id=TARGET_EXECUTION_ID,
        target_worker_id=TARGET_WORKER_ID,
        adapter_module_ref=UUID("94000000-0000-0000-0000-000000000001"),
        adapter_config_version=2,
        workspace_module_ref=UUID("95000000-0000-0000-0000-000000000001"),
        workspace_config_version=3,
        repositories=({"repository_ref": "repo://main", "revision": HASH},),
        anticipated_fencing_token=anticipated_fencing_token,
    )


def workspace_transfer(*, target_fencing_token: int = 8) -> HandoffWorkspaceTransfer:
    return HandoffWorkspaceTransfer(
        transition_id=TRANSITION_ID,
        logical_attempt=1,
        operation_id=uuid4(),
        source_manifest_id=SOURCE_MANIFEST_ID,
        target_manifest_id=uuid4(),
        source_execution_id=SOURCE_EXECUTION_ID,
        target_execution_id=TARGET_EXECUTION_ID,
        source_fencing_token=7,
        target_fencing_token=target_fencing_token,
        location_ref_hash=HASH,
        repository_state_digest=HASH,
        status=HandoffWorkspaceTransferStatus.SUCCEEDED,
        failure_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


def bundle(**overrides: object) -> HandoffContextBundle:
    values: dict[str, object] = {
        "id": uuid4(),
        "schema_version": 1,
        "workspace_id": WORKSPACE_ID,
        "worker_item_id": WORKER_ITEM_ID,
        "handoff_transition_id": TRANSITION_ID,
        "logical_attempt": 1,
        "recovery_decision_id": None,
        "source_execution_id": SOURCE_EXECUTION_ID,
        "source_session_id": SOURCE_SESSION_ID,
        "source_checkpoint_sequence": 0,
        "source_last_accepted_sequence": 0,
        "source_context_package_id": PACKAGE_ID,
        "source_context_package_hash": HASH,
        "source_worker_context_version": 1,
        "target_execution_id": TARGET_EXECUTION_ID,
        "target_worker_id": TARGET_WORKER_ID,
        "target_runtime_snapshot_hash": HASH,
        "target_context_version": 1,
        "source_workspace_manifest_id": SOURCE_MANIFEST_ID,
        "source_workspace_version": 2,
        "workspace_location_ref_hash": HASH,
        "repository_state": (
            {
                "repository_ref": "repo://main",
                "base_revision": HASH,
                "current_head": HASH,
                "dirty_state_digest": HASH,
                "diff_ref": "artifact://diff/1",
            },
        ),
        "artifact_refs": ("artifact://output/1",),
        "evidence_refs": ("evidence://checkpoint/1",),
        "handoff_reason": "target worker has the required capability",
        "handoff_instruction": "continue from the checkpoint",
        "delivered_input_refs": (uuid4(),),
        "transferable_input_refs": (uuid4(),),
        "excluded_input_refs": (),
        "created_at": NOW,
    }
    values.update(overrides)
    return HandoffContextBundle.create(**values)  # type: ignore[arg-type]


def test_checkpoint_requires_quiescent_exact_session_and_package_hash() -> None:
    valid = checkpoint()
    assert valid.accepted_sequence == 0

    for changes in (
        {"quiescent": False},
        {"source_session_id": None},
        {"context_package_id": None},
        {"context_package_hash": None},
        {"accepted_sequence": -1},
        {"context_version": 0},
    ):
        with pytest.raises(ValidationError):
            replace(valid, **changes)


def test_stop_evidence_requires_matching_checkpoint_session_and_sequence() -> None:
    source_checkpoint = checkpoint()
    valid = HandoffStopEvidence(
        operation_id=uuid4(),
        source_session_id=SOURCE_SESSION_ID,
        accepted_sequence=0,
        stopped=True,
        produced_at=NOW,
    )
    valid.assert_matches(source_checkpoint)

    with pytest.raises(ValidationError):
        replace(valid, source_session_id=TARGET_SESSION_ID).assert_matches(
            source_checkpoint
        )
    with pytest.raises(ValidationError):
        replace(valid, accepted_sequence=1).assert_matches(source_checkpoint)


def test_workspace_target_spec_is_pre_lease_and_has_higher_fence() -> None:
    target = target_spec()
    assert "lease_id" not in {field.name for field in fields(target)}
    assert target.anticipated_fencing_token == 8

    with pytest.raises(ValidationError, match="higher"):
        workspace_transfer(target_fencing_token=7)
    with pytest.raises(ValidationError, match="higher"):
        workspace_transfer(target_fencing_token=6)


def test_session_context_binding_is_session_bound() -> None:
    binding = SessionContextBinding(
        operation_id=uuid4(),
        execution_session_id=TARGET_SESSION_ID,
        consumer_execution_id=TARGET_EXECUTION_ID,
        package_id=PACKAGE_ID,
        package_hash=HASH,
        context_version=1,
        acknowledged_at=NOW,
    )
    binding.assert_matches(
        execution_session_id=TARGET_SESSION_ID,
        consumer_execution_id=TARGET_EXECUTION_ID,
        package_id=PACKAGE_ID,
        package_hash=HASH,
        context_version=1,
        operation_id=binding.operation_id,
    )

    for field_name, value in (
        ("execution_session_id", SOURCE_SESSION_ID),
        ("consumer_execution_id", SOURCE_EXECUTION_ID),
        ("package_id", SOURCE_MANIFEST_ID),
        ("package_hash", "b" * 64),
        ("context_version", 2),
        ("operation_id", uuid4()),
    ):
        with pytest.raises(ValidationError):
            replace(binding, **{field_name: value}).assert_matches(
                execution_session_id=TARGET_SESSION_ID,
                consumer_execution_id=TARGET_EXECUTION_ID,
                package_id=PACKAGE_ID,
                package_hash=HASH,
                context_version=1,
                operation_id=binding.operation_id,
            )


@pytest.mark.parametrize(
    "bad_state",
    [
        {"workspace_path": "/tmp/sidera"},
        {"environment": "HOME=/tmp"},
        {"api_token": "secret"},
        {"provider_trace": "private"},
        {"content": "inline file body"},
    ],
)
def test_bundle_rejects_paths_secrets_raw_trace_and_inline_content(
    bad_state: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        bundle(repository_state=(bad_state,))

    valid = bundle()
    assert valid.bundle_hash == valid.content_hash
    assert valid.repository_state[0]["diff_ref"] == "artifact://diff/1"


def test_operation_inspection_uses_closed_outcomes() -> None:
    inspection = HandoffOperationInspection(
        operation_id=uuid4(),
        operation_type=GatewayOperationType.HANDOFF_CHECKPOINT,
        outcome=HandoffInspectionOutcome.SUCCEEDED,
        execution_id=SOURCE_EXECUTION_ID,
        session_id=SOURCE_SESSION_ID,
        accepted_sequence=0,
        context_binding=None,
        observed_at=NOW,
        evidence_refs=("evidence://checkpoint/1",),
    )
    assert inspection.outcome is HandoffInspectionOutcome.SUCCEEDED


def test_input_action_is_an_explicit_contract() -> None:
    assert HandoffInputAction.TRANSFER_QUEUED.value == "transfer_queued"


def test_recovery_decision_requires_actor_reason_evidence_and_version() -> None:
    decision = HandoffRecoveryDecision(
        id=uuid4(),
        handoff_transition_id=TRANSITION_ID,
        expected_transition_version=2,
        strategy=HandoffRecoveryStrategy.RETRY_CURRENT_PHASE,
        actor_type="user",
        actor_id="developer",
        source_channel="web",
        reason="the operation is proven absent",
        evidence_refs=("evidence://inspection/1",),
        replacement_target_snapshot=None,
        resulting_operation_refs=(),
        created_at=NOW,
    )
    assert decision.strategy is HandoffRecoveryStrategy.RETRY_CURRENT_PHASE

    for field_name, value in (
        ("actor_type", ""),
        ("actor_id", ""),
        ("reason", ""),
        ("evidence_refs", ()),
        ("expected_transition_version", 0),
    ):
        with pytest.raises(ValidationError):
            replace(decision, **{field_name: value})

    with pytest.raises(ValidationError, match="select_new_target"):
        replace(
            decision,
            strategy=HandoffRecoveryStrategy.SELECT_NEW_TARGET,
        )
    with pytest.raises(ValidationError, match="requires select_new_target"):
        replace(decision, replacement_target_snapshot={"worker_id": "worker-2"})
