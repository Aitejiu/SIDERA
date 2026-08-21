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
        TaskPlanSnapshot,
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
    assert overview.json()["planned_count"] == 2
    assert graph.json()["overview"] == overview.json()
    nodes = graph.json()["nodes"]
    assert [node["work_item_key"] for node in nodes] == [
        "backend",
        "frontend",
        "planned",
        "added",
    ]
    assert nodes[0]["state"] == "running"
    assert nodes[0]["worker_display_name"] == "OpenCode backend worker"
    assert nodes[1]["state"] == "queued"
    assert all(edge["scheduler_enforced"] is False for edge in graph.json()["edges"])
    assert detail.json()["node"] == nodes[0]
    assert len(detail.json()["executions"]) == 1
    assert detail.json()["artifacts"][0]["artifact_id"] == str(ARTIFACT_ID)
    assert detail.json()["evidence"][0]["acceptance_ref"] == "acceptance:backend"
    assert activity.json()["schema_version"] == 1
    assert activity.json()["items"]
    diff_entries = {entry["work_item_key"]: entry for entry in diff.json()["entries"]}
    assert diff_entries["backend"]["change"] == "unchanged"
    assert diff_entries["frontend"]["change"] == "patched"
    assert diff_entries["added"]["change"] == "added"
    assert capabilities.json()["main_agent"] == {
        "available": False,
        "request_revision_supported": False,
        "entry_uri": "http://127.0.0.1:18080/main-agent",
    }
