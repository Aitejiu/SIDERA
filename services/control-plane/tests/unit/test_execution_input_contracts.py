from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sidera_control_plane.domain.errors import InvalidTransition
from sidera_control_plane.domain.inputs import (
    ExecutionInput,
    ExecutionInputStatus,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def make_input() -> ExecutionInput:
    return ExecutionInput.accept(
        id=uuid4(),
        workspace_id=uuid4(),
        execution_id=uuid4(),
        session_id=uuid4(),
        client_idempotency_key="input-001",
        text="Please fix the failing test",
        attachment_refs=("artifact://context",),
        now=NOW,
    )


def test_accepted_input_can_be_edited_and_cancelled() -> None:
    item = make_input()
    item.update_text("Please fix the exact failing test")
    item.cancel(now=NOW)

    assert item.text == "Please fix the exact failing test"
    assert item.status is ExecutionInputStatus.CANCELLED


def test_dispatching_input_is_immutable_and_unknown_delivery_is_blocked() -> None:
    item = make_input()
    item.queue()
    item.begin_dispatch(operation_id=uuid4())

    with pytest.raises(InvalidTransition):
        item.update_text("late edit")

    item.block_unknown(failure_code="response_lost")
    assert item.status is ExecutionInputStatus.BLOCKED


def test_blocked_input_requires_explicit_retry_before_delivery() -> None:
    item = make_input()
    item.queue()
    item.begin_dispatch(operation_id=uuid4())
    item.block_unknown(failure_code="response_lost")
    item.retry_blocked()
    item.begin_dispatch(operation_id=uuid4())
    item.deliver()

    assert item.status is ExecutionInputStatus.DELIVERED
