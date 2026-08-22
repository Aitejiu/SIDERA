from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.plans import (
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    EventModel,
    PlanSnapshotModel,
    TaskModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
)

pytestmark = pytest.mark.integration


ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
FIRST_SNAPSHOT_ID = UUID("10000000-0000-0000-0000-000000000001")
SECOND_SNAPSHOT_ID = UUID("10000000-0000-0000-0000-000000000002")
V2_FIRST_SNAPSHOT_ID = UUID("10000000-0000-0000-0000-000000000003")
V2_SECOND_SNAPSHOT_ID = UUID("10000000-0000-0000-0000-000000000004")
SUBTASK_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url))) as test_client:
        yield test_client


def make_snapshot(
    *, snapshot_id: UUID, generation: int, parent_snapshot_id: UUID | None = None
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=1,
        workspace_id=UUID("00000000-0000-0000-0000-000000000001"),
        root_task_id=ROOT_TASK_ID,
        generation=generation,
        parent_snapshot_id=parent_snapshot_id,
        source_type="test",
        source_ref=f"api:g{generation}",
        title="API plan",
        goal_summary="Expose immutable generations.",
        acceptance_refs=("test:api",),
        work_items=(),
        worker_slots=(),
        milestones=(),
        dependencies=(),
        published_by="local:test",
        published_at=datetime(2026, 8, 21, tzinfo=UTC),
    )


def publish_request(
    workspace_id: UUID,
    snapshot: TaskPlanSnapshot,
    *,
    expected_generation: int,
    idempotency_key: str,
) -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "PublishPlanSnapshot",
        "target_type": "task",
        "actor": {"type": "local", "id": "aitejiu"},
        "target_id": str(snapshot.root_task_id),
        "expected_version": 1,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": {
            "expected_current_generation": expected_generation,
            "snapshot": snapshot.to_payload(),
        },
    }


def seed_root_task(database: Database, workspace_id: UUID) -> None:
    with database.session() as session:
        session.add(
            TaskModel(
                id=ROOT_TASK_ID,
                workspace_id=workspace_id,
                title="R4 root",
                description="Plan API root task",
                acceptance_criteria=["generations are readable"],
                status="pending",
                priority=50,
                requested_by="test",
                version=1,
            )
        )
        session.commit()


def seed_v2_subtask(database: Database, workspace_id: UUID) -> None:
    with database.session() as session:
        session.add(
            TaskModel(
                id=SUBTASK_TASK_ID,
                workspace_id=workspace_id,
                title="Callback subtask",
                description="Materialized callback subtask",
                acceptance_criteria=["callbacks are safe"],
                status="pending",
                priority=50,
                requested_by="test",
                version=1,
            )
        )
        session.commit()


def make_v2_snapshot(
    *,
    snapshot_id: UUID,
    generation: int,
    parent_snapshot_id: UUID | None = None,
    worker_item_id: UUID | None = None,
    responsibility: str = "Keep callback retries idempotent.",
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=2,
        workspace_id=UUID("00000000-0000-0000-0000-000000000001"),
        root_task_id=ROOT_TASK_ID,
        generation=generation,
        parent_snapshot_id=parent_snapshot_id,
        source_type="test",
        source_ref=f"api:v2:g{generation}",
        title="API v2 plan",
        goal_summary="Materialize WorkerItems from a v2 plan.",
        acceptance_refs=("test:v2",),
        published_by="local:test",
        published_at=datetime(2026, 8, 22, tzinfo=UTC),
        subtasks=(
            SubtaskPlanItem(
                key="callbacks",
                task_id=SUBTASK_TASK_ID,
                kind=WorkItemKind.CHILD,
                title="Callbacks",
                goal_summary="Keep callbacks safe.",
                acceptance_refs=("callbacks are safe",),
                milestone_key=None,
                expected_outputs=("callback code",),
                source_refs=("test",),
            ),
        ),
        worker_item_plans=(
            WorkerItemPlanItem(
                key="callback-implementer",
                worker_item_id=worker_item_id,
                subtask_key="callbacks",
                title="Callback implementer",
                role="implementer",
                responsibility=responsibility,
                capability_requirements=("python",),
                worker_policy_ref="policy:balanced",
                configuration_ref="config:callback",
                concurrency_limit=1,
                source_refs=("test",),
            ),
        ),
        subtask_milestones=(),
        subtask_dependencies=(),
        worker_item_dependencies=(),
    )


def test_v2_publish_materializes_stable_worker_item_and_events(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    seed_root_task(database, workspace_id)
    seed_v2_subtask(database, workspace_id)
    first = make_v2_snapshot(
        snapshot_id=V2_FIRST_SNAPSHOT_ID,
        generation=1,
    )

    response = client.post(
        "/api/v1/commands",
        json=publish_request(
            workspace_id,
            first,
            expected_generation=0,
            idempotency_key="api-v2-publish-1",
        ),
    )

    assert response.status_code == 200, response.text

    with database.session() as session:
        current = session.scalar(
            select(PlanSnapshotModel).where(
                PlanSnapshotModel.id == V2_FIRST_SNAPSHOT_ID
            )
        )
        assert current is not None
        worker_item_id = current.payload["worker_items"][0]["worker_item_id"]
        worker_item = session.scalar(
            select(WorkerItemModel).where(WorkerItemModel.id == UUID(worker_item_id))
        )
        placement = session.scalar(select(WorkerItemPlacementModel))
        event_types = session.scalars(select(EventModel.event_type)).all()

    assert worker_item is not None
    assert worker_item.stable_key == f"worker-item:{worker_item.id}"
    assert placement is not None
    assert placement.worker_item_id == worker_item.id
    assert event_types == [
        "task.plan_snapshot_published",
        "worker_item.created",
        "worker_item.placement_materialized",
    ]


def test_v2_invalid_reuse_rolls_back_snapshot_placement_and_events(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    seed_root_task(database, workspace_id)
    seed_v2_subtask(database, workspace_id)
    first = make_v2_snapshot(
        snapshot_id=V2_FIRST_SNAPSHOT_ID,
        generation=1,
    )
    first_response = client.post(
        "/api/v1/commands",
        json=publish_request(
            workspace_id,
            first,
            expected_generation=0,
            idempotency_key="api-v2-publish-rollback-1",
        ),
    )
    assert first_response.status_code == 200, first_response.text

    with database.session() as session:
        worker_item = session.scalar(select(WorkerItemModel))
    assert worker_item is not None

    invalid = make_v2_snapshot(
        snapshot_id=V2_SECOND_SNAPSHOT_ID,
        generation=2,
        parent_snapshot_id=first.id,
        worker_item_id=worker_item.id,
        responsibility="Change the callback architecture entirely.",
    )
    invalid_response = client.post(
        "/api/v1/commands",
        json=publish_request(
            workspace_id,
            invalid,
            expected_generation=1,
            idempotency_key="api-v2-publish-rollback-2",
        ),
    )

    assert invalid_response.status_code == 422
    assert (
        invalid_response.json()["error"]["details"]["reason"]
        == "worker_item_responsibility_change_not_supported"
    )
    with database.session() as session:
        assert session.scalar(select(PlanSnapshotModel).where(
            PlanSnapshotModel.id == V2_SECOND_SNAPSHOT_ID
        )) is None
        assert session.scalar(select(WorkerItemPlacementModel).where(
            WorkerItemPlacementModel.plan_snapshot_id == V2_SECOND_SNAPSHOT_ID
        )) is None
        assert session.scalar(select(EventModel.event_type).where(
            EventModel.event_type == "worker_item.placement_materialized",
            EventModel.payload["snapshot_id"].astext == str(V2_SECOND_SNAPSHOT_ID),
        )) is None


def test_publish_and_read_immutable_plan_generations(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    seed_root_task(database, workspace_id)
    first = make_snapshot(snapshot_id=FIRST_SNAPSHOT_ID, generation=1)
    second = make_snapshot(
        snapshot_id=SECOND_SNAPSHOT_ID,
        generation=2,
        parent_snapshot_id=first.id,
    )

    first_response = client.post(
        "/api/v1/commands",
        json=publish_request(
            workspace_id,
            first,
            expected_generation=0,
            idempotency_key="api-publish-1",
        ),
    )
    assert first_response.status_code == 200, first_response.text

    second_response = client.post(
        "/api/v1/commands",
        json=publish_request(
            workspace_id,
            second,
            expected_generation=1,
            idempotency_key="api-publish-2",
        ),
    )
    assert second_response.status_code == 200, second_response.text

    current = client.get(
        f"/api/v1/tasks/{ROOT_TASK_ID}/plan/current",
        params={"workspace_id": str(workspace_id)},
    )
    assert current.status_code == 200, current.text
    assert current.json()["generation"] == 2
    assert current.json()["id"] == str(SECOND_SNAPSHOT_ID)

    history = client.get(
        f"/api/v1/tasks/{ROOT_TASK_ID}/plan/generations",
        params={"workspace_id": str(workspace_id)},
    )
    assert history.status_code == 200, history.text
    assert [item["generation"] for item in history.json()] == [2, 1]
    assert history.json()[1] == first.to_payload()

    generation_one = client.get(
        f"/api/v1/tasks/{ROOT_TASK_ID}/plan/generations/1",
        params={"workspace_id": str(workspace_id)},
    )
    assert generation_one.status_code == 200
    assert generation_one.json() == first.to_payload()


def test_unknown_root_plan_is_not_found(
    client: TestClient,
    workspace_id: UUID,
) -> None:
    response = client.get(
        f"/api/v1/tasks/{uuid4()}/plan/current",
        params={"workspace_id": str(workspace_id)},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_malformed_plan_reference_is_a_stable_command_rejection(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    seed_root_task(database, workspace_id)
    snapshot = make_snapshot(snapshot_id=FIRST_SNAPSHOT_ID, generation=1)
    payload = snapshot.to_payload()
    payload["dependencies"] = [
        {
            "upstream_work_item_key": "missing-upstream",
            "downstream_work_item_key": "missing-downstream",
            "kind": "hard",
            "required_evidence_refs": [],
            "enforcement": "informational",
        }
    ]
    request = publish_request(
        workspace_id,
        snapshot,
        expected_generation=0,
        idempotency_key="api-publish-invalid",
    )
    request["payload"]["snapshot"] = payload

    response = client.post("/api/v1/commands", json=request)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["details"]["reason"] == "unknown_reference"
