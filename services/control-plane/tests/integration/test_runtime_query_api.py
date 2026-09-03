import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.scheduling import (
    SchedulingBlockReason,
    scheduling_fingerprint,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    SchedulingBlockModel,
    WorkerRuntimeConfigurationModel,
    WorkspaceModel,
)
from tests.integration.test_gateway_runner import NOW, _seed_assignment

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url))) as test_client:
        yield test_client


def _assert_no_sensitive_keys(value: Any) -> None:
    if isinstance(value, dict):
        assert "config" not in value
        assert "secret_refs" not in value
        for item in value.values():
            _assert_no_sensitive_keys(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_sensitive_keys(item)


def _add_runtime_configuration(
    database: Database,
    workspace_id: UUID,
    seed: dict[str, UUID],
    *,
    adapter_config_version: int = 1,
) -> None:
    with database.session() as session:
        session.add(
            WorkerRuntimeConfigurationModel(
                worker_id=seed["worker_id"],
                workspace_id=workspace_id,
                adapter_module_instance_id=seed["adapter_instance_id"],
                adapter_module_config_version=adapter_config_version,
                workspace_module_instance_id=seed["workspace_instance_id"],
                workspace_module_config_version=1,
                version=1,
            )
        )
        session.commit()


def test_worker_runtime_query_exposes_missing_ready_invalid_and_safe_summaries(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    seed = _seed_assignment(database, workspace_id)

    missing = client.get(
        f"/api/v1/workers/{seed['worker_id']}",
        params={"workspace_id": str(workspace_id)},
    )
    assert missing.status_code == 200, missing.text
    assert missing.json()["runtime_configuration_status"] == "missing"
    assert missing.json()["runtime_configuration_reason"] == (
        "runtime_configuration_missing"
    )
    assert missing.json()["adapter_module_summary"] is None
    assert missing.json()["workspace_module_summary"] is None

    _add_runtime_configuration(database, workspace_id, seed)
    ready = client.get(
        f"/api/v1/workers/{seed['worker_id']}",
        params={"workspace_id": str(workspace_id)},
    )
    assert ready.status_code == 200, ready.text
    ready_payload = ready.json()
    assert ready_payload["runtime_configuration_status"] == "ready"
    assert ready_payload["runtime_configuration_reason"] is None
    assert ready_payload["runtime_configuration_version"] == 1
    assert ready_payload["runtime_configured_at"]
    assert set(ready_payload["adapter_module_summary"]) == {
        "id",
        "instance_name",
        "provider",
        "module_type",
        "config_version",
        "definition_lifecycle",
        "instance_lifecycle",
    }
    assert ready_payload["adapter_module_summary"]["provider"] == "fake"
    assert ready_payload["workspace_module_summary"]["module_type"] == (
        "workspace_provider"
    )
    _assert_no_sensitive_keys(ready_payload)

    with database.session() as session:
        session.execute(
            update(WorkerRuntimeConfigurationModel)
            .where(
                WorkerRuntimeConfigurationModel.worker_id == seed["worker_id"],
                WorkerRuntimeConfigurationModel.workspace_id == workspace_id,
            )
            .values(adapter_module_config_version=2)
        )
        session.commit()
    invalid = client.get(
        f"/api/v1/workers/{seed['worker_id']}",
        params={"workspace_id": str(workspace_id)},
    )
    assert invalid.status_code == 200, invalid.text
    assert invalid.json()["runtime_configuration_status"] == "invalid"
    assert invalid.json()["runtime_configuration_reason"] == (
        "runtime_configuration_invalid"
    )

    other_workspace = uuid4()
    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=other_workspace,
                name="Other runtime query workspace",
                status="active",
                version=1,
            )
        )
        session.commit()
    cross_workspace = client.get(
        f"/api/v1/workers/{seed['worker_id']}",
        params={"workspace_id": str(other_workspace)},
    )
    assert cross_workspace.status_code == 404, cross_workspace.text


def test_module_query_filters_workspace_orders_stably_and_exposes_lifecycle(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    seed = _seed_assignment(database, workspace_id)
    with database.session() as session:
        adapter_definition_id = session.scalar(
            select(ModuleInstanceModel.definition_id).where(
                ModuleInstanceModel.id == seed["adapter_instance_id"],
                ModuleInstanceModel.workspace_id == workspace_id,
            )
        )
    assert adapter_definition_id is not None
    with database.session() as session:
        session.execute(
            update(ModuleDefinitionModel)
            .where(
                ModuleDefinitionModel.id == adapter_definition_id,
                ModuleDefinitionModel.workspace_id == workspace_id,
            )
            .values(lifecycle="disabled")
        )
        session.execute(
            update(ModuleInstanceModel)
            .where(
                ModuleInstanceModel.id == seed["workspace_instance_id"],
                ModuleInstanceModel.workspace_id == workspace_id,
            )
            .values(lifecycle="draining")
        )
        session.commit()

    first = client.get(
        "/api/v1/modules",
        params={"workspace_id": str(workspace_id)},
    )
    second = client.get(
        "/api/v1/modules",
        params={"workspace_id": str(workspace_id)},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_payload = first.json()
    assert [item["id"] for item in first_payload] == [
        item["id"] for item in second.json()
    ]
    assert {item["definition_lifecycle"] for item in first_payload} == {
        "disabled",
        "active",
    }
    assert {item["instance_lifecycle"] for item in first_payload} == {
        "active",
        "draining",
    }
    assert first_payload[0]["module_type"] == "worker_adapter"
    assert set(first_payload[0]) == {
        "id",
        "instance_name",
        "provider",
        "module_type",
        "config_version",
        "definition_lifecycle",
        "instance_lifecycle",
    }
    _assert_no_sensitive_keys(first_payload)

    filtered = client.get(
        "/api/v1/modules",
        params={
            "workspace_id": str(workspace_id),
            "module_type": "worker_adapter",
        },
    )
    assert filtered.status_code == 200, filtered.text
    assert len(filtered.json()) == 1
    assert filtered.json()[0]["id"] == str(seed["adapter_instance_id"])

    for module_type in (
        "planner_adapter",
        "collaboration_runtime",
        "notification_channel",
    ):
        supported_filter = client.get(
            "/api/v1/modules",
            params={
                "workspace_id": str(workspace_id),
                "module_type": module_type,
            },
        )
        assert supported_filter.status_code == 200, supported_filter.text
        assert supported_filter.json() == []

    other_workspace = uuid4()
    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=other_workspace,
                name="Other module query workspace",
                status="active",
                version=1,
            )
        )
        session.commit()
    cross_workspace = client.get(
        "/api/v1/modules",
        params={"workspace_id": str(other_workspace)},
    )
    assert cross_workspace.status_code == 200
    assert cross_workspace.json() == []


def test_execution_query_projects_waiting_and_assignment_facts(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    seed = _seed_assignment(database, workspace_id)
    waiting_execution_id = uuid4()
    reason_details = {
        "candidate_count": 3,
        "candidate_issue_counts": {
            "runtime_configuration_missing": 2,
            "adapter_module_unavailable": 1,
        },
    }
    with database.session() as session:
        session.add(
            ExecutionModel(
                id=waiting_execution_id,
                workspace_id=workspace_id,
                task_id=seed["task_id"],
                lineage_id=waiting_execution_id,
                attempt=1,
                max_attempts=2,
                purpose="Wait for runtime",
                status="queued",
                capability_requirements=["python"],
                adapter_preferences=["fake"],
                adapter_forbidden=[],
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
            )
        )
        session.add(
            SchedulingBlockModel(
                id=uuid4(),
                workspace_id=workspace_id,
                execution_id=waiting_execution_id,
                status="active",
                reason_code=SchedulingBlockReason.NO_ELIGIBLE_WORKER.value,
                reason_details=reason_details,
                fingerprint=scheduling_fingerprint(
                    SchedulingBlockReason.NO_ELIGIBLE_WORKER,
                    reason_details,
                    requires_operator=True,
                ),
                requires_operator=True,
                first_seen_at=NOW,
                last_seen_at=NOW + timedelta(minutes=2),
                occurrence_count=2,
                resolved_at=None,
                version=2,
            )
        )
        session.commit()

    executions = client.get(
        "/api/v1/executions",
        params={"workspace_id": str(workspace_id), "task_id": str(seed["task_id"])},
    )
    assert executions.status_code == 200, executions.text
    by_id = {item["id"]: item for item in executions.json()}

    assigned = by_id[str(seed["execution_id"])]
    assert assigned["adapter_preferences"] == ["opencode", "codex"]
    assert assigned["adapter_forbidden"] == ["unsafe"]
    assert assigned["scheduling_status"] == "assigned"
    assert assigned["assigned_worker_summary"]["id"] == str(seed["worker_id"])
    assert assigned["binding_summary"]["adapter_module_id"] == str(
        seed["adapter_instance_id"]
    )
    assert assigned["waiting_reason"] is None
    assert assigned["waiting_details_summary"] is None

    waiting = by_id[str(waiting_execution_id)]
    assert waiting["scheduling_status"] == "waiting"
    assert waiting["waiting_reason"] == "no_eligible_worker"
    assert "3 个候选执行节点" in waiting["waiting_details_summary"]
    assert "运行环境未配置 2 个" in waiting["waiting_details_summary"]
    assert waiting["requires_operator"] is True
    assert waiting["blocked_since"].startswith(NOW.isoformat().replace("+00:00", ""))
    assert waiting["pinned_worker_summary"] is None
    assert waiting["assigned_worker_summary"] is None
    _assert_no_sensitive_keys(json.loads(json.dumps(waiting)))

    with database.session() as session:
        session.execute(
            update(ExecutionModel)
            .where(
                ExecutionModel.id == seed["execution_id"],
                ExecutionModel.workspace_id == workspace_id,
            )
            .values(status="succeeded", finished_at=NOW)
        )
        session.commit()
    terminal = client.get(
        f"/api/v1/executions/{seed['execution_id']}",
        params={"workspace_id": str(workspace_id)},
    )
    assert terminal.status_code == 200, terminal.text
    assert terminal.json()["scheduling_status"] == "terminal"
