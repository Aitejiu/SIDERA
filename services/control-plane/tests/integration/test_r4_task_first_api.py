from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.plans import (
    Dependency,
    DependencyEnforcement,
    DependencyKind,
    Milestone,
    SubtaskDependency,
    SubtaskMilestone,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemPlanItem,
    WorkerSlot,
    WorkItem,
    WorkItemKind,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    AssignmentLeaseModel,
    EvidenceClaimModel,
    ExecutionBindingModel,
    ExecutionEventModel,
    ExecutionModel,
    ExecutionSessionModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    PlanSnapshotModel,
    ProjectionChangeModel,
    TaskModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
    WorkerModel,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
BACKEND_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")
FRONTEND_TASK_ID = UUID("30000000-0000-0000-0000-000000000003")
BACKEND_EXECUTION_ID = UUID("70000000-0000-0000-0000-000000000001")
FRONTEND_EXECUTION_ID = UUID("70000000-0000-0000-0000-000000000002")
SESSION_ID = UUID("80000000-0000-0000-0000-000000000001")
WORKER_ID = UUID("60000000-0000-0000-0000-000000000001")
LEASE_ID = UUID("90000000-0000-0000-0000-000000000001")
ARTIFACT_ID = UUID("a0000000-0000-0000-0000-000000000001")
GENERATION_ONE_ID = UUID("10000000-0000-0000-0000-000000000001")
GENERATION_TWO_ID = UUID("10000000-0000-0000-0000-000000000002")
CONSOLE_ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000101")
CONSOLE_CALLBACK_TASK_ID = UUID("30000000-0000-0000-0000-000000000102")
CONSOLE_INTEGRATION_TASK_ID = UUID("30000000-0000-0000-0000-000000000103")
CONSOLE_SNAPSHOT_ID = UUID("10000000-0000-0000-0000-000000000101")
CONSOLE_IMPLEMENTER_ID = UUID("40000000-0000-0000-0000-000000000101")
CONSOLE_REVIEWER_ID = UUID("40000000-0000-0000-0000-000000000102")
CONSOLE_INTEGRATOR_ID = UUID("40000000-0000-0000-0000-000000000103")
CONSOLE_RUNNING_EXECUTION_ID = UUID("70000000-0000-0000-0000-000000000101")
CONSOLE_WAITING_EXECUTION_ID = UUID("70000000-0000-0000-0000-000000000102")


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(
        database_url=database_url,
        main_agent_entry_uri="http://127.0.0.1:18080/main-agent",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _work_item(
    key: str,
    task_id: UUID | None,
    title: str,
    *,
    milestone_key: str,
) -> WorkItem:
    return WorkItem(
        key=key,
        task_id=task_id,
        kind=WorkItemKind.CHILD,
        title=title,
        goal_summary=f"Deliver {key}",
        acceptance_refs=(f"acceptance:{key}",),
        milestone_key=milestone_key,
        worker_slot_key="backend",
        expected_outputs=(f"output:{key}",),
        source_refs=(f"source:{key}",),
    )


def _snapshot(*, generation: int, snapshot_id: UUID, parent: UUID | None) -> TaskPlanSnapshot:
    frontend_title = "Frontend implementation" if generation == 1 else "Frontend patch"
    work_items = (
        _work_item("backend", BACKEND_TASK_ID, "Backend implementation", milestone_key="m1"),
        _work_item("frontend", FRONTEND_TASK_ID, frontend_title, milestone_key="m1"),
        _work_item(
            "planned",
            None,
            "Planned documentation",
            milestone_key="m2",
        ),
    )
    if generation == 2:
        work_items = work_items + (
            _work_item("added", None, "Added documentation", milestone_key="m2"),
        )
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=1,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=generation,
        parent_snapshot_id=parent,
        source_type="test",
        source_ref=f"api:g{generation}",
        title="Task First integration",
        goal_summary="Expose the complete delivery graph.",
        acceptance_refs=("acceptance:root",),
        work_items=work_items,
        worker_slots=(
            WorkerSlot(
                key="backend",
                role="implementation",
                capability_requirements=("python",),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=1,
            ),
        ),
        milestones=(
            Milestone(
                key="m1",
                title="Parallel implementation",
                work_item_keys=("backend", "frontend"),
                acceptance_refs=(),
                order_hint=1,
            ),
            Milestone(
                key="m2",
                title="Documentation",
                work_item_keys=("planned", "added") if generation == 2 else ("planned",),
                acceptance_refs=(),
                order_hint=2,
            ),
        ),
        dependencies=(
            Dependency(
                upstream_work_item_key="backend",
                downstream_work_item_key="frontend",
                kind=DependencyKind.INFORMATIONAL,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.INFORMATIONAL,
            ),
        ),
        published_by="local:test",
        published_at=NOW + timedelta(minutes=generation),
    )


def _seed(database: Database) -> None:
    first = _snapshot(generation=1, snapshot_id=GENERATION_ONE_ID, parent=None)
    second = _snapshot(
        generation=2, snapshot_id=GENERATION_TWO_ID, parent=GENERATION_ONE_ID
    )
    adapter_definition_id = uuid4()
    workspace_definition_id = uuid4()
    adapter_instance_id = uuid4()
    workspace_instance_id = uuid4()
    capabilities = {
        "input": "queued",
        "idempotency": "operation-id",
        "resume": "none",
        "stop": "exact-execution",
        "sandbox": "filesystem",
        "stability": "experimental",
    }
    with database.session() as session:
        session.add_all(
            [
                TaskModel(
                    id=ROOT_TASK_ID,
                    workspace_id=WORKSPACE_ID,
                    title="Root delivery",
                    description="Complete delivery root",
                    acceptance_criteria=["acceptance:root"],
                    status="running",
                    priority=50,
                    requested_by="test",
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                TaskModel(
                    id=BACKEND_TASK_ID,
                    workspace_id=WORKSPACE_ID,
                    title="Backend implementation",
                    description="Implement the backend",
                    acceptance_criteria=["acceptance:backend"],
                    status="running",
                    priority=50,
                    requested_by="test",
                    version=2,
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=3),
                ),
                TaskModel(
                    id=FRONTEND_TASK_ID,
                    workspace_id=WORKSPACE_ID,
                    title="Frontend implementation",
                    description="Implement the frontend",
                    acceptance_criteria=["acceptance:frontend"],
                    status="pending",
                    priority=50,
                    requested_by="test",
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                WorkerModel(
                    id=WORKER_ID,
                    workspace_id=WORKSPACE_ID,
                    adapter_type="opencode",
                    display_name="OpenCode backend worker",
                    status="busy",
                    desired_state="active",
                    capabilities=["python"],
                    capacity=1,
                    current_load=1,
                    cost_per_hour_microunits=1,
                    startup_latency_ms=10,
                    last_heartbeat_at=NOW,
                    version=2,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                ModuleDefinitionModel(
                    id=adapter_definition_id,
                    workspace_id=WORKSPACE_ID,
                    module_type="worker_adapter",
                    provider="opencode",
                    interface_version="sidera.worker-adapter.v1",
                    config_schema_version=1,
                    capabilities=capabilities,
                    lifecycle="active",
                ),
                ModuleDefinitionModel(
                    id=workspace_definition_id,
                    workspace_id=WORKSPACE_ID,
                    module_type="workspace_provider",
                    provider="git-worktree",
                    interface_version="sidera.workspace-provider.v1",
                    config_schema_version=1,
                    capabilities=capabilities,
                    lifecycle="active",
                ),
                ModuleInstanceModel(
                    id=adapter_instance_id,
                    workspace_id=WORKSPACE_ID,
                    definition_id=adapter_definition_id,
                    instance_name="opencode-task-first",
                    config_version=1,
                    config_namespace="opencode",
                    config={},
                    secret_refs=[],
                    lifecycle="active",
                ),
                ModuleInstanceModel(
                    id=workspace_instance_id,
                    workspace_id=WORKSPACE_ID,
                    definition_id=workspace_definition_id,
                    instance_name="git-task-first",
                    config_version=1,
                    config_namespace="workspace",
                    config={},
                    secret_refs=[],
                    lifecycle="active",
                ),
            ]
        )
        session.flush()
        session.add(
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
            )
        )
        session.flush()
        session.add(
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
            )
        )
        session.add_all(
            [
                ExecutionModel(
                    id=BACKEND_EXECUTION_ID,
                    workspace_id=WORKSPACE_ID,
                    task_id=BACKEND_TASK_ID,
                    lineage_id=BACKEND_EXECUTION_ID,
                    attempt=1,
                    max_attempts=3,
                    purpose="implement backend",
                    status="running",
                    capability_requirements=["python"],
                    pinned_worker_id=WORKER_ID,
                    assigned_worker_id=WORKER_ID,
                    active_lease_id=None,
                    last_fencing_token=1,
                    not_before=NOW,
                    failure_code=None,
                    started_at=NOW,
                    finished_at=None,
                    version=3,
                    stop_requested=False,
                    source_execution_id=None,
                    source_session_id=None,
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=3),
                ),
                ExecutionModel(
                    id=FRONTEND_EXECUTION_ID,
                    workspace_id=WORKSPACE_ID,
                    task_id=FRONTEND_TASK_ID,
                    lineage_id=FRONTEND_EXECUTION_ID,
                    attempt=1,
                    max_attempts=3,
                    purpose="implement frontend",
                    status="queued",
                    capability_requirements=["python"],
                    pinned_worker_id=None,
                    assigned_worker_id=None,
                    active_lease_id=None,
                    last_fencing_token=0,
                    not_before=NOW,
                    failure_code=None,
                    started_at=None,
                    finished_at=None,
                    version=1,
                    stop_requested=False,
                    source_execution_id=None,
                    source_session_id=None,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                AssignmentLeaseModel(
                    id=LEASE_ID,
                    workspace_id=WORKSPACE_ID,
                    execution_id=BACKEND_EXECUTION_ID,
                    worker_id=WORKER_ID,
                    status="active",
                    issued_at=NOW,
                    expires_at=NOW + timedelta(minutes=5),
                    renewed_at=None,
                    ended_at=None,
                    fencing_token=1,
                    assignment_reason={"source": "integration"},
                    version=1,
                ),
                ExecutionSessionModel(
                    id=SESSION_ID,
                    workspace_id=WORKSPACE_ID,
                    execution_id=BACKEND_EXECUTION_ID,
                    worker_id=WORKER_ID,
                    adapter_session_id="task-first-session",
                    status="active",
                    last_sequence=2,
                    last_activity_at=NOW + timedelta(seconds=3),
                    input_mode="queued",
                    adapter_module_id=adapter_instance_id,
                    adapter_config_version=1,
                    workspace_module_id=workspace_instance_id,
                    workspace_config_version=1,
                    capability_snapshot=capabilities,
                    process_ref="opaque-process",
                    workspace_ref="opaque-workspace",
                    version=2,
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=3),
                ),
            ]
        )
        session.flush()
        session.add(
            ExecutionBindingModel(
                id=uuid4(),
                workspace_id=WORKSPACE_ID,
                execution_id=BACKEND_EXECUTION_ID,
                worker_id=WORKER_ID,
                adapter_module_id=adapter_instance_id,
                workspace_module_id=workspace_instance_id,
                adapter_config_version=1,
                workspace_config_version=1,
                capability_snapshot=capabilities,
                lease_id=LEASE_ID,
                fencing_token=1,
                version=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            ProjectionChangeModel(
                workspace_id=WORKSPACE_ID,
                cursor=7,
                schema_version=1,
                projection_type="task_first",
                resource_id=BACKEND_TASK_ID,
                change_type="task.updated",
                resource_version=2,
                occurred_at=NOW + timedelta(seconds=3),
                correlation_id=uuid4(),
            )
        )
        session.add_all(
            [
                ExecutionEventModel(
                    id=uuid4(),
                    workspace_id=WORKSPACE_ID,
                    execution_id=BACKEND_EXECUTION_ID,
                    session_id=SESSION_ID,
                    sequence=1,
                    event_type="checkpoint.created",
                    payload={"title": "Backend checkpoint", "artifact_ids": [str(ARTIFACT_ID)]},
                    occurred_at=NOW + timedelta(seconds=1),
                    raw_ref=None,
                ),
                ExecutionEventModel(
                    id=uuid4(),
                    workspace_id=WORKSPACE_ID,
                    execution_id=BACKEND_EXECUTION_ID,
                    session_id=SESSION_ID,
                    sequence=2,
                    event_type="assistant.summary",
                    payload={"summary": "Running backend tests"},
                    occurred_at=NOW + timedelta(seconds=3),
                    raw_ref=None,
                ),
                ArtifactModel(
                    id=ARTIFACT_ID,
                    workspace_id=WORKSPACE_ID,
                    task_id=BACKEND_TASK_ID,
                    execution_id=BACKEND_EXECUTION_ID,
                    session_id=SESSION_ID,
                    artifact_type="report",
                    storage_ref="artifact://task-first/report",
                    content_hash="a" * 64,
                    size_bytes=256,
                    media_type="text/plain",
                    provenance={"producer": "integration"},
                    created_at=NOW + timedelta(seconds=2),
                ),
                EvidenceClaimModel(
                    id=uuid4(),
                    workspace_id=WORKSPACE_ID,
                    task_id=BACKEND_TASK_ID,
                    execution_id=BACKEND_EXECUTION_ID,
                    acceptance_criterion_ref="acceptance:backend",
                    conclusion="Backend tests are running.",
                    environment_ref="task-first",
                    artifact_ids=[str(ARTIFACT_ID)],
                    producer="integration",
                    status="candidate",
                    created_at=NOW + timedelta(seconds=2),
                ),
            ]
        )
        session.add(
            ProjectionChangeModel(
                workspace_id=WORKSPACE_ID,
                cursor=8,
                schema_version=1,
                projection_type="worker_trace",
                resource_id=BACKEND_EXECUTION_ID,
                change_type="execution_event.appended",
                resource_version=2,
                occurred_at=NOW + timedelta(seconds=3),
                correlation_id=uuid4(),
            )
        )
        session.add(
            ProjectionChangeModel(
                workspace_id=WORKSPACE_ID,
                cursor=9,
                schema_version=1,
                projection_type="task_first",
                resource_id=BACKEND_TASK_ID,
                change_type="evidence.created",
                resource_version=2,
                occurred_at=NOW + timedelta(seconds=3),
                correlation_id=uuid4(),
            )
        )
        session.flush()
        session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == BACKEND_EXECUTION_ID)
            .values(active_lease_id=LEASE_ID)
        )
        session.commit()


def test_task_first_read_paths_share_one_projection_contract(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    _seed(database)
    params = {"workspace_id": str(workspace_id)}

    overviews = client.get("/api/v1/workspaces/00000000-0000-0000-0000-000000000001/task-overviews")
    overview = client.get(f"/api/v1/tasks/{ROOT_TASK_ID}/overview", params=params)
    graph = client.get(f"/api/v1/tasks/{ROOT_TASK_ID}/graph", params=params)
    detail = client.get(
        f"/api/v1/tasks/{ROOT_TASK_ID}/work-items/backend", params=params
    )
    activity = client.get(
        f"/api/v1/tasks/{ROOT_TASK_ID}/activity", params={**params, "limit": 50}
    )
    diff = client.get(
        f"/api/v1/tasks/{ROOT_TASK_ID}/plan-diff",
        params={**params, "left": 1, "right": 2},
    )
    capabilities = client.get(
        "/api/v1/workspaces/00000000-0000-0000-0000-000000000001/capabilities"
    )

    assert overviews.status_code == 200, overviews.text
    assert overview.status_code == 200, overview.text
    assert graph.status_code == 200, graph.text
    assert detail.status_code == 200, detail.text
    assert activity.status_code == 200, activity.text
    assert diff.status_code == 200, diff.text
    assert capabilities.status_code == 200, capabilities.text

    assert overview.json()["schema_version"] == 1
    assert overview.json()["generation"] == 2
    assert overview.json()["subtask_count"] == 4
    assert overview.json()["planned_subtask_count"] == 2
    assert graph.json()["overview"] == overview.json()
    nodes = graph.json()["nodes"]
    assert [node["subtask_key"] for node in nodes] == [
        "backend",
        "frontend",
        "planned",
        "added",
    ]
    assert nodes[0]["state"] == "running"
    assert nodes[1]["state"] == "running"
    assert set(nodes[0]) >= {
        "subtask_key",
        "task_id",
        "title",
        "goal_summary",
        "state",
        "worker_item_count",
        "running_worker_item_count",
        "attention_worker_item_count",
        "evidence_required",
        "evidence_satisfied",
    }
    assert "work_item_key" not in nodes[0]
    assert "worker_display_name" not in nodes[0]
    assert "execution_id" not in nodes[0]
    assert detail.json()["node"]["work_item_key"] == "backend"
    assert len(detail.json()["executions"]) == 1
    assert detail.json()["artifacts"][0]["artifact_id"] == str(ARTIFACT_ID)
    assert detail.json()["evidence"][0]["acceptance_ref"] == "acceptance:backend"
    assert activity.json()["schema_version"] == 1
    assert activity.json()["items"]
    assert activity.json()["items"][0]["subtask_key"] == "backend"
    diff_entries = {entry["work_item_key"]: entry for entry in diff.json()["entries"]}
    assert diff_entries["backend"]["change"] == "unchanged"
    assert diff_entries["frontend"]["change"] == "patched"
    assert diff_entries["added"]["change"] == "added"
    assert capabilities.json()["main_agent"] == {
        "available": False,
        "request_revision_supported": False,
        "entry_uri": "http://127.0.0.1:18080/main-agent",
    }


def _console_snapshot() -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=CONSOLE_SNAPSHOT_ID,
        schema_version=2,
        workspace_id=WORKSPACE_ID,
        root_task_id=CONSOLE_ROOT_TASK_ID,
        generation=3,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="api:task-console",
        title="支付重构",
        goal_summary="完成支付回调链路重构",
        acceptance_refs=("acceptance:root",),
        published_by="local:test",
        published_at=NOW,
        subtasks=(
            SubtaskPlanItem(
                key="callback-idempotency",
                task_id=CONSOLE_CALLBACK_TASK_ID,
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
                task_id=CONSOLE_INTEGRATION_TASK_ID,
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
                worker_item_id=CONSOLE_IMPLEMENTER_ID,
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
                worker_item_id=CONSOLE_REVIEWER_ID,
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
                worker_item_id=CONSOLE_INTEGRATOR_ID,
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
    )


def _seed_console(database: Database) -> None:
    snapshot = _console_snapshot()
    with database.session() as session:
        session.add_all(
            [
                TaskModel(
                    id=CONSOLE_ROOT_TASK_ID,
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
                    id=CONSOLE_CALLBACK_TASK_ID,
                    workspace_id=WORKSPACE_ID,
                    title="回调幂等",
                    description="保证支付回调可安全重试",
                    acceptance_criteria=["acceptance:callback"],
                    status="running",
                    priority=50,
                    requested_by="test",
                    version=4,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                TaskModel(
                    id=CONSOLE_INTEGRATION_TASK_ID,
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
                *[
                    WorkerItemModel(
                        id=item_id,
                        workspace_id=WORKSPACE_ID,
                        root_task_id=CONSOLE_ROOT_TASK_ID,
                        stable_key=stable_key,
                        title=title,
                        responsibility=responsibility,
                        status="active",
                        context_version=1,
                        acknowledged_context_version=1,
                        derived_from_worker_item_ids=[],
                        version=1,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                    for item_id, stable_key, title, responsibility in (
                        (
                            CONSOLE_IMPLEMENTER_ID,
                            "callback-implementer",
                            "回调实现",
                            "实现回调幂等逻辑",
                        ),
                        (
                            CONSOLE_REVIEWER_ID,
                            "callback-reviewer",
                            "回调复核",
                            "确认重试规则和验收证据",
                        ),
                        (
                            CONSOLE_INTEGRATOR_ID,
                            "payment-integrator",
                            "支付集成",
                            "执行支付链路集成验证",
                        ),
                    )
                ],
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
        session.flush()
        session.add_all(
            [
                WorkerItemPlacementModel(
                    plan_snapshot_id=CONSOLE_SNAPSHOT_ID,
                    plan_generation=3,
                    workspace_id=WORKSPACE_ID,
                    root_task_id=CONSOLE_ROOT_TASK_ID,
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
                        CONSOLE_IMPLEMENTER_ID,
                        "callback-implementer",
                        "callback-idempotency",
                        CONSOLE_CALLBACK_TASK_ID,
                        "implementer",
                    ),
                    (
                        CONSOLE_REVIEWER_ID,
                        "callback-reviewer",
                        "callback-idempotency",
                        CONSOLE_CALLBACK_TASK_ID,
                        "reviewer",
                    ),
                    (
                        CONSOLE_INTEGRATOR_ID,
                        "payment-integrator",
                        "integration",
                        CONSOLE_INTEGRATION_TASK_ID,
                        "integrator",
                    ),
                )
            ]
        )
        session.add_all(
            [
                ExecutionModel(
                    id=CONSOLE_RUNNING_EXECUTION_ID,
                    workspace_id=WORKSPACE_ID,
                    task_id=CONSOLE_CALLBACK_TASK_ID,
                    lineage_id=CONSOLE_RUNNING_EXECUTION_ID,
                    attempt=1,
                    max_attempts=3,
                    purpose="implement callback",
                    status="running",
                    capability_requirements=["python"],
                    pinned_worker_id=None,
                    assigned_worker_id=None,
                    active_lease_id=None,
                    worker_item_id=CONSOLE_IMPLEMENTER_ID,
                    last_fencing_token=0,
                    not_before=NOW,
                    failure_code=None,
                    started_at=NOW,
                    finished_at=None,
                    version=2,
                    stop_requested=False,
                    source_execution_id=None,
                    source_session_id=None,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                ExecutionModel(
                    id=CONSOLE_WAITING_EXECUTION_ID,
                    workspace_id=WORKSPACE_ID,
                    task_id=CONSOLE_CALLBACK_TASK_ID,
                    lineage_id=CONSOLE_WAITING_EXECUTION_ID,
                    attempt=1,
                    max_attempts=3,
                    purpose="review callback",
                    status="waiting_user",
                    capability_requirements=["python"],
                    pinned_worker_id=None,
                    assigned_worker_id=None,
                    active_lease_id=None,
                    worker_item_id=CONSOLE_REVIEWER_ID,
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
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                EvidenceClaimModel(
                    id=uuid4(),
                    workspace_id=WORKSPACE_ID,
                    task_id=CONSOLE_CALLBACK_TASK_ID,
                    execution_id=CONSOLE_RUNNING_EXECUTION_ID,
                    acceptance_criterion_ref="acceptance:callback",
                    conclusion="回调重试规则已有测试证据",
                    environment_ref="task-console",
                    artifact_ids=[],
                    producer="integration",
                    status="candidate",
                    created_at=NOW,
                ),
                ProjectionChangeModel(
                    workspace_id=WORKSPACE_ID,
                    cursor=17,
                    schema_version=1,
                    projection_type="task_console",
                    resource_id=CONSOLE_ROOT_TASK_ID,
                    change_type="worker_item.updated",
                    resource_version=1,
                    occurred_at=NOW,
                    correlation_id=uuid4(),
                ),
            ]
        )
        session.commit()


def test_task_console_graph_exposes_subtasks_and_worker_item_summary(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    _seed_console(database)
    response = client.get(
        f"/api/v1/tasks/{CONSOLE_ROOT_TASK_ID}/graph",
        params={"workspace_id": str(workspace_id)},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [node["subtask_key"] for node in payload["nodes"]] == [
        "callback-idempotency",
        "integration",
    ]
    callback = payload["nodes"][0]
    assert callback["worker_item_count"] == 2
    assert callback["running_worker_item_count"] == 1
    assert callback["attention_worker_item_count"] == 1
    assert callback["state"] == "waiting_user"
    assert callback["evidence_required"] == 1
    assert callback["evidence_satisfied"] == 1
    assert "work_item_key" not in callback
    assert "worker_display_name" not in callback
    assert "execution_id" not in callback
    assert payload["edges"] == [
        {
            "upstream_subtask_key": "callback-idempotency",
            "downstream_subtask_key": "integration",
            "kind": "hard",
        }
    ]
    assert payload["overview"]["attention_items"][0]["task_title"] == "支付重构"
    assert payload["overview"]["attention_items"][0]["subtask_title"] == "回调幂等"
