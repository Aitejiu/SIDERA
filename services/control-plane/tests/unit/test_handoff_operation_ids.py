from uuid import UUID

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.gateway import (
    HANDOFF_OPERATION_NAMESPACE,
    GatewayOperationType,
    gateway_operation_id,
    handoff_step_operation_id,
)
from sidera_control_plane.domain.handoffs import HandoffPhase

TRANSITION_ID = UUID("50000000-0000-0000-0000-000000000001")


def test_handoff_operation_id_is_stable_for_same_logical_step() -> None:
    first = handoff_step_operation_id(
        TRANSITION_ID,
        HandoffPhase.CHECKPOINT_SOURCE,
        logical_attempt=1,
        action="checkpoint",
    )
    second = handoff_step_operation_id(
        TRANSITION_ID,
        HandoffPhase.CHECKPOINT_SOURCE,
        logical_attempt=1,
        action="checkpoint",
    )

    assert first == second
    assert first != handoff_step_operation_id(
        TRANSITION_ID,
        HandoffPhase.CHECKPOINT_SOURCE,
        logical_attempt=2,
        action="checkpoint",
    )
    assert first != handoff_step_operation_id(
        TRANSITION_ID,
        HandoffPhase.STOP_SOURCE,
        logical_attempt=1,
        action="checkpoint",
    )
    assert HANDOFF_OPERATION_NAMESPACE


def test_handoff_operation_id_rejects_unknown_action_and_attempt() -> None:
    with pytest.raises(ValidationError):
        handoff_step_operation_id(
            TRANSITION_ID,
            HandoffPhase.CHECKPOINT_SOURCE,
            logical_attempt=0,
            action="checkpoint",
        )
    with pytest.raises(ValidationError):
        handoff_step_operation_id(
            TRANSITION_ID,
            HandoffPhase.CHECKPOINT_SOURCE,
            logical_attempt=1,
            action="start_session",
        )


def test_gateway_has_distinct_handoff_operation_types() -> None:
    assert GatewayOperationType.HANDOFF_CHECKPOINT.value == "handoff_checkpoint"
    assert GatewayOperationType.HANDOFF_ENABLE_WRITER.value == "handoff_enable_writer"


def test_native_stop_step_keeps_the_saved_transition_and_logical_attempt() -> None:
    first = handoff_step_operation_id(
        TRANSITION_ID, HandoffPhase.STOP_SOURCE, logical_attempt=1, action="stop_source"
    )
    assert first == handoff_step_operation_id(
        TRANSITION_ID, HandoffPhase.STOP_SOURCE, logical_attempt=1, action="stop_source"
    )
    assert first != handoff_step_operation_id(
        TRANSITION_ID, HandoffPhase.STOP_SOURCE, logical_attempt=2, action="stop_source"
    )
    assert first != handoff_step_operation_id(
        UUID("50000000-0000-0000-0000-000000000002"),
        HandoffPhase.STOP_SOURCE,
        logical_attempt=1,
        action="stop_source",
    )


@pytest.mark.parametrize(
    ("phase", "action", "expected"),
    [
        (
            HandoffPhase.CHECKPOINT_SOURCE,
            "checkpoint",
            "0fb0f1ce-72ea-5538-b220-1a39ec48ccc7",
        ),
        (
            HandoffPhase.FREEZE_SOURCE_WORKSPACE,
            "freeze_workspace",
            "d1f285fd-a1de-50a5-a94e-21052d546b9e",
        ),
        (
            HandoffPhase.TRANSFER_WORKSPACE,
            "transfer_workspace",
            "ba085852-abba-57df-8d27-8ce8e1cc38de",
        ),
        (
            HandoffPhase.ACTIVATE_TARGET,
            "enable_writer",
            "4cd77d93-023a-501c-898f-2ba37b45c260",
        ),
    ],
)
def test_previous_handoff_step_ids_remain_frozen(phase, action, expected) -> None:
    assert handoff_step_operation_id(
        TRANSITION_ID, phase, logical_attempt=1, action=action
    ) == UUID(expected)


def test_legacy_stop_keeps_its_canonical_execution_identity() -> None:
    assert gateway_operation_id(
        TRANSITION_ID, GatewayOperationType.STOP_SESSION
    ) == UUID("d0fe539d-b187-500b-825b-4f2df13a4429")
