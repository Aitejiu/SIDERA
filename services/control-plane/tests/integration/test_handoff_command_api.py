from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.api.schemas import CommandRequest
from sidera_control_plane.config import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as test_client:
        yield test_client


def _request(
    workspace_id: UUID,
    command_type: str,
    target_type: str,
    target_id: UUID,
    expected_version: int,
    payload: dict[str, Any],
    *,
    actor: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": target_type,
        "actor": actor or {"type": "local", "id": "operator"},
        "target_id": str(target_id),
        "expected_version": expected_version,
        "idempotency_key": f"{command_type}:{uuid4()}",
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def test_handoff_command_payloads_bind_to_exact_aggregate_targets() -> None:
    workspace_id = uuid4()
    worker_item_id = uuid4()
    proposal_id = uuid4()
    transition_id = uuid4()

    prepare = CommandRequest.model_validate(
        _request(
            workspace_id,
            "PrepareWorkerHandoff",
            "worker_item",
            worker_item_id,
            7,
            {
                "target_worker_id": uuid4(),
                "handoff_reason": "specialist needed",
                "handoff_instruction": "continue from the checkpoint",
                "input_plan": {},
                "proposal_ttl_seconds": 900,
            },
        )
    )
    assert prepare.payload["target_worker_id"]

    confirm = CommandRequest.model_validate(
        _request(
            workspace_id,
            "ConfirmWorkerHandoff",
            "handoff_proposal",
            proposal_id,
            2,
            {
                "proposal_id": proposal_id,
                "expected_proposal_version": 2,
                "expected_proposal_hash": "a" * 64,
                "expected_worker_item_version": 7,
            },
        )
    )
    assert confirm.target_type == "handoff_proposal"

    cancel = CommandRequest.model_validate(
        _request(
            workspace_id,
            "CancelWorkerHandoff",
            "handoff_proposal",
            proposal_id,
            2,
            {"reason": "operator changed direction"},
        )
    )
    assert cancel.payload == {"reason": "operator changed direction"}

    recover = CommandRequest.model_validate(
        _request(
            workspace_id,
            "RecoverWorkerHandoff",
            "handoff_transition",
            transition_id,
            3,
            {
                "transition_id": transition_id,
                "expected_transition_version": 3,
                "strategy": "retry_current_phase",
                "reason": "boundary confirmed absent",
                "evidence_refs": ["handoff-recovery://evidence/1"],
            },
        )
    )
    assert "replacement_target_worker_id" not in recover.payload


def test_handoff_commands_reject_non_local_actor_pairs(
    client: TestClient,
    workspace_id: UUID,
) -> None:
    response = client.post(
        "/api/v1/commands",
        json=_request(
            workspace_id,
            "PrepareWorkerHandoff",
            "worker_item",
            uuid4(),
            1,
            {
                "target_worker_id": str(uuid4()),
                "handoff_reason": "operator request",
                "handoff_instruction": "continue the implementation",
                "input_plan": {},
                "proposal_ttl_seconds": 900,
            },
            actor={"type": "Main Agent", "id": "main"},
        ),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "handoff_actor_forbidden"


def test_handoff_command_target_shape_is_validated_before_dispatch() -> None:
    with pytest.raises(ValueError):
        CommandRequest.model_validate(
            _request(
                uuid4(),
                "ConfirmWorkerHandoff",
                "worker_item",
                uuid4(),
                1,
                {
                    "proposal_id": uuid4(),
                    "expected_proposal_version": 1,
                    "expected_proposal_hash": "b" * 64,
                    "expected_worker_item_version": 1,
                },
            )
        )
