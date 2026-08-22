from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.plans import (
    SubtaskMilestone,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    EvidenceClaimModel,
    ExecutionEventModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionSessionModel,
    PlanSnapshotModel,
    ProjectionChangeModel,
    TaskModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
    WorkerModel,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("33000000-0000-0000-0000-000000000001")
CALLBACK_TASK_ID = UUID("33000000-0000-0000-0000-000000000002")
RETRY_TASK_ID = UUID("33000000-0000-0000-0000-000000000003")
SNAPSHOT_ONE_ID = UUID("13000000-0000-0000-0000-000000000001")
SNAPSHOT_TWO_ID = UUID("13000000-0000-0000-0000-000000000002")
WORKER_ITEM_ID = UUID("43000000-0000-0000-0000-000000000001")
WORKER_ID = UUID("73000000-0000-0000-0000-000000000001")
EXECUTION_ONE_ID = UUID("53000000-0000-0000-0000-000000000001")
EXECUTION_TWO_ID = UUID("53000000-0000-0000-0000-000000000002")
SESSION_ONE_ID = UUID("63000000-0000-0000-0000-000000000001")
SESSION_TWO_ID = UUID("63000000-0000-0000-0000-000000000002")
EVENT_ONE_ID = UUID("83000000-0000-0000-0000-000000000001")
EVENT_TWO_ID = UUID("83000000-0000-0000-0000-000000000002")
ARTIFACT_ID = UUID("83000000-0000-0000-0000-000000000003")
EVIDENCE_ID = UUID("83000000-0000-0000-0000-000000000004")
INPUT_ID = UUID("a3000000-0000-0000-0000-000000000001")


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url))) as test_client:
        yield test_client


def _snapshot(
    *,
    snapshot_id: UUID,
    generation: int,
    parent_snapshot_id: UUID | None,
    subtask_key: str,
    task_id: UUID,
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=2,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=generation,
        parent_snapshot_id=parent_snapshot_id,
        source_type="test",
        source_ref=f"api:worker-workspace:{generation}",
        title="支付重构",
        goal_summary="完成支付回调链路重构",
        acceptance_refs=("acceptance:root",),
        published_by="local:test",
        published_at=NOW + timedelta(minutes=generation),
        subtasks=(
            SubtaskPlanItem(
                key=subtask_key,
                task_id=task_id,
                kind=WorkItemKind.CHILD,
                title="回调幂等" if generation == 1 else "回调重试",
                goal_summary="保证支付回调可安全重试",
                acceptance_refs=("acceptance:callback",),
                milestone_key="implementation",
                expected_outputs=("callback patch",),
                source_refs=("planner:callback",),
            ),
        ),
        worker_item_plans=(
            WorkerItemPlanItem(
                key="callback-implementer",
                worker_item_id=WORKER_ITEM_ID,
                subtask_key=subtask_key,
                title="回调实现",
                role="implementer",
                responsibility="实现回调幂等逻辑",
                capability_requirements=("python",),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=("planner:callback-implementer",),
            ),
        ),
        subtask_milestones=(
            SubtaskMilestone(
                key="implementation",
                title="实现",
                subtask_keys=(subtask_key,),
                acceptance_refs=(),
                order_hint=1,
            ),
        ),
    )


def _seed(database: Database) -> None:
    first = _snapshot(
        snapshot_id=SNAPSHOT_ONE_ID,
        generation=1,
        parent_snapshot_id=None,
        subtask_key="callback-idempotency",
        task_id=CALLBACK_TASK_ID,
    )
    second = _snapshot(
        snapshot_id=SNAPSHOT_TWO_ID,
        generation=2,
        parent_snapshot_id=SNAPSHOT_ONE_ID,
        subtask_key="callback-retry",
        task_id=RETRY_TASK_ID,
    )
    with database.session() as session:
        session.add_all(
            [
                TaskModel(
                    id=ROOT_TASK_ID,
                    workspace_id=WORKSPACE_ID,
                    title="支付重构",
                    description="完成支付回调链路重构",
                    acceptance_criteria=["acceptance:root"],
                    status="running",
                    priority=50,
                    requested_by="test",
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                TaskModel(
                    id=CALLBACK_TASK_ID,
                    workspace_id=WORKSPACE_ID,
                    title="回调幂等",
                    description="保证支付回调可安全重试",
                    acceptance_criteria=["acceptance:callback"],
                    status="succeeded",
                    priority=50,
                    requested_by="test",
                    version=2,
                    created_at=NOW,
                    updated_at=NOW + timedelta(minutes=1),
                ),
                TaskModel(
                    id=RETRY_TASK_ID,
                    workspace_id=WORKSPACE_ID,
                    title="回调重试",
                    description="在新的计划代继续回调工作",
                    acceptance_criteria=["acceptance:callback"],
                    status="running",
                    priority=50,
                    requested_by="test",
                    version=4,
                    created_at=NOW + timedelta(minutes=4),
                    updated_at=NOW + timedelta(minutes=8),
                ),
                WorkerItemModel(
                    id=WORKER_ITEM_ID,
                    workspace_id=WORKSPACE_ID,
                    root_task_id=ROOT_TASK_ID,
                    stable_key="callback-implementer",
                    title="回调实现",
                    responsibility="实现回调幂等逻辑",
                    status="active",
                    context_version=4,
                    acknowledged_context_version=4,
                    derived_from_worker_item_ids=[],
                    version=7,
                    created_at=NOW,
                    updated_at=NOW + timedelta(minutes=8),
                ),
                WorkerModel(
                    id=WORKER_ID,
                    workspace_id=WORKSPACE_ID,
                    adapter_type="opencode",
                    display_name="OpenCode callback worker",
                    status="busy",
                    desired_state="active",
                    capabilities=["python"],
                    capacity=1,
                    current_load=1,
                    cost_per_hour_microunits=1,
                    startup_latency_ms=10,
                    last_heartbeat_at=NOW + timedelta(minutes=8),
                    version=2,
                    created_at=NOW,
                    updated_at=NOW + timedelta(minutes=8),
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                PlanSnapshotModel(
                    id=first.id,
                    schema_version=first.schema_version,
                    workspace_id=first.workspace_id,
                    root_task_id=first.root_task_id,
                    generation=first.generation,
                    parent_snapshot_id=None,
                    source_type=first.source_type,
                    source_ref=first.source_ref,
                    title=first.title,
                    goal_summary=first.goal_summary,
                    payload=first.to_payload(),
                    published_by=first.published_by,
                    published_at=first.published_at,
                ),
                PlanSnapshotModel(
                    id=second.id,
                    schema_version=second.schema_version,
                    workspace_id=second.workspace_id,
                    root_task_id=second.root_task_id,
                    generation=second.generation,
                    parent_snapshot_id=second.parent_snapshot_id,
                    source_type=second.source_type,
                    source_ref=second.source_ref,
                    title=second.title,
                    goal_summary=second.goal_summary,
                    payload=second.to_payload(),
                    published_by=second.published_by,
                    published_at=second.published_at,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                WorkerItemPlacementModel(
                    plan_snapshot_id=SNAPSHOT_ONE_ID,
                    plan_generation=1,
                    workspace_id=WORKSPACE_ID,
                    root_task_id=ROOT_TASK_ID,
                    worker_item_id=WORKER_ITEM_ID,
                    worker_item_key="callback-implementer",
                    subtask_key="callback-idempotency",
                    subtask_task_id=CALLBACK_TASK_ID,
                    role="implementer",
                    capability_requirements=["python"],
                    worker_policy_ref=None,
                    configuration_ref=None,
                    created_at=NOW,
                ),
                WorkerItemPlacementModel(
                    plan_snapshot_id=SNAPSHOT_TWO_ID,
                    plan_generation=2,
                    workspace_id=WORKSPACE_ID,
                    root_task_id=ROOT_TASK_ID,
                    worker_item_id=WORKER_ITEM_ID,
                    worker_item_key="callback-implementer",
                    subtask_key="callback-retry",
                    subtask_task_id=RETRY_TASK_ID,
                    role="implementer",
                    capability_requirements=["python"],
                    worker_policy_ref=None,
                    configuration_ref=None,
                    created_at=NOW + timedelta(minutes=2),
                ),
            ]
        )
        session.add(
            ExecutionModel(
                id=EXECUTION_ONE_ID,
                workspace_id=WORKSPACE_ID,
                task_id=CALLBACK_TASK_ID,
                lineage_id=EXECUTION_ONE_ID,
                attempt=1,
                max_attempts=3,
                purpose="implement callback",
                status="succeeded",
                capability_requirements=["python"],
                pinned_worker_id=WORKER_ID,
                assigned_worker_id=WORKER_ID,
                active_lease_id=None,
                worker_item_id=WORKER_ITEM_ID,
                last_fencing_token=0,
                not_before=NOW,
                failure_code=None,
                started_at=NOW,
                finished_at=NOW + timedelta(minutes=1),
                version=2,
                stop_requested=False,
                source_execution_id=None,
                source_session_id=None,
                created_at=NOW,
                updated_at=NOW + timedelta(minutes=1),
            )
        )
        session.flush()
        session.add(
            ExecutionModel(
                id=EXECUTION_TWO_ID,
                workspace_id=WORKSPACE_ID,
                task_id=RETRY_TASK_ID,
                lineage_id=EXECUTION_ONE_ID,
                attempt=2,
                max_attempts=3,
                purpose="continue callback",
                status="running",
                capability_requirements=["python"],
                pinned_worker_id=WORKER_ID,
                assigned_worker_id=WORKER_ID,
                active_lease_id=None,
                worker_item_id=WORKER_ITEM_ID,
                last_fencing_token=0,
                not_before=NOW + timedelta(minutes=4),
                failure_code=None,
                started_at=NOW + timedelta(minutes=4),
                finished_at=None,
                version=3,
                stop_requested=False,
                source_execution_id=EXECUTION_ONE_ID,
                source_session_id=None,
                created_at=NOW + timedelta(minutes=4),
                updated_at=NOW + timedelta(minutes=8),
            )
        )
        session.flush()
        session.add_all(
            [
                ExecutionSessionModel(
                    id=SESSION_ONE_ID,
                    workspace_id=WORKSPACE_ID,
                    execution_id=EXECUTION_ONE_ID,
                    worker_id=WORKER_ID,
                    adapter_session_id="session-old",
                    status="closed",
                    last_sequence=2,
                    last_activity_at=NOW + timedelta(minutes=1),
                    input_mode="queued",
                    adapter_module_id=None,
                    adapter_config_version=None,
                    workspace_module_id=None,
                    workspace_config_version=None,
                    capability_snapshot=None,
                    process_ref=None,
                    workspace_ref=None,
                    version=3,
                    created_at=NOW,
                    updated_at=NOW + timedelta(minutes=1),
                ),
                ExecutionSessionModel(
                    id=SESSION_TWO_ID,
                    workspace_id=WORKSPACE_ID,
                    execution_id=EXECUTION_TWO_ID,
                    worker_id=WORKER_ID,
                    adapter_session_id="session-current",
                    status="active",
                    last_sequence=3,
                    last_activity_at=NOW + timedelta(minutes=8),
                    input_mode="queued",
                    adapter_module_id=None,
                    adapter_config_version=None,
                    workspace_module_id=None,
                    workspace_config_version=None,
                    capability_snapshot=None,
                    process_ref=None,
                    workspace_ref=None,
                    version=4,
                    created_at=NOW + timedelta(minutes=4),
                    updated_at=NOW + timedelta(minutes=8),
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                ExecutionEventModel(
                    id=EVENT_ONE_ID,
                    workspace_id=WORKSPACE_ID,
                    execution_id=EXECUTION_TWO_ID,
                    session_id=SESSION_TWO_ID,
                    sequence=2,
                    event_type="tool.call",
                    payload={
                        "tool": "pytest",
                        "summary": "运行回调测试",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                        "arguments": {"path": "tests/callback.py"},
                    },
                    occurred_at=NOW + timedelta(minutes=7),
                    raw_ref="trace://tool/1",
                ),
                ExecutionEventModel(
                    id=EVENT_TWO_ID,
                    workspace_id=WORKSPACE_ID,
                    execution_id=EXECUTION_TWO_ID,
                    session_id=SESSION_TWO_ID,
                    sequence=3,
                    event_type="checkpoint.created",
                    payload={
                        "title": "测试完成",
                        "artifact_ids": [str(ARTIFACT_ID)],
                    },
                    occurred_at=NOW + timedelta(minutes=8),
                    raw_ref=None,
                ),
                ExecutionInputModel(
                    id=INPUT_ID,
                    workspace_id=WORKSPACE_ID,
                    execution_id=EXECUTION_TWO_ID,
                    session_id=SESSION_TWO_ID,
                    client_idempotency_key="worker-input-1",
                    text="只修改这个回调重试规则",
                    attachment_refs=[],
                    status="delivered",
                    operation_id=None,
                    failure_code=None,
                    version=2,
                    source_command_id=None,
                    created_at=NOW + timedelta(minutes=6),
                    updated_at=NOW + timedelta(minutes=6),
                ),
                ArtifactModel(
                    id=ARTIFACT_ID,
                    workspace_id=WORKSPACE_ID,
                    task_id=RETRY_TASK_ID,
                    execution_id=EXECUTION_TWO_ID,
                    session_id=SESSION_TWO_ID,
                    artifact_type="test-report",
                    storage_ref="artifact-local://report",
                    content_hash="a" * 64,
                    size_bytes=128,
                    media_type="text/plain",
                    provenance={},
                    created_at=NOW + timedelta(minutes=8),
                ),
                EvidenceClaimModel(
                    id=EVIDENCE_ID,
                    workspace_id=WORKSPACE_ID,
                    task_id=RETRY_TASK_ID,
                    execution_id=EXECUTION_TWO_ID,
                    acceptance_criterion_ref="acceptance:callback",
                    conclusion="回调测试通过",
                    environment_ref="worker-workspace",
                    artifact_ids=[str(ARTIFACT_ID)],
                    producer="integration",
                    status="candidate",
                    created_at=NOW + timedelta(minutes=8),
                ),
                ProjectionChangeModel(
                    workspace_id=WORKSPACE_ID,
                    cursor=31,
                    schema_version=1,
                    projection_type="worker_item_workspace",
                    resource_id=WORKER_ITEM_ID,
                    change_type="worker_item.updated",
                    resource_version=7,
                    occurred_at=NOW + timedelta(minutes=8),
                    correlation_id=EVENT_TWO_ID,
                ),
            ]
        )
        session.commit()


def test_worker_item_workspace_and_activity_are_stable_and_scoped(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    _seed(database)
    workspace_url = (
        f"/api/v1/workspaces/{workspace_id}/worker-items/{WORKER_ITEM_ID}/workspace"
    )
    activity_url = (
        f"/api/v1/workspaces/{workspace_id}/worker-items/{WORKER_ITEM_ID}/activity"
    )

    workspace_response = client.get(workspace_url)

    assert workspace_response.status_code == 200, workspace_response.text
    workspace = workspace_response.json()
    assert workspace["worker_item"]["id"] == str(WORKER_ITEM_ID)
    assert workspace["current_placement"]["subtask_key"] == "callback-retry"
    assert [item["plan_generation"] for item in workspace["placement_history"]] == [
        2,
        1,
    ]
    assert workspace["current_runtime"]["execution_id"] == str(EXECUTION_TWO_ID)
    assert workspace["current_runtime"]["session_id"] == str(SESSION_TWO_ID)
    assert workspace["current_runtime"]["worker_id"] == str(WORKER_ID)
    assert workspace["can_intervene"] is True
    assert workspace["read_only_reason"] is None
    assert workspace["intervention_context"] == {
        "worker_item_id": str(WORKER_ITEM_ID),
        "plan_generation": 2,
        "context_version": 4,
        "execution_id": str(EXECUTION_TWO_ID),
        "session_id": str(SESSION_TWO_ID),
        "session_version": 4,
        "worker_id": str(WORKER_ID),
    }
    assert workspace["artifacts"][0]["artifact_id"] == str(ARTIFACT_ID)
    assert workspace["evidence"][0]["acceptance_ref"] == "acceptance:callback"

    pages: list[dict[str, object]] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": 2}
        if cursor is not None:
            params["after"] = cursor
        response = client.get(activity_url, params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    items = [item for page in pages for item in page["items"]]
    assert len({item["activity_id"] for item in items}) == len(items)
    user_input = next(item for item in items if item["kind"] == "user_input")
    tool_event = next(item for item in items if item["kind"] == "tool")
    assert user_input["body"] == "只修改这个回调重试规则"
    assert user_input["delivery_status"] == "delivered"
    assert tool_event["body"] is None
    assert tool_event["trace_descriptor"] is not None

    invalid = client.get(activity_url, params={"after": "bad-cursor"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_cursor"

    with database.session() as session:
        session.execute(
            update(WorkerItemModel)
            .where(
                WorkerItemModel.workspace_id == WORKSPACE_ID,
                WorkerItemModel.id == WORKER_ITEM_ID,
            )
            .values(status="retired", version=8)
        )
        session.commit()
    retired = client.get(workspace_url)
    assert retired.status_code == 200
    assert retired.json()["can_intervene"] is False
    assert retired.json()["read_only_reason"] == "worker_item_retired"

    assert client.get(
        f"/api/v1/workspaces/{workspace_id}/worker-items/"
        "43000000-0000-0000-0000-000000000099/workspace"
    ).status_code == 404
    assert client.get(
        "/api/v1/workspaces/00000000-0000-0000-0000-000000000099/"
        f"worker-items/{WORKER_ITEM_ID}/workspace"
    ).status_code == 404
