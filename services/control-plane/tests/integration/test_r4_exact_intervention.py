from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_r4_task_first_api import (
    BACKEND_EXECUTION_ID,
    GENERATION_TWO_ID,
    ROOT_TASK_ID,
    SESSION_ID,
    WORKER_ID,
    _seed,
    _snapshot,
)

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import EventModel, ExecutionInputModel

pytestmark = pytest.mark.integration

GENERATION_THREE_ID = UUID("10000000-0000-0000-0000-000000000003")


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url))) as test_client:
        yield test_client


def _publish_generation_three(client: TestClient, workspace_id: UUID) -> None:
    snapshot = _snapshot(
        generation=3,
        snapshot_id=GENERATION_THREE_ID,
        parent=GENERATION_TWO_ID,
    )
    response = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "PublishPlanSnapshot",
            "target_type": "task",
            "actor": {"type": "local", "id": "aitejiu"},
            "target_id": str(ROOT_TASK_ID),
            "expected_version": 1,
            "idempotency_key": "exact-intervention:publish:g3",
            "correlation_id": str(uuid4()),
            "payload": {
                "expected_current_generation": 2,
                "snapshot": snapshot.to_payload(),
            },
        },
    )
    assert response.status_code == 200, response.text


def _send_request(
    workspace_id: UUID,
    *,
    plan_snapshot_id: UUID,
    client_key: str,
) -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "SendExecutionInput",
        "target_type": "execution_session",
        "actor": {"type": "local", "id": "aitejiu"},
        "target_id": str(SESSION_ID),
        "expected_version": 2,
        "idempotency_key": f"exact-intervention:send:{client_key}",
        "correlation_id": str(uuid4()),
        "payload": {
            "context": {
                "plan_snapshot_id": str(plan_snapshot_id),
                "root_task_id": str(ROOT_TASK_ID),
                "work_item_key": "backend",
                "worker_slot_key": "backend",
                "worker_id": str(WORKER_ID),
                "execution_id": str(BACKEND_EXECUTION_ID),
                "session_id": str(SESSION_ID),
            },
            "client_idempotency_key": client_key,
            "text": "Continue the exact backend task",
            "attachment_refs": [],
        },
    }


def test_intervention_rejects_stale_plan_address_and_accepts_refreshed_address(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    _seed(database)
    _publish_generation_three(client, workspace_id)

    stale = client.post(
        "/api/v1/commands",
        json=_send_request(
            workspace_id,
            plan_snapshot_id=GENERATION_TWO_ID,
            client_key="stale-context",
        ),
    )

    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "execution_context_changed"
    assert stale.json()["error"]["details"]["changed_fields"] == [
        "plan_snapshot_id"
    ]
    assert stale.json()["error"]["details"]["current"] == {
        "plan_snapshot_id": str(GENERATION_THREE_ID),
        "root_task_id": str(ROOT_TASK_ID),
        "work_item_key": "backend",
        "worker_slot_key": "backend",
        "worker_id": str(WORKER_ID),
        "execution_id": str(BACKEND_EXECUTION_ID),
        "session_id": str(SESSION_ID),
    }

    with database.session() as session:
        assert session.query(ExecutionInputModel).count() == 0

    refreshed = client.post(
        "/api/v1/commands",
        json=_send_request(
            workspace_id,
            plan_snapshot_id=GENERATION_THREE_ID,
            client_key="refreshed-context",
        ),
    )
    assert refreshed.status_code == 200, refreshed.text

    with database.session() as session:
        assert session.query(ExecutionInputModel).count() == 1
        accepted = (
            session.query(EventModel)
            .filter(EventModel.event_type == "ExecutionInputAccepted")
            .one()
        )
        assert accepted.payload["execution_context"] == {
            "plan_snapshot_id": str(GENERATION_THREE_ID),
            "root_task_id": str(ROOT_TASK_ID),
            "work_item_key": "backend",
            "worker_slot_key": "backend",
            "worker_id": str(WORKER_ID),
            "execution_id": str(BACKEND_EXECUTION_ID),
            "session_id": str(SESSION_ID),
        }
        assert "adapter_session_id" not in accepted.payload["execution_context"]
