from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import EventModel, WorkspaceModel

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def add_workspace(database: Database, workspace_id: UUID, name: str) -> None:
    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=workspace_id,
                name=name,
                status="active",
                version=1,
            )
        )
        session.commit()


def register_worker_request(
    workspace_id: UUID,
    *,
    display_name: str = "Fake Worker A",
    idempotency_key: str = "register-worker-001",
) -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "RegisterWorker",
        "target_type": "worker",
        "actor": {"type": "admin", "id": "admin:test"},
        "target_id": None,
        "expected_version": None,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": {
            "adapter_type": "fake",
            "display_name": display_name,
            "capabilities": ["python", " code ", "python"],
            "capacity": 2,
            "cost_per_hour_microunits": 500_000,
            "startup_latency_ms": 25,
        },
    }


def worker_mutation_request(
    workspace_id: UUID,
    worker_id: UUID,
    command_type: str,
    expected_version: int,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": "worker",
        "actor": {"type": "admin", "id": "admin:test"},
        "target_id": str(worker_id),
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": payload or {},
    }


def register_worker(
    client: TestClient,
    workspace_id: UUID,
    *,
    display_name: str,
    idempotency_key: str,
) -> UUID:
    response = client.post(
        "/api/v1/commands",
        json=register_worker_request(
            workspace_id,
            display_name=display_name,
            idempotency_key=idempotency_key,
        ),
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["aggregate_id"])


def test_worker_registration_heartbeat_drain_and_version_replay(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    row_counts: Any,
) -> None:
    registered = client.post(
        "/api/v1/commands", json=register_worker_request(workspace_id)
    )

    assert registered.status_code == 201, registered.text
    worker_id = UUID(registered.json()["aggregate_id"])
    detail = client.get(
        f"/api/v1/workers/{worker_id}",
        params={"workspace_id": str(workspace_id)},
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["capabilities"] == ["code", "python"]
    assert detail.json()["status"] == "idle"
    assert detail.json()["desired_state"] == "active"
    assert detail.json()["current_load"] == 0
    assert detail.json()["version"] == 1
    assert detail.json()["created_at"]
    assert detail.json()["updated_at"]

    unhealthy = client.post(
        "/api/v1/commands",
        json=worker_mutation_request(
            workspace_id,
            worker_id,
            "HeartbeatWorker",
            1,
            "heartbeat-worker-unhealthy-001",
            {"healthy": False},
        ),
    )
    assert unhealthy.status_code == 200, unhealthy.text
    assert unhealthy.json()["aggregate_version"] == 2

    recovered = client.post(
        "/api/v1/commands",
        json=worker_mutation_request(
            workspace_id,
            worker_id,
            "HeartbeatWorker",
            2,
            "heartbeat-worker-healthy-001",
            {"healthy": True},
        ),
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["aggregate_version"] == 3

    drained = client.post(
        "/api/v1/commands",
        json=worker_mutation_request(
            workspace_id,
            worker_id,
            "DrainWorker",
            3,
            "drain-worker-001",
        ),
    )
    assert drained.status_code == 200, drained.text
    assert drained.json()["aggregate_version"] == 4

    final_detail = client.get(
        f"/api/v1/workers/{worker_id}",
        params={"workspace_id": str(workspace_id)},
    )
    assert final_detail.status_code == 200
    assert final_detail.json()["status"] == "draining"
    assert final_detail.json()["desired_state"] == "draining"
    assert final_detail.json()["version"] == 4

    stale_request = worker_mutation_request(
        workspace_id,
        worker_id,
        "DrainWorker",
        3,
        "stale-drain-worker-001",
    )
    stale = client.post("/api/v1/commands", json=stale_request)
    assert stale.status_code == 409
    assert stale.json() == {
        "error": {
            "code": "version_conflict",
            "message": "expected version 3, actual version 4",
            "details": {"expected": 3, "actual": 4},
        }
    }

    replay = {
        **stale_request,
        "correlation_id": str(uuid4()),
    }
    replayed = client.post("/api/v1/commands", json=replay)
    assert replayed.status_code == 409
    assert replayed.json() == stale.json()
    assert row_counts() == {
        "tasks": 0,
        "commands": 5,
        "events": 4,
        "outbox_messages": 4,
    }
    with database.session() as session:
        events = session.execute(
            select(EventModel.event_type, EventModel.payload).order_by(
                EventModel.aggregate_version
            )
        ).all()
    assert [event.event_type for event in events] == [
        "WorkerRegistered",
        "WorkerHeartbeatRecorded",
        "WorkerHeartbeatRecorded",
        "WorkerDraining",
    ]
    assert events[0].payload == {
        "adapter_type": "fake",
        "display_name": "Fake Worker A",
        "status": "idle",
        "desired_state": "active",
        "capabilities": ["code", "python"],
        "capacity": 2,
        "current_load": 0,
        "cost_per_hour_microunits": 500_000,
        "startup_latency_ms": 25,
    }
    assert events[1].payload["status"] == "unhealthy"
    assert events[2].payload["status"] == "idle"
    assert events[3].payload["status"] == "draining"
    assert all("last_heartbeat_at" in event.payload for event in events[1:])


def test_duplicate_worker_registration_replays_without_new_event(
    client: TestClient,
    workspace_id: UUID,
    row_counts: Any,
) -> None:
    request = register_worker_request(workspace_id)
    first = client.post("/api/v1/commands", json=request)
    assert first.status_code == 201, first.text

    replay = {
        **request,
        "correlation_id": str(uuid4()),
    }
    second = client.post("/api/v1/commands", json=replay)

    assert second.status_code == 201, second.text
    assert second.json() == first.json()
    assert row_counts() == {
        "tasks": 0,
        "commands": 1,
        "events": 1,
        "outbox_messages": 1,
    }


def test_worker_queries_are_ordered_and_workspace_isolated(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    workspace_b_id = uuid4()
    add_workspace(database, workspace_b_id, "Workspace B")
    zeta_id = register_worker(
        client,
        workspace_id,
        display_name="Zeta Worker",
        idempotency_key="register-zeta-001",
    )
    alpha_id = register_worker(
        client,
        workspace_id,
        display_name="Alpha Worker",
        idempotency_key="register-alpha-001",
    )
    beta_id = register_worker(
        client,
        workspace_b_id,
        display_name="Beta Worker",
        idempotency_key="register-beta-001",
    )

    listed = client.get(
        "/api/v1/workers",
        params={"workspace_id": str(workspace_id)},
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [str(alpha_id), str(zeta_id)]

    isolated_list = client.get(
        "/api/v1/workers",
        params={"workspace_id": str(workspace_b_id)},
    )
    assert isolated_list.status_code == 200
    assert [item["id"] for item in isolated_list.json()] == [str(beta_id)]

    isolated_detail = client.get(
        f"/api/v1/workers/{beta_id}",
        params={"workspace_id": str(workspace_id)},
    )
    missing_detail = client.get(
        f"/api/v1/workers/{uuid4()}",
        params={"workspace_id": str(workspace_id)},
    )
    assert isolated_detail.status_code == 404
    assert isolated_detail.json()["error"]["code"] == "not_found"
    assert missing_detail.status_code == 404
    assert missing_detail.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    ("target_type", "payload"),
    [
        ("task", register_worker_request(uuid4())["payload"]),
        ("worker", {**register_worker_request(uuid4())["payload"], "capabilities": []}),
        ("worker", {**register_worker_request(uuid4())["payload"], "unexpected": True}),
    ],
)
def test_worker_commands_reject_invalid_shapes(
    client: TestClient,
    workspace_id: UUID,
    row_counts: Any,
    target_type: str,
    payload: dict[str, Any],
) -> None:
    request = register_worker_request(workspace_id)
    request["target_type"] = target_type
    request["payload"] = payload

    response = client.post("/api/v1/commands", json=request)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert row_counts() == {
        "tasks": 0,
        "commands": 0,
        "events": 0,
        "outbox_messages": 0,
    }
