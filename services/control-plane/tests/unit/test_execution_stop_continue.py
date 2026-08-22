from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.api.schemas import CommandRequest
from sidera_control_plane.application.execution_commands import _continuation_worker_id
from sidera_control_plane.domain.errors import InvalidTransition
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSession,
    ExecutionStatus,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
WORKER_ITEM_ID = UUID("73000000-0000-0000-0000-000000000001")


def _execution(*, worker_item_id: UUID | None = WORKER_ITEM_ID) -> Execution:
    execution_id = uuid4()
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=uuid4(),
        task_id=uuid4(),
        worker_item_id=worker_item_id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=2,
        purpose="Stop safely",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=None,
        not_before=NOW,
    )
    execution.assign(lease_id=uuid4(), worker_id=uuid4(), fencing_token=1)
    execution.start(
        lease_id=execution.active_lease_id or uuid4(),
        fencing_token=1,
        now=NOW,
    )
    return execution


def _request(
    command_type: str,
    target_type: str,
    payload: dict[str, object],
) -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "command_type": command_type,
        "target_type": target_type,
        "actor": {"type": "user", "id": "user:1"},
        "target_id": str(uuid4()),
        "expected_version": 1,
        "idempotency_key": f"command-{uuid4()}",
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def test_execution_can_request_stop_once_while_running() -> None:
    execution = _execution()
    previous_version = execution.version

    execution.request_stop()

    assert execution.status is ExecutionStatus.RUNNING
    assert execution.stop_requested is True
    assert execution.version == previous_version + 1
    with pytest.raises(InvalidTransition):
        execution.request_stop()


def test_stop_and_continue_command_shapes_are_exactly_targeted() -> None:
    stop = CommandRequest.model_validate(
        _request("StopExecution", "execution", {})
    )
    continue_request = CommandRequest.model_validate(
        _request(
            "ContinueExecutionSession",
            "execution_session",
            {"not_before": NOW.isoformat()},
        )
    )

    assert stop.target_type == "execution"
    assert continue_request.target_type == "execution_session"
    assert continue_request.payload["not_before"] == "2026-08-20T12:00:00Z"


def test_continue_rejects_an_execution_target() -> None:
    with pytest.raises(ValueError, match="execution_session"):
        CommandRequest.model_validate(
            _request("ContinueExecutionSession", "execution", {"not_before": NOW.isoformat()})
        )


def test_continuation_is_pinned_to_the_original_session_worker() -> None:
    assigned_worker_id = uuid4()
    session_worker_id = uuid4()
    execution = _execution()
    execution.assigned_worker_id = assigned_worker_id
    session = ExecutionSession.start(
        session_id=uuid4(),
        workspace_id=execution.workspace_id,
        execution_id=execution.id,
        worker_id=session_worker_id,
        adapter_session_id="adapter-session",
        now=NOW,
    )

    assert _continuation_worker_id(execution, session) == session_worker_id


def test_continuation_source_keeps_worker_item_binding() -> None:
    execution = _execution()

    assert execution.worker_item_id == WORKER_ITEM_ID
