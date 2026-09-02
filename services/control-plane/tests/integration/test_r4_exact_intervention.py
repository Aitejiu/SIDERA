from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from tests.integration.test_r4_task_first_api import (
    BACKEND_EXECUTION_ID,
    GENERATION_TWO_ID,
    ROOT_TASK_ID,
    SESSION_ID,
    WORKER_ID,
    _seed,
    _snapshot,
)

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.worker_item_backfill import (
    WorkerItemBackfillService,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import EventModel, ExecutionInputModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

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


def _bind_historical_worker_item(database: Database, workspace_id: UUID) -> UUID:
    with SqlAlchemyUnitOfWork(database) as uow:
        WorkerItemBackfillService().backfill_root(
            uow, workspace_id, ROOT_TASK_ID
        )
        item = uow.worker_items.get_by_stable_key(
            ROOT_TASK_ID, "legacy:backend:backend"
        )
        assert item is not None
        execution = uow.executions.get(
            BACKEND_EXECUTION_ID, workspace_id, for_update=True
        )
        assert execution is not None
        previous_version = execution.version
        execution.worker_item_id = item.id
        uow.executions.save(execution, previous_version)
        return item.id


def _send_request(
    workspace_id: UUID,
    *,
    worker_item_id: UUID,
    plan_generation: int,
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
                "worker_item_id": str(worker_item_id),
                "plan_generation": plan_generation,
                "context_version": 1,
                "worker_id": str(WORKER_ID),
                "execution_id": str(BACKEND_EXECUTION_ID),
                "session_id": str(SESSION_ID),
                "session_version": 2,
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
    worker_item_id = _bind_historical_worker_item(database, workspace_id)

    stale = client.post(
        "/api/v1/commands",
        json=_send_request(
            workspace_id,
            worker_item_id=worker_item_id,
            plan_generation=2,
            client_key="stale-context",
        ),
    )

    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "execution_context_changed"
    assert stale.json()["error"]["details"]["changed_fields"] == ["plan_generation"]
    assert stale.json()["error"]["details"]["current"] == {
        "worker_item_id": str(worker_item_id),
        "plan_generation": 3,
        "context_version": 1,
        "execution_id": str(BACKEND_EXECUTION_ID),
        "session_id": str(SESSION_ID),
        "session_version": 2,
        "worker_id": str(WORKER_ID),
    }

    with database.session() as session:
        assert session.query(ExecutionInputModel).count() == 0

    refreshed = client.post(
        "/api/v1/commands",
        json=_send_request(
            workspace_id,
            worker_item_id=worker_item_id,
            plan_generation=3,
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
            "worker_item_id": str(worker_item_id),
            "plan_generation": "3",
            "context_version": "1",
            "execution_id": str(BACKEND_EXECUTION_ID),
            "session_id": str(SESSION_ID),
            "session_version": "2",
            "worker_id": str(WORKER_ID),
        }
        assert "adapter_session_id" not in accepted.payload["execution_context"]
