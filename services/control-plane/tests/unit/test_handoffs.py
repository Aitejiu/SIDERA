from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError
from sidera_control_plane.domain.handoffs import (
    ACTIVE_HANDOFF_TRANSITION_STATUSES,
    HandoffCandidateInspection,
    HandoffCandidateList,
    HandoffPhase,
    HandoffProposal,
    HandoffProposalPayload,
    HandoffProposalStatus,
    HandoffSourceChannel,
    HandoffTransferMode,
    HandoffTransition,
    HandoffTransitionStatus,
    HandoffWriterState,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("30000000-0000-0000-0000-000000000001")
SOURCE_EXECUTION_ID = UUID("40000000-0000-0000-0000-000000000001")
SOURCE_SESSION_ID = UUID("50000000-0000-0000-0000-000000000001")
SOURCE_MANIFEST_ID = UUID("60000000-0000-0000-0000-000000000001")
SOURCE_WORKER_ID = UUID("70000000-0000-0000-0000-000000000001")
TARGET_WORKER_ID = UUID("80000000-0000-0000-0000-000000000001")
HASH = "a" * 64


def transition(
    *,
    status: HandoffTransitionStatus = HandoffTransitionStatus.PENDING,
    phase: HandoffPhase = HandoffPhase.CHECKPOINT_SOURCE,
    writer_state: HandoffWriterState = HandoffWriterState.SOURCE_ACTIVE,
) -> HandoffTransition:
    return HandoffTransition(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        worker_item_id=WORKER_ITEM_ID,
        proposal_id=uuid4(),
        source_execution_id=SOURCE_EXECUTION_ID,
        source_session_id=SOURCE_SESSION_ID,
        source_workspace_manifest_id=SOURCE_MANIFEST_ID,
        target_worker_id=TARGET_WORKER_ID,
        target_runtime_snapshot={"worker_id": str(TARGET_WORKER_ID)},
        target_execution_id=None,
        target_session_id=None,
        target_workspace_manifest_id=None,
        handoff_context_bundle_id=None,
        target_context_version=3,
        target_context_package_id=None,
        target_context_package_hash=None,
        status=status,
        phase=phase,
        writer_state=writer_state,
        source_checkpoint_sequence=None,
        source_checkpoint_facts=None,
        source_stop_sequence=None,
        source_stop_facts=None,
        source_workspace_freeze_operation_id=None,
        source_workspace_freeze_facts=None,
        source_input_cutoff_sequence=4,
        failure_code=None,
        blocked_operation_id=None,
        logical_attempt=1,
        lease_owner=None,
        lease_expires_at=None,
        next_attempt_at=None,
        version=1,
        confirmed_by_type="user",
        confirmed_by_id="developer",
        confirmed_source_channel="web",
        confirmed_at=NOW,
        started_at=None,
        completed_at=None,
    )


def proposal_payload(
    *,
    source_runtime_snapshot: dict[str, object] | None = None,
    target_worker_id: UUID = TARGET_WORKER_ID,
) -> HandoffProposalPayload:
    return HandoffProposalPayload(
        proposal_schema_version=1,
        workspace_id=WORKSPACE_ID,
        root_task_id=UUID("90000000-0000-0000-0000-000000000001"),
        subtask_id=UUID("91000000-0000-0000-0000-000000000001"),
        worker_item_id=WORKER_ITEM_ID,
        source_execution_id=SOURCE_EXECUTION_ID,
        source_execution_version=2,
        source_session_id=SOURCE_SESSION_ID,
        source_session_version=3,
        source_session_last_sequence=4,
        source_worker_id=SOURCE_WORKER_ID,
        source_worker_version=5,
        source_runtime_snapshot=source_runtime_snapshot
        or {"worker_id": str(SOURCE_WORKER_ID), "runtime": {"version": 1}},
        source_workspace_manifest_id=SOURCE_MANIFEST_ID,
        source_workspace_manifest_version=6,
        source_context_package_id=UUID("92000000-0000-0000-0000-000000000001"),
        source_context_package_hash=HASH,
        source_worker_context_version=7,
        observed_plan_snapshot_id=UUID("93000000-0000-0000-0000-000000000001"),
        observed_plan_generation=8,
        observed_plan_head_version=9,
        target_worker_id=target_worker_id,
        target_worker_version=10,
        target_runtime_snapshot={
            "worker_id": str(target_worker_id),
            "runtime": {"version": 2},
        },
        target_capability_snapshot_hash=HASH,
        target_policy_reason={"rank": 1},
        target_policy_facts={"healthy": True},
        transfer_mode=HandoffTransferMode.REUSE_EXACT,
        handoff_reason="source worker is unavailable",
        handoff_instruction="continue from the accepted checkpoint",
        input_plan={"safe": ["input-1"]},
        impact_summary={"scope": "worker_item"},
    )


def proposal(
    *,
    payload: HandoffProposalPayload | None = None,
    expires_at: datetime = NOW + timedelta(minutes=10),
) -> HandoffProposal:
    return HandoffProposal.create(
        id=uuid4(),
        payload=payload or proposal_payload(),
        created_by_type="user",
        created_by_id="developer",
        source_channel=HandoffSourceChannel.WEB,
        created_at=NOW,
        expires_at=expires_at,
    )


def test_handoff_phase_order_is_exact() -> None:
    assert tuple(item.value for item in HandoffPhase) == (
        "checkpoint_source",
        "stop_source",
        "freeze_source_workspace",
        "create_target_execution",
        "assemble_bundle",
        "transfer_workspace",
        "start_target_session",
        "acknowledge_context",
        "transfer_safe_inputs",
        "activate_target",
        "complete",
    )


def test_transition_rejects_phase_skip() -> None:
    current = transition()
    current.start(now=NOW)
    before = (current.phase, current.status, current.writer_state, current.version)

    with pytest.raises(InvalidTransition):
        current.advance(HandoffPhase.FREEZE_SOURCE_WORKSPACE, now=NOW)

    assert (
        current.phase,
        current.status,
        current.writer_state,
        current.version,
    ) == before


def test_converged_transition_cannot_change() -> None:
    current = transition()
    current.start(now=NOW)
    for phase in tuple(HandoffPhase)[1:]:
        current.advance(phase, now=NOW)
    current.converge(now=NOW)
    before = (
        current.phase,
        current.status,
        current.writer_state,
        current.failure_code,
        current.version,
    )

    with pytest.raises(InvalidTransition):
        current.advance(HandoffPhase.COMPLETE, now=NOW)
    with pytest.raises(InvalidTransition):
        current.block(failure_code="later", now=NOW)
    with pytest.raises(InvalidTransition):
        current.converge(now=NOW)

    assert (
        current.phase,
        current.status,
        current.writer_state,
        current.failure_code,
        current.version,
    ) == before


def test_failed_transition_still_owns_worker_item() -> None:
    assert HandoffTransitionStatus.FAILED in ACTIVE_HANDOFF_TRANSITION_STATUSES
    assert HandoffTransitionStatus.CONVERGED not in ACTIVE_HANDOFF_TRANSITION_STATUSES


def test_proposal_hash_is_stable_for_equivalent_mapping_order() -> None:
    first = proposal_payload(
        source_runtime_snapshot={
            "worker_id": str(SOURCE_WORKER_ID),
            "runtime": {"version": 1, "adapter": "fake"},
        }
    )
    second = proposal_payload(
        source_runtime_snapshot={
            "runtime": {"adapter": "fake", "version": 1},
            "worker_id": str(SOURCE_WORKER_ID),
        }
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.content_hash == second.content_hash
    assert (
        proposal(payload=first).proposal_hash == proposal(payload=second).proposal_hash
    )


def test_proposal_payload_is_deeply_immutable() -> None:
    payload = proposal_payload()

    with pytest.raises(TypeError):
        payload.source_runtime_snapshot["runtime"]["version"] = 9  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        payload.handoff_reason = "changed"  # type: ignore[misc]


def test_proposal_rejects_same_source_and_target_worker() -> None:
    with pytest.raises(ValidationError, match="differ from source"):
        proposal_payload(target_worker_id=SOURCE_WORKER_ID)


def test_proposal_requires_aware_timestamps_and_positive_ttl() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        proposal().mark_confirmed(now=datetime.fromisoformat("2026-08-29T12:01:00"))
    with pytest.raises(ValidationError, match="expires_at"):
        HandoffProposal.create(
            id=uuid4(),
            payload=proposal_payload(),
            created_by_type="user",
            created_by_id="developer",
            source_channel="web",
            created_at=NOW,
            expires_at=NOW,
        )


def test_proposal_lifecycle_is_single_step_and_versioned() -> None:
    confirmed = proposal()
    confirmed.mark_confirmed(now=NOW + timedelta(minutes=1))
    assert confirmed.status is HandoffProposalStatus.CONFIRMED
    assert confirmed.version == 2
    with pytest.raises(InvalidTransition):
        confirmed.mark_cancelled(now=NOW + timedelta(minutes=2))
    assert confirmed.version == 2

    stale = proposal()
    stale.mark_stale(reason="superseded_by_new_proposal", now=NOW)
    assert stale.status is HandoffProposalStatus.STALE
    assert stale.stale_reason == "superseded_by_new_proposal"
    with pytest.raises(InvalidTransition):
        stale.mark_stale(reason="again", now=NOW)

    cancelled = proposal()
    cancelled.mark_cancelled(now=NOW)
    assert cancelled.status is HandoffProposalStatus.CANCELLED

    expired = proposal(expires_at=NOW + timedelta(minutes=1))
    with pytest.raises(InvalidTransition, match="has not expired"):
        expired.mark_expired(now=NOW)
    expired.mark_expired(now=NOW + timedelta(minutes=1))
    assert expired.status is HandoffProposalStatus.EXPIRED


def test_candidate_list_rejects_duplicate_or_source_workers() -> None:
    candidate = HandoffCandidateInspection(
        worker_id=TARGET_WORKER_ID,
        display_name="Target",
        adapter_type="fake",
        eligible=True,
        recommended=True,
        ineligibility_code=None,
        runtime_snapshot={"worker_id": str(TARGET_WORKER_ID)},
        capability_snapshot_hash=HASH,
        policy_reason={"rank": 1},
        workspace_compatibility={"mode": "reuse_exact"},
    )
    candidates = HandoffCandidateList(
        workspace_id=WORKSPACE_ID,
        worker_item_id=WORKER_ITEM_ID,
        source_execution_id=SOURCE_EXECUTION_ID,
        source_session_id=SOURCE_SESSION_ID,
        source_worker_id=SOURCE_WORKER_ID,
        source_worker_item_version=1,
        candidates=(candidate,),
        recommended_worker_id=TARGET_WORKER_ID,
        blocking_codes=(),
        inspected_at=NOW,
    )
    assert candidates.recommended_worker_id == TARGET_WORKER_ID

    with pytest.raises(ValidationError, match="source worker"):
        HandoffCandidateList(
            workspace_id=WORKSPACE_ID,
            worker_item_id=WORKER_ITEM_ID,
            source_execution_id=SOURCE_EXECUTION_ID,
            source_session_id=SOURCE_SESSION_ID,
            source_worker_id=SOURCE_WORKER_ID,
            source_worker_item_version=1,
            candidates=(replace(candidate, worker_id=SOURCE_WORKER_ID),),
            recommended_worker_id=SOURCE_WORKER_ID,
            blocking_codes=(),
            inspected_at=NOW,
        )


def test_transition_create_copies_exact_source_cutoff_and_context_version() -> None:
    confirmed = proposal()
    confirmed.mark_confirmed(now=NOW)
    current = HandoffTransition.create(
        id=uuid4(),
        proposal=confirmed,
        confirmed_by_type="user",
        confirmed_by_id="developer",
        confirmed_at=NOW,
    )

    assert current.status is HandoffTransitionStatus.PENDING
    assert current.phase is HandoffPhase.CHECKPOINT_SOURCE
    assert current.writer_state is HandoffWriterState.SOURCE_ACTIVE
    assert current.source_input_cutoff_sequence == 4
    assert current.target_context_version == 7
    assert current.target_execution_id is None


def test_transition_phase_edges_assign_the_expected_writer_state() -> None:
    current = transition()
    current.start(now=NOW)
    expected = (
        (HandoffPhase.STOP_SOURCE, HandoffWriterState.SOURCE_STOP_REQUESTED),
        (HandoffPhase.FREEZE_SOURCE_WORKSPACE, HandoffWriterState.WRITER_NONE),
        (HandoffPhase.CREATE_TARGET_EXECUTION, HandoffWriterState.WRITER_NONE),
        (HandoffPhase.ASSEMBLE_BUNDLE, HandoffWriterState.WRITER_NONE),
        (HandoffPhase.TRANSFER_WORKSPACE, HandoffWriterState.WRITER_NONE),
        (HandoffPhase.START_TARGET_SESSION, HandoffWriterState.TARGET_STARTING),
        (HandoffPhase.ACKNOWLEDGE_CONTEXT, HandoffWriterState.TARGET_STARTING),
        (HandoffPhase.TRANSFER_SAFE_INPUTS, HandoffWriterState.TARGET_ACTIVE),
        (HandoffPhase.ACTIVATE_TARGET, HandoffWriterState.TARGET_ACTIVE),
        (HandoffPhase.COMPLETE, HandoffWriterState.TARGET_ACTIVE),
    )

    for phase, writer_state in expected:
        current.advance(phase, now=NOW)
        assert current.phase is phase
        assert current.writer_state is writer_state


def test_source_evidence_slots_are_write_once() -> None:
    current = transition()
    current.record_checkpoint(sequence=4, facts={"quiescent": True})
    with pytest.raises(InvalidTransition, match="write-once"):
        current.record_checkpoint(sequence=4, facts={"quiescent": True})
    current.record_stop(sequence=4, facts={"stopped": True})
    with pytest.raises(InvalidTransition, match="write-once"):
        current.record_stop(sequence=4, facts={"stopped": True})
    operation_id = uuid4()
    current.record_workspace_freeze(
        operation_id=operation_id, facts={"lifecycle": "frozen"}
    )
    with pytest.raises(InvalidTransition, match="write-once"):
        current.record_workspace_freeze(
            operation_id=operation_id, facts={"lifecycle": "frozen"}
        )


def test_blocked_unknown_requires_operation_evidence_and_unknown_writer() -> None:
    current = transition()
    current.start(now=NOW)
    current.block(
        failure_code="stop_response_lost",
        now=NOW,
        status=HandoffTransitionStatus.BLOCKED_UNKNOWN,
        blocked_operation_id=uuid4(),
    )

    assert current.status is HandoffTransitionStatus.BLOCKED_UNKNOWN
    assert current.writer_state is HandoffWriterState.UNKNOWN


def test_workspace_only_unknown_can_be_hydrated_with_no_writer() -> None:
    current = transition()
    hydrated = replace(
        current,
        phase=HandoffPhase.FREEZE_SOURCE_WORKSPACE,
        writer_state=HandoffWriterState.WRITER_NONE,
        source_stop_sequence=4,
        status=HandoffTransitionStatus.BLOCKED_UNKNOWN,
        blocked_operation_id=uuid4(),
        failure_code="freeze_response_lost",
    )

    assert hydrated.status is HandoffTransitionStatus.BLOCKED_UNKNOWN
    assert hydrated.writer_state is HandoffWriterState.WRITER_NONE


def test_workspace_only_unknown_requires_exact_source_stop() -> None:
    current = transition()
    current.record_checkpoint(sequence=4, facts={"quiescent": True})
    current.record_stop(sequence=4, facts={"stopped": True})
    current.start(now=NOW)
    current.advance(HandoffPhase.STOP_SOURCE, now=NOW)
    current.advance(HandoffPhase.FREEZE_SOURCE_WORKSPACE, now=NOW)
    current.block(
        failure_code="freeze_response_lost",
        now=NOW,
        status=HandoffTransitionStatus.BLOCKED_UNKNOWN,
        blocked_operation_id=uuid4(),
        workspace_only=True,
    )

    assert current.writer_state is HandoffWriterState.WRITER_NONE
