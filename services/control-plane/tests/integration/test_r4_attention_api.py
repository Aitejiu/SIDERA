from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.plans import TaskPlanSnapshot, WorkItem, WorkItemKind
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    EventModel,
    PlanSnapshotModel,
    TaskModel,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("31000000-0000-0000-0000-000000000001")
BLOCKED_TASK_ID = UUID("31000000-0000-0000-0000-000000000002")
PLAN_ID = UUID("11000000-0000-0000-0000-000000000001")


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _seed(database: Database) -> None:
    snapshot = TaskPlanSnapshot.create(
        snapshot_id=PLAN_ID,
        schema_version=1,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=1,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="attention:test",
        title="Attention test",
        goal_summary="Confirm one attention item.",
        acceptance_refs=("acceptance:root",),
        work_items=(
            WorkItem(
                key="blocked",
                task_id=BLOCKED_TASK_ID,
                kind=WorkItemKind.CHILD,
                title="Blocked worker",
                goal_summary="Waiting for intervention.",
                acceptance_refs=("acceptance:blocked",),
                milestone_key=None,
                worker_slot_key=None,
                expected_outputs=(),
                source_refs=("test",),
            ),
        ),
        worker_slots=(),
        milestones=(),
        dependencies=(),
        published_by="local:test",
        published_at=NOW,
    )
    with database.session() as session:
        session.add_all(
            [
                TaskModel(
                    id=ROOT_TASK_ID,
                    workspace_id=WORKSPACE_ID,
                    title="Attention root",
                    description="Attention root",
                    acceptance_criteria=["acceptance:root"],
                    status="running",
                    priority=50,
                    requested_by="test",
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                TaskModel(
                    id=BLOCKED_TASK_ID,
                    workspace_id=WORKSPACE_ID,
                    title="Blocked worker",
                    description="Blocked worker",
                    acceptance_criteria=["acceptance:blocked"],
                    status="blocked",
                    priority=50,
                    requested_by="test",
                    version=3,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add(
            PlanSnapshotModel(
                id=snapshot.id,
                schema_version=snapshot.schema_version,
                workspace_id=snapshot.workspace_id,
                root_task_id=snapshot.root_task_id,
                generation=snapshot.generation,
                parent_snapshot_id=None,
                source_type=snapshot.source_type,
                source_ref=snapshot.source_ref,
                title=snapshot.title,
                goal_summary=snapshot.goal_summary,
                payload=snapshot.to_payload(),
                published_by=snapshot.published_by,
                published_at=snapshot.published_at,
            )
        )
        session.commit()


def _request(*, command_id: UUID, source_version: int = 3) -> dict[str, object]:
    return {
        "command_id": str(command_id),
        "workspace_id": str(WORKSPACE_ID),
        "command_type": "AcknowledgeAttentionItem",
        "target_type": "task",
        "actor": {"type": "local", "id": "aitejiu"},
        "target_id": str(ROOT_TASK_ID),
        "expected_version": 1,
        "idempotency_key": f"attention:{command_id}",
        "correlation_id": str(uuid4()),
        "payload": {
            "attention_key": f"task:{BLOCKED_TASK_ID}:blocked",
            "source_correlation_id": str(UUID(int=0)),
            "source_resource_version": source_version,
        },
    }


def test_attention_acknowledgement_is_actor_local_and_source_bound(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    _seed(database)
    params = {"workspace_id": str(workspace_id)}

    before = client.get(f"/api/v1/tasks/{ROOT_TASK_ID}/overview", params=params)
    assert before.status_code == 200, before.text
    attention = before.json()["attention_items"]
    assert [item["key"] for item in attention] == [f"task:{BLOCKED_TASK_ID}:blocked"]

    request = _request(command_id=uuid4())
    acknowledged = client.post("/api/v1/commands", json=request)
    assert acknowledged.status_code == 200, acknowledged.text
    replay = client.post("/api/v1/commands", json=request)
    assert replay.status_code == 200, replay.text
    assert replay.json() == acknowledged.json()

    after = client.get(f"/api/v1/tasks/{ROOT_TASK_ID}/overview", params=params)
    assert after.status_code == 200, after.text
    assert after.json()["attention_items"] == []

    with database.session() as session:
        task = session.get(TaskModel, BLOCKED_TASK_ID)
        assert task is not None
        assert task.version == 3
        assert session.query(EventModel).filter_by(
            event_type="attention.item_acknowledged"
        ).count() == 1

    other_settings = Settings(
        database_url=str(database.engine.url),
        local_actor_id="other-user",
    )
    with TestClient(create_app(other_settings)) as other_client:
        other_view = other_client.get(
            f"/api/v1/tasks/{ROOT_TASK_ID}/overview", params=params
        )
    assert len(other_view.json()["attention_items"]) == 1

    with database.session() as session:
        session.execute(
            update(TaskModel)
            .where(TaskModel.id == BLOCKED_TASK_ID)
            .values(version=4, updated_at=NOW)
        )
        session.commit()

    current = client.get(f"/api/v1/tasks/{ROOT_TASK_ID}/overview", params=params)
    assert len(current.json()["attention_items"]) == 1
    stale = client.post("/api/v1/commands", json=_request(command_id=uuid4()))
    assert stale.status_code == 422
    assert stale.json()["error"]["code"] == "validation_error"
    assert stale.json()["error"]["details"]["current_resource_version"] == 4
