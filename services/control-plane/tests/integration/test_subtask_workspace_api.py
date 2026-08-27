from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.plans import (
    DependencyEnforcement,
    DependencyKind,
    SubtaskDependency,
    SubtaskMilestone,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemDependency,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    EvidenceClaimModel,
    ExecutionModel,
    PlanSnapshotModel,
    ProjectionChangeModel,
    RootTaskPlanHeadModel,
    TaskModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("31000000-0000-0000-0000-000000000001")
CALLBACK_TASK_ID = UUID("31000000-0000-0000-0000-000000000002")
INTEGRATION_TASK_ID = UUID("31000000-0000-0000-0000-000000000003")
SNAPSHOT_ID = UUID("11000000-0000-0000-0000-000000000001")
IMPLEMENTER_ID = UUID("41000000-0000-0000-0000-000000000001")
REVIEWER_ID = UUID("41000000-0000-0000-0000-000000000002")
INTEGRATOR_ID = UUID("41000000-0000-0000-0000-000000000003")
EXECUTION_ID = UUID("71000000-0000-0000-0000-000000000001")
EVIDENCE_ID = UUID("a1000000-0000-0000-0000-000000000001")


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url))) as test_client:
        yield test_client


def _snapshot() -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=SNAPSHOT_ID,
        schema_version=2,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=1,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="api:subtask-workspace",
        title="支付重构",
        goal_summary="完成支付回调链路重构",
        acceptance_refs=("acceptance:root",),
        published_by="local:test",
        published_at=NOW,
        subtasks=(
            SubtaskPlanItem(
                key="callback-idempotency",
                task_id=CALLBACK_TASK_ID,
                kind=WorkItemKind.CHILD,
                title="回调幂等",
                goal_summary="保证支付回调可安全重试",
                acceptance_refs=("acceptance:callback",),
                milestone_key="implementation",
                expected_outputs=("callback patch",),
                source_refs=("planner:callback",),
            ),
            SubtaskPlanItem(
                key="integration",
                task_id=INTEGRATION_TASK_ID,
                kind=WorkItemKind.INTEGRATION,
                title="支付集成",
                goal_summary="完成支付链路集成验证",
                acceptance_refs=("acceptance:integration",),
                milestone_key="verification",
                expected_outputs=("integration report",),
                source_refs=("planner:integration",),
            ),
        ),
        worker_item_plans=(
            WorkerItemPlanItem(
                key="callback-implementer",
                worker_item_id=IMPLEMENTER_ID,
                subtask_key="callback-idempotency",
                title="回调实现",
                role="implementer",
                responsibility="实现回调幂等逻辑",
                capability_requirements=("python",),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=("planner:callback-implementer",),
            ),
            WorkerItemPlanItem(
                key="callback-reviewer",
                worker_item_id=REVIEWER_ID,
                subtask_key="callback-idempotency",
                title="回调复核",
                role="reviewer",
                responsibility="确认重试规则和验收证据",
                capability_requirements=("python",),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=("planner:callback-reviewer",),
            ),
            WorkerItemPlanItem(
                key="payment-integrator",
                worker_item_id=INTEGRATOR_ID,
                subtask_key="integration",
                title="支付集成",
                role="integrator",
                responsibility="执行支付链路集成验证",
                capability_requirements=("python",),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=("planner:payment-integrator",),
            ),
        ),
        subtask_milestones=(
            SubtaskMilestone(
                key="implementation",
                title="实现",
                subtask_keys=("callback-idempotency",),
                acceptance_refs=(),
                order_hint=1,
            ),
            SubtaskMilestone(
                key="verification",
                title="验证",
                subtask_keys=("integration",),
                acceptance_refs=(),
                order_hint=2,
            ),
        ),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="callback-idempotency",
                downstream_subtask_key="integration",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
        ),
        worker_item_dependencies=(
            WorkerItemDependency(
                upstream_worker_item_key="callback-implementer",
                downstream_worker_item_key="callback-reviewer",
                kind="review",
                required_artifact_refs=("report",),
            ),
        ),
    )


def _seed(database: Database) -> None:
    snapshot = _snapshot()
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
                    status="running",
                    priority=50,
                    requested_by="test",
                    version=2,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                TaskModel(
                    id=INTEGRATION_TASK_ID,
                    workspace_id=WORKSPACE_ID,
                    title="支付集成",
                    description="完成支付链路集成验证",
                    acceptance_criteria=["acceptance:integration"],
                    status="pending",
                    priority=50,
                    requested_by="test",
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                WorkerItemModel(
                    id=IMPLEMENTER_ID,
                    workspace_id=WORKSPACE_ID,
                    root_task_id=ROOT_TASK_ID,
                    stable_key="callback-implementer",
                    title="回调实现",
                    responsibility="实现回调幂等逻辑",
                    status="active",
                    context_version=2,
                    acknowledged_context_version=2,
                    derived_from_worker_item_ids=[],
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                WorkerItemModel(
                    id=REVIEWER_ID,
                    workspace_id=WORKSPACE_ID,
                    root_task_id=ROOT_TASK_ID,
                    stable_key="callback-reviewer",
                    title="回调复核",
                    responsibility="确认重试规则和验收证据",
                    status="active",
                    context_version=1,
                    acknowledged_context_version=1,
                    derived_from_worker_item_ids=[],
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                WorkerItemModel(
                    id=INTEGRATOR_ID,
                    workspace_id=WORKSPACE_ID,
                    root_task_id=ROOT_TASK_ID,
                    stable_key="payment-integrator",
                    title="支付集成",
                    responsibility="执行支付链路集成验证",
                    status="active",
                    context_version=1,
                    acknowledged_context_version=1,
                    derived_from_worker_item_ids=[],
                    version=1,
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
        session.flush()
        session.add_all(
            [
                WorkerItemPlacementModel(
                    plan_snapshot_id=SNAPSHOT_ID,
                    plan_generation=1,
                    workspace_id=WORKSPACE_ID,
                    root_task_id=ROOT_TASK_ID,
                    worker_item_id=item_id,
                    worker_item_key=worker_item_key,
                    subtask_key=subtask_key,
                    subtask_task_id=task_id,
                    role=role,
                    capability_requirements=["python"],
                    worker_policy_ref=None,
                    configuration_ref=None,
                    created_at=NOW,
                )
                for item_id, worker_item_key, subtask_key, task_id, role in (
                    (
                        IMPLEMENTER_ID,
                        "callback-implementer",
                        "callback-idempotency",
                        CALLBACK_TASK_ID,
                        "implementer",
                    ),
                    (
                        REVIEWER_ID,
                        "callback-reviewer",
                        "callback-idempotency",
                        CALLBACK_TASK_ID,
                        "reviewer",
                    ),
                    (
                        INTEGRATOR_ID,
                        "payment-integrator",
                        "integration",
                        INTEGRATION_TASK_ID,
                        "integrator",
                    ),
                )
            ]
        )
        session.add(
            ExecutionModel(
                id=EXECUTION_ID,
                workspace_id=WORKSPACE_ID,
                task_id=CALLBACK_TASK_ID,
                lineage_id=EXECUTION_ID,
                attempt=1,
                max_attempts=3,
                purpose="implement callback",
                status="running",
                capability_requirements=["python"],
                pinned_worker_id=None,
                assigned_worker_id=None,
                active_lease_id=None,
                worker_item_id=IMPLEMENTER_ID,
                last_fencing_token=0,
                not_before=NOW,
                failure_code=None,
                started_at=NOW,
                finished_at=None,
                version=1,
                stop_requested=False,
                source_execution_id=None,
                source_session_id=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add_all(
            [
                EvidenceClaimModel(
                    id=EVIDENCE_ID,
                    workspace_id=WORKSPACE_ID,
                    task_id=CALLBACK_TASK_ID,
                    execution_id=EXECUTION_ID,
                    acceptance_criterion_ref="acceptance:callback",
                    conclusion="回调规则已有测试证据",
                    environment_ref="subtask-workspace",
                    artifact_ids=[],
                    producer="integration",
                    status="candidate",
                    created_at=NOW,
                ),
                ProjectionChangeModel(
                    workspace_id=WORKSPACE_ID,
                    cursor=21,
                    schema_version=1,
                    projection_type="subtask_workspace",
                    resource_id=ROOT_TASK_ID,
                    change_type="worker_item.updated",
                    resource_version=1,
                    occurred_at=NOW,
                    correlation_id=EVIDENCE_ID,
                ),
            ]
        )
        session.commit()


def test_subtask_workspace_returns_local_worker_graph_and_isolates_scope(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    _seed(database)
    url = f"/api/v1/tasks/{ROOT_TASK_ID}/subtasks/callback-idempotency"

    response = client.get(url, params={"workspace_id": str(workspace_id)})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["subtask"]["key"] == "callback-idempotency"
    assert {
        item["worker_item_id"] for item in payload["worker_items"]
    } == {str(IMPLEMENTER_ID), str(REVIEWER_ID)}
    assert payload["worker_graph"]["worker_item_ids"] == [
        str(IMPLEMENTER_ID),
        str(REVIEWER_ID),
    ]
    assert payload["worker_graph"]["edges"] == [
        {
            "upstream_worker_item_id": str(IMPLEMENTER_ID),
            "downstream_worker_item_id": str(REVIEWER_ID),
            "kind": "review",
        }
    ]
    assert payload["downstream_subtasks"] == [
        {"subtask_key": "integration", "title": "支付集成", "state": "waiting_dependency"}
    ]
    assert payload["evidence"][0]["acceptance_ref"] == "acceptance:callback"
    assert "trace_events" not in payload
    assert "intervention_composer_uri" not in payload

    assert client.get(
        f"/api/v1/tasks/{ROOT_TASK_ID}/subtasks/unknown",
        params={"workspace_id": str(workspace_id)},
    ).status_code == 404
    assert client.get(
        url,
        params={"workspace_id": "00000000-0000-0000-0000-000000000099"},
    ).status_code == 404
