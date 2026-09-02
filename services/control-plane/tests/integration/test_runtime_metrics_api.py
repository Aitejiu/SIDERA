from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from tests.integration.test_scheduler_runtime_binding import _scheduler, _seed

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    CommandModel,
    EventModel,
    ExecutionModel,
)

pytestmark = pytest.mark.integration


def test_runtime_metrics_are_workspace_scoped_and_low_cardinality(
    database: Database,
    database_url: str,
    workspace_id: UUID,
) -> None:
    fixture = _seed(database, workspace_id, configured_worker_indexes=(1,))
    assert _scheduler(database).run_once(limit=1).assigned == 1

    second_execution_id = uuid4()
    second_execution = Execution.create(
        execution_id=second_execution_id,
        workspace_id=workspace_id,
        task_id=fixture.task.id,
        lineage_id=second_execution_id,
        attempt=1,
        max_attempts=1,
        purpose="record a scheduling wait",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=None,
        not_before=fixture.execution.not_before,
    )
    with database.session() as session:
        session.add(
            # The active worker is at capacity; the other candidate has no runtime.
            ExecutionModel(
                id=second_execution.id,
                workspace_id=workspace_id,
                task_id=second_execution.task_id,
                lineage_id=second_execution.lineage_id,
                attempt=second_execution.attempt,
                max_attempts=second_execution.max_attempts,
                purpose=second_execution.purpose,
                status=second_execution.status.value,
                capability_requirements=sorted(second_execution.capability_requirements),
                adapter_preferences=list(second_execution.adapter_preferences),
                adapter_forbidden=sorted(second_execution.adapter_forbidden),
                pinned_worker_id=None,
                assigned_worker_id=None,
                worker_item_id=None,
                active_lease_id=None,
                last_fencing_token=0,
                not_before=second_execution.not_before,
                failure_code=None,
                started_at=None,
                finished_at=None,
                version=1,
                stop_requested=False,
                source_execution_id=None,
                source_session_id=None,
            )
        )
        command_id = uuid4()
        session.add(
            CommandModel(
                id=command_id,
                workspace_id=workspace_id,
                command_type="GatewayStart",
                actor_type="system",
                actor_id="gateway",
                target_type="execution",
                target_id=fixture.execution.id,
                payload={},
                expected_version=None,
                idempotency_key=f"runtime-metrics:{command_id}",
                request_hash="0" * 64,
                correlation_id=command_id,
                status="applied",
                result={},
                rejection_reason=None,
            )
        )
        session.flush()
        session.add(
            EventModel(
                id=uuid4(),
                workspace_id=workspace_id,
                aggregate_type="gateway_operation",
                aggregate_id=uuid4(),
                aggregate_version=1,
                event_type="ExecutionRuntimeStartBlocked",
                payload={
                    "execution_id": str(fixture.execution.id),
                    "error_code": "runtime_module_disabled",
                },
                actor_type="system",
                actor_id="gateway",
                causation_id=command_id,
                correlation_id=command_id,
            )
        )
        session.commit()

    assert _scheduler(database).run_once(limit=1).deferred == 1
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        response = client.get(f"/api/v1/workspaces/{workspace_id}/runtime-metrics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scheduler_assignments_total"] == 1
    assert body["scheduler_blocks_active"] == {"no_eligible_worker": 1}
    assert body["worker_runtime_configuration_status"] == {"missing": 1, "ready": 1}
    assert body["runtime_binding_failures_total"] == {
        "runtime_module_disabled": 1
    }
    duration = body["scheduler_assignment_duration_seconds"]
    assert duration["count"] == 1
    assert duration["total_seconds"] >= 0
    assert duration["average_seconds"] >= 0
    assert "config" not in body
    assert "secret" not in body
    assert "prompt" not in body
