from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    AssignmentLeaseModel,
    EvidenceClaimModel,
    ExecutionBindingModel,
    ExecutionEventModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionSessionModel,
    ExecutionWorkspaceModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    TaskModel,
    WorkerModel,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url))) as test_client:
        yield test_client


def _seed_gateway_projection(database: Database, workspace_id: UUID) -> dict[str, UUID]:
    task_id = uuid4()
    worker_id = uuid4()
    execution_id = uuid4()
    lease_id = uuid4()
    session_id = uuid4()
    closed_session_id = uuid4()
    adapter_definition_id = uuid4()
    workspace_definition_id = uuid4()
    adapter_instance_id = uuid4()
    workspace_instance_id = uuid4()
    artifact_id = uuid4()

    capabilities = {
        "input": "unsupported",
        "idempotency": "none",
        "resume": "unsupported",
        "stop": "unsupported",
        "sandbox": "filesystem",
        "stability": "experimental",
    }
    execution = ExecutionModel(
        id=execution_id,
        workspace_id=workspace_id,
        task_id=task_id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=3,
        purpose="query progress",
        status="running",
        capability_requirements=["code"],
        pinned_worker_id=worker_id,
        assigned_worker_id=worker_id,
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
    )
    with database.session() as session:
        session.add_all(
            [
                TaskModel(
                    id=task_id,
                    workspace_id=workspace_id,
                    title="R3 query task",
                    description="Expose gateway progress",
                    acceptance_criteria=["progress is queryable"],
                    status="running",
                    priority=50,
                    requested_by="integration",
                    version=2,
                ),
                WorkerModel(
                    id=worker_id,
                    workspace_id=workspace_id,
                    adapter_type="opencode",
                    display_name="OpenCode query worker",
                    status="running",
                    desired_state="active",
                    capabilities=["code"],
                    capacity=1,
                    current_load=1,
                    cost_per_hour_microunits=1,
                    startup_latency_ms=10,
                    last_heartbeat_at=NOW,
                    version=2,
                ),
                ModuleDefinitionModel(
                    id=adapter_definition_id,
                    workspace_id=workspace_id,
                    module_type="worker_adapter",
                    provider="opencode",
                    interface_version="sidera.worker-adapter.v1",
                    config_schema_version=1,
                    capabilities=capabilities,
                    lifecycle="active",
                ),
                ModuleDefinitionModel(
                    id=workspace_definition_id,
                    workspace_id=workspace_id,
                    module_type="workspace_provider",
                    provider="git-worktree",
                    interface_version="sidera.workspace-provider.v1",
                    config_schema_version=1,
                    capabilities=capabilities,
                    lifecycle="active",
                ),
                ModuleInstanceModel(
                    id=adapter_instance_id,
                    workspace_id=workspace_id,
                    definition_id=adapter_definition_id,
                    instance_name="opencode-query-adapter",
                    config_version=1,
                    config_namespace="opencode",
                    config={"opencode.executable": "opencode"},
                    secret_refs=[],
                    lifecycle="active",
                ),
                ModuleInstanceModel(
                    id=workspace_instance_id,
                    workspace_id=workspace_id,
                    definition_id=workspace_definition_id,
                    instance_name="git-query-workspace",
                    config_version=1,
                    config_namespace="workspace",
                    config={
                        "workspace.repository_ref": "repo://query",
                        "workspace.base_revision": "b" * 40,
                    },
                    secret_refs=[],
                    lifecycle="active",
                ),
            ]
        )
        session.flush()
        session.add(execution)
        session.flush()
        session.add_all(
            [
                AssignmentLeaseModel(
                    id=lease_id,
                    workspace_id=workspace_id,
                    execution_id=execution_id,
                    worker_id=worker_id,
                    status="active",
                    issued_at=NOW,
                    expires_at=NOW + timedelta(minutes=5),
                    renewed_at=None,
                    ended_at=None,
                    fencing_token=1,
                    assignment_reason={"source": "query-test"},
                    version=1,
                ),
                ExecutionBindingModel(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    execution_id=execution_id,
                    worker_id=worker_id,
                    adapter_module_id=adapter_instance_id,
                    workspace_module_id=workspace_instance_id,
                    adapter_config_version=1,
                    workspace_config_version=1,
                    capability_snapshot=capabilities,
                    lease_id=lease_id,
                    fencing_token=1,
                    version=1,
                ),
                ExecutionSessionModel(
                    id=session_id,
                    workspace_id=workspace_id,
                    execution_id=execution_id,
                    worker_id=worker_id,
                    adapter_session_id="opencode-query-session",
                    status="active",
                    last_sequence=3,
                    last_activity_at=NOW + timedelta(seconds=3),
                    input_mode="unsupported",
                    adapter_module_id=adapter_instance_id,
                    adapter_config_version=1,
                    workspace_module_id=workspace_instance_id,
                    workspace_config_version=1,
                    capability_snapshot=capabilities,
                    process_ref="opaque-process-ref",
                    workspace_ref="query-workspace",
                    version=4,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                ExecutionSessionModel(
                    id=closed_session_id,
                    workspace_id=workspace_id,
                    execution_id=execution_id,
                    worker_id=worker_id,
                    adapter_session_id="opencode-query-closed-session",
                    status="closed",
                    last_sequence=1,
                    last_activity_at=NOW + timedelta(seconds=4),
                    input_mode="unsupported",
                    adapter_module_id=adapter_instance_id,
                    adapter_config_version=1,
                    workspace_module_id=workspace_instance_id,
                    workspace_config_version=1,
                    capability_snapshot=capabilities,
                    process_ref="opaque-closed-process-ref",
                    workspace_ref="query-workspace",
                    version=2,
                    created_at=NOW + timedelta(seconds=1),
                    updated_at=NOW + timedelta(seconds=1),
                ),
                ExecutionWorkspaceModel(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    task_id=task_id,
                    execution_id=execution_id,
                    checkouts=[
                        {
                            "repository_ref": "repo://query",
                            "branch": "sidera/execution/query",
                            "current_head": "b" * 40,
                        }
                    ],
                    location_ref="worktree://query",
                    lifecycle="active",
                    retention_seconds=3600,
                    retain_until=None,
                    fencing_token=1,
                    frozen_at=None,
                    released_at=None,
                    version=1,
                ),
            ]
        )
        session.flush()
        session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == execution_id)
            .values(active_lease_id=lease_id)
        )
        session.add_all(
            [
                ExecutionEventModel(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    execution_id=execution_id,
                    session_id=session_id,
                    sequence=1,
                    event_type="session.started",
                    payload={"source": "query-test"},
                    occurred_at=NOW,
                    raw_ref=None,
                ),
                ExecutionEventModel(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    execution_id=execution_id,
                    session_id=session_id,
                    sequence=2,
                    event_type="checkpoint.created",
                    payload={"boundary": "prompt_complete"},
                    occurred_at=NOW + timedelta(seconds=1),
                    raw_ref=None,
                ),
                ExecutionEventModel(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    execution_id=execution_id,
                    session_id=session_id,
                    sequence=3,
                    event_type="input.accepted",
                    payload={"text_length": 12},
                    occurred_at=NOW + timedelta(seconds=2),
                    raw_ref=None,
                ),
                ArtifactModel(
                    id=artifact_id,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    execution_id=execution_id,
                    session_id=session_id,
                    artifact_type="diff",
                    storage_ref="artifact://query.diff",
                    content_hash="a" * 64,
                    size_bytes=128,
                    media_type="text/plain",
                    provenance={"producer": "opencode-query-adapter"},
                    created_at=NOW,
                ),
                EvidenceClaimModel(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    task_id=task_id,
                    execution_id=execution_id,
                    acceptance_criterion_ref="progress is queryable",
                    conclusion="The gateway projection is available.",
                    environment_ref="query-workspace",
                    artifact_ids=[str(artifact_id)],
                    producer="opencode-query-adapter",
                    status="candidate",
                    created_at=NOW,
                ),
                ExecutionInputModel(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    execution_id=execution_id,
                    session_id=session_id,
                    client_idempotency_key="query-input-1",
                    text="continue task",
                    attachment_refs=["artifact://context"],
                    status="queued",
                    operation_id=None,
                    failure_code=None,
                    version=1,
                    source_command_id=None,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        session.commit()
    return {
        "task_id": task_id,
        "execution_id": execution_id,
        "session_id": session_id,
        "closed_session_id": closed_session_id,
        "artifact_id": artifact_id,
    }


def test_gateway_query_projections_filters_and_replay_cursor(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_gateway_projection(database, workspace_id)
    params = {"workspace_id": str(workspace_id)}

    execution = client.get(
        "/api/v1/executions",
        params={**params, "status": "running"},
    )
    binding = client.get(
        f"/api/v1/gateway/executions/{ids['execution_id']}/binding",
        params=params,
    )
    workspace = client.get(
        f"/api/v1/gateway/executions/{ids['execution_id']}/workspace",
        params=params,
    )
    assert execution.status_code == 200
    assert execution.json()[0]["stop_requested"] is False
    assert binding.status_code == 200
    assert binding.json()["capability_snapshot"]["input"] == "unsupported"
    assert workspace.status_code == 200
    assert workspace.json()["lifecycle"] == "active"

    first_sessions = client.get(
        "/api/v1/gateway/sessions",
        params={**params, "execution_id": str(ids["execution_id"]), "limit": 1},
    )
    assert first_sessions.status_code == 200
    first_session_items = first_sessions.json()["items"]
    assert len(first_session_items) == 1
    assert first_session_items[0]["input_mode"] == "unsupported"
    next_cursor = first_sessions.json()["next_cursor"]
    assert next_cursor is not None
    second_sessions = client.get(
        "/api/v1/gateway/sessions",
        params={
            **params,
            "execution_id": str(ids["execution_id"]),
            "cursor": next_cursor,
            "limit": 1,
        },
    )
    assert second_sessions.status_code == 200
    second_session_items = second_sessions.json()["items"]
    assert len(second_session_items) == 1
    assert {
        item["id"] for item in first_session_items + second_session_items
    } == {str(ids["session_id"]), str(ids["closed_session_id"])}

    first_events = client.get(
        "/api/v1/gateway/events",
        params={
            **params,
            "session_id": str(ids["session_id"]),
            "limit": 2,
        },
    )
    assert [item["sequence"] for item in first_events.json()["items"]] == [1, 2]
    event_cursor = first_events.json()["next_cursor"]
    assert event_cursor is not None
    second_events = client.get(
        "/api/v1/gateway/events",
        params={
            **params,
            "session_id": str(ids["session_id"]),
            "cursor": event_cursor,
            "limit": 2,
        },
    )
    assert [item["sequence"] for item in second_events.json()["items"]] == [3]
    after_first = client.get(
        "/api/v1/gateway/events",
        params={
            **params,
            "session_id": str(ids["session_id"]),
            "after_sequence": 1,
        },
    )
    assert [item["sequence"] for item in after_first.json()["items"]] == [2, 3]

    artifacts = client.get(
        "/api/v1/gateway/artifacts",
        params={**params, "task_id": str(ids["task_id"])},
    )
    evidence = client.get(
        "/api/v1/gateway/evidence",
        params={**params, "execution_id": str(ids["execution_id"])},
    )
    inputs = client.get(
        "/api/v1/gateway/inputs",
        params={
            **params,
            "session_id": str(ids["session_id"]),
            "status": "queued",
        },
    )
    assert artifacts.json()["items"][0]["id"] == str(ids["artifact_id"])
    assert evidence.json()["items"][0]["status"] == "candidate"
    assert inputs.json()["items"][0]["attachment_count"] == 1

    outside_workspace = client.get(
        f"/api/v1/gateway/executions/{ids['execution_id']}/binding",
        params={"workspace_id": str(uuid4())},
    )
    isolated_sessions = client.get(
        "/api/v1/gateway/sessions",
        params={"workspace_id": str(uuid4())},
    )
    assert outside_workspace.status_code == 404
    assert isolated_sessions.status_code == 200
    assert isolated_sessions.json()["items"] == []
