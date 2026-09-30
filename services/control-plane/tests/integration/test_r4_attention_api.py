from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.plans import TaskPlanSnapshot, WorkItem, WorkItemKind
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    EventModel,
    ExecutionEventModel,
    ExecutionModel,
    ExecutionSessionModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
    TaskModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
    WorkerModel,
)
from tests.integration.test_revision_confirmation import _seed_revision_confirmation

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("31000000-0000-0000-0000-000000000001")
BLOCKED_TASK_ID = UUID("31000000-0000-0000-0000-000000000002")
PLAN_ID = UUID("11000000-0000-0000-0000-000000000001")
WORKER_ID = UUID("41000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("42000000-0000-0000-0000-000000000001")
EXECUTION_ID = UUID("43000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("44000000-0000-0000-0000-000000000001")
INPUT_EVENT_ID = UUID("45000000-0000-0000-0000-000000000001")


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
        session.add(
            RootTaskPlanHeadModel(
                workspace_id=WORKSPACE_ID,
                root_task_id=ROOT_TASK_ID,
                active_snapshot_id=snapshot.id,
                active_generation=snapshot.generation,
                active_snapshot_hash=plan_snapshot_content_hash(snapshot),
                version=1,
                updated_at=snapshot.published_at,
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
            "attention_key": f"subtask:{ROOT_TASK_ID}:blocked:blocked",
            "source_correlation_id": str(UUID(int=0)),
            "source_resource_version": source_version,
        },
    }


def _worker_input_request(*, command_id: UUID) -> dict[str, object]:
    return {
        "command_id": str(command_id),
        "workspace_id": str(WORKSPACE_ID),
        "command_type": "AcknowledgeAttentionItem",
        "target_type": "task",
        "actor": {"type": "local", "id": "aitejiu"},
        "target_id": str(ROOT_TASK_ID),
        "expected_version": 1,
        "idempotency_key": f"worker-input:{command_id}",
        "correlation_id": str(uuid4()),
        "payload": {
            "attention_key": f"worker_input:{WORKER_ITEM_ID}:{INPUT_EVENT_ID}",
            "source_correlation_id": str(INPUT_EVENT_ID),
            "source_resource_version": 1,
        },
    }


def _seed_worker_input(database: Database) -> None:
    _seed(database)
    with database.session() as session:
        session.add(
            WorkerModel(
                id=WORKER_ID,
                workspace_id=WORKSPACE_ID,
                adapter_type="fake",
                display_name="Input Worker",
                status="busy",
                desired_state="active",
                capabilities=["python"],
                capacity=1,
                current_load=1,
                cost_per_hour_microunits=1,
                startup_latency_ms=1,
                last_heartbeat_at=NOW,
                version=1,
            )
        )
        session.add(
            WorkerItemModel(
                id=WORKER_ITEM_ID,
                workspace_id=WORKSPACE_ID,
                root_task_id=ROOT_TASK_ID,
                stable_key="blocked-input",
                title="Blocked input WorkerItem",
                responsibility="Wait for the operator input.",
                status="active",
                context_version=1,
                acknowledged_context_version=1,
                derived_from_worker_item_ids=[],
                version=1,
            )
        )
        session.add(
            WorkerItemPlacementModel(
                plan_snapshot_id=PLAN_ID,
                plan_generation=1,
                workspace_id=WORKSPACE_ID,
                root_task_id=ROOT_TASK_ID,
                worker_item_id=WORKER_ITEM_ID,
                worker_item_key="blocked-input",
                subtask_key="blocked",
                subtask_task_id=BLOCKED_TASK_ID,
                role="implementer",
                capability_requirements=["python"],
                worker_policy_ref=None,
                configuration_ref=None,
            )
        )
        session.flush()
        execution = ExecutionModel(
            id=EXECUTION_ID,
            workspace_id=WORKSPACE_ID,
            task_id=BLOCKED_TASK_ID,
            lineage_id=EXECUTION_ID,
            attempt=1,
            max_attempts=1,
            purpose="Wait for input",
            status="running",
            capability_requirements=["python"],
            pinned_worker_id=None,
            assigned_worker_id=WORKER_ID,
            active_lease_id=None,
            last_fencing_token=1,
            not_before=NOW,
            failure_code=None,
            started_at=NOW,
            finished_at=None,
            version=1,
            worker_item_id=WORKER_ITEM_ID,
        )
        session.add(execution)
        session.flush([execution])
        session.add(
            ExecutionSessionModel(
                id=SESSION_ID,
                workspace_id=WORKSPACE_ID,
                execution_id=EXECUTION_ID,
                worker_id=WORKER_ID,
                adapter_session_id="input-session",
                status="active",
                last_sequence=1,
                last_activity_at=NOW,
                input_mode="queued",
                adapter_module_id=None,
                adapter_config_version=None,
                workspace_module_id=None,
                workspace_config_version=None,
                capability_snapshot=None,
                process_ref=None,
                workspace_ref=None,
                version=1,
            )
        )
        session.flush()
        session.add(
            ExecutionEventModel(
                id=INPUT_EVENT_ID,
                workspace_id=WORKSPACE_ID,
                execution_id=EXECUTION_ID,
                session_id=SESSION_ID,
                sequence=1,
                event_type="progress.updated",
                payload={
                    "trace_summary": "等待操作员输入",
                    "requires_user_input": True,
                },
                occurred_at=NOW,
                raw_ref=None,
            )
        )
        session.commit()


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
    assert [item["key"] for item in attention] == [
        f"subtask:{ROOT_TASK_ID}:blocked:blocked"
    ]

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


def test_worker_input_attention_acknowledgement_resolves_current_event(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    del workspace_id
    _seed_worker_input(database)

    before = client.get(
        f"/api/v1/tasks/{ROOT_TASK_ID}/overview",
        params={"workspace_id": str(WORKSPACE_ID)},
    )
    assert before.status_code == 200, before.text
    assert [item["key"] for item in before.json()["attention_items"]] == [
        f"worker_input:{WORKER_ITEM_ID}:{INPUT_EVENT_ID}"
    ]

    acknowledged = client.post(
        "/api/v1/commands", json=_worker_input_request(command_id=uuid4())
    )
    assert acknowledged.status_code == 200, acknowledged.text

    after = client.get(
        f"/api/v1/tasks/{ROOT_TASK_ID}/overview",
        params={"workspace_id": str(WORKSPACE_ID)},
    )
    assert after.status_code == 200, after.text
    assert after.json()["attention_items"] == []


def test_root_revision_attention_keeps_human_scope_and_confirmation_link(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    seed = _seed_revision_confirmation(database, workspace_id)

    response = client.get(
        f"/api/v1/tasks/{seed['root_task'].id}/overview",
        params={"workspace_id": str(workspace_id)},
    )

    assert response.status_code == 200, response.text
    revision_attention = response.json()["revision_attention"]
    assert revision_attention["status"] == "proposal_ready"
    assert revision_attention["subtask_path"]
    assert revision_attention["action"] == "review_revision"
    assert revision_attention["deep_link"].endswith(
        f"/revisions/{seed['request'].id}"
    )
    assert revision_attention["correlation_id"] == str(seed["request"].correlation_id)
