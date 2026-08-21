from uuid import uuid4

import pytest

from sidera_control_plane.api.schemas import CommandRequest


def _request(command_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "command_type": command_type,
        "target_type": "execution_session",
        "actor": {"type": "user", "id": "user:1"},
        "target_id": str(uuid4()),
        "expected_version": 1,
        "idempotency_key": f"command-{uuid4()}",
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def test_send_execution_input_requires_exact_session_and_preserves_text() -> None:
    plan_snapshot_id = uuid4()
    root_task_id = uuid4()
    session_id = uuid4()
    execution_id = uuid4()
    worker_id = uuid4()
    text = "  keep this text exactly  "
    request = CommandRequest.model_validate(
        _request(
            "SendExecutionInput",
            {
                "context": {
                    "plan_snapshot_id": str(plan_snapshot_id),
                    "root_task_id": str(root_task_id),
                    "work_item_key": "backend",
                    "worker_slot_key": "backend",
                    "worker_id": str(worker_id),
                    "execution_id": str(execution_id),
                    "session_id": str(session_id),
                },
                "client_idempotency_key": "client-1",
                "text": text,
                "attachment_refs": ["artifact://context/1"],
            },
        )
    )

    assert request.target_type == "execution_session"
    assert request.payload["context"]["execution_id"] == str(execution_id)
    assert request.payload["text"] == text
    assert request.payload["attachment_refs"] == ["artifact://context/1"]


@pytest.mark.parametrize(
    "command_type,payload",
    [
        (
            "UpdatePendingExecutionInput",
            {
                "execution_id": str(uuid4()),
                "client_idempotency_key": "client-1",
                "text": "updated",
            },
        ),
        (
            "CancelExecutionInput",
            {
                "execution_id": str(uuid4()),
                "client_idempotency_key": "client-1",
            },
        ),
        (
            "RetryBlockedExecutionInput",
            {
                "execution_id": str(uuid4()),
                "client_idempotency_key": "client-1",
            },
        ),
    ],
)
def test_pending_input_commands_require_execution_id_and_client_key(
    command_type: str, payload: dict[str, object]
) -> None:
    request = CommandRequest.model_validate(_request(command_type, payload))
    assert request.payload["client_idempotency_key"] == "client-1"


def test_input_schema_rejects_empty_text() -> None:
    with pytest.raises(ValueError):
        CommandRequest.model_validate(
            _request(
                "SendExecutionInput",
                {
                    "context": {
                        "plan_snapshot_id": str(uuid4()),
                        "root_task_id": str(uuid4()),
                        "work_item_key": "backend",
                        "worker_slot_key": "backend",
                        "worker_id": str(uuid4()),
                        "execution_id": str(uuid4()),
                        "session_id": str(uuid4()),
                    },
                    "client_idempotency_key": "client-1",
                    "text": "",
                },
            )
        )


def test_send_execution_input_requires_context_address() -> None:
    with pytest.raises(ValueError):
        CommandRequest.model_validate(
            _request(
                "SendExecutionInput",
                {
                    "client_idempotency_key": "client-1",
                    "text": "send without an address",
                },
            )
        )
