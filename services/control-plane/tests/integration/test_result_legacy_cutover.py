from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url))) as test_client:
        yield test_client


@pytest.fixture
def enabled_client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(
        database_url=database_url,
        result_integration_enabled=True,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _result_rework_request(workspace_id: UUID) -> dict[str, Any]:
    root_task_id = uuid4()
    candidate_id = uuid4()
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "RequestTaskRework",
        "target_type": "task",
        "actor": {"type": "local", "id": "operator"},
        "target_id": str(root_task_id),
        "expected_version": 1,
        "idempotency_key": f"r6-result-cutover:{uuid4()}",
        "correlation_id": str(uuid4()),
        "payload": {
            "candidate_id": str(candidate_id),
            "candidate_hash": "a" * 64,
            "mode": "user",
            "issue_keys": [],
            "target_worker_item_ids": [],
            "impact_worker_item_ids": [],
            "reason": "cutover gate test",
        },
    }


def test_disabled_result_integration_rejects_new_recovery_writes(
    client: TestClient, workspace_id: UUID
) -> None:
    response = client.post("/api/v1/commands", json=_result_rework_request(workspace_id))

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "result_integration_disabled"


def test_legacy_task_history_remains_readable_when_result_integration_is_disabled(
    client: TestClient, database: Database, workspace_id: UUID
) -> None:
    create = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "CreateTask",
            "target_type": "task",
            "actor": {"type": "user", "id": "user:cutover"},
            "target_id": None,
            "expected_version": None,
            "idempotency_key": f"r6-legacy-task:{uuid4()}",
            "correlation_id": str(uuid4()),
            "payload": {
                "title": "Legacy result history",
                "description": "Read before result integration is enabled",
                "acceptance_criteria": ["history is readable"],
                "priority": 50,
            },
        },
    )
    assert create.status_code == 201, create.text
    task_id = create.json()["aggregate_id"]

    with SqlAlchemyUnitOfWork(database) as uow:
        task = uow.tasks.get(UUID(task_id), workspace_id, for_update=True)
        assert task is not None
        previous_version = task.version
        task.await_acceptance()
        uow.tasks.save(task, previous_version)

    complete = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "CompleteTask",
            "target_type": "task",
            "actor": {"type": "user", "id": "user:cutover"},
            "target_id": task_id,
            "expected_version": 2,
            "idempotency_key": f"r6-legacy-complete:{uuid4()}",
            "correlation_id": str(uuid4()),
            "payload": {},
        },
    )
    assert complete.status_code == 200, complete.text

    history = client.get(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}/results"
    )

    assert history.status_code == 200, history.text
    assert history.json()["result_stage"] == "legacy"


def test_enabled_cutover_rejects_legacy_task_completion_without_result_head(
    client: TestClient,
    enabled_client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    create = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "CreateTask",
            "target_type": "task",
            "actor": {"type": "user", "id": "user:cutover"},
            "target_id": None,
            "expected_version": None,
            "idempotency_key": f"r6-cutover-task:{uuid4()}",
            "correlation_id": str(uuid4()),
            "payload": {
                "title": "Unmigrated result task",
                "description": "Completion must use the result contract",
                "acceptance_criteria": ["legacy completion is rejected"],
                "priority": 50,
            },
        },
    )
    assert create.status_code == 201, create.text
    task_id = UUID(create.json()["aggregate_id"])

    with SqlAlchemyUnitOfWork(database) as uow:
        task = uow.tasks.get(task_id, workspace_id, for_update=True)
        assert task is not None
        previous_version = task.version
        task.await_acceptance()
        uow.tasks.save(task, previous_version)

    response = enabled_client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "CompleteTask",
            "target_type": "task",
            "actor": {"type": "local", "id": "aitejiu"},
            "target_id": str(task_id),
            "expected_version": 2,
            "idempotency_key": f"r6-cutover-complete:{uuid4()}",
            "correlation_id": str(uuid4()),
            "payload": {},
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "result_integration_cutover_required"
