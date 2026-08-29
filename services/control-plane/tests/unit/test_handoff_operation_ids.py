from uuid import UUID

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.gateway import (
    HANDOFF_OPERATION_NAMESPACE,
    GatewayOperationType,
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
