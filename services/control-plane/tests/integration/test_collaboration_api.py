import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.api.dependencies import get_collaboration_query_service
from sidera_control_plane.application.collaboration_queries import (
    CollaborationQueryService,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.collaboration_projections import (
    CollaborationDecisionProjectionFact,
    CollaborationIntentProjectionFact,
)
from sidera_control_plane.persistence.models import WorkerItemPlacementModel
from tests.integration.test_collaboration_evaluation_recovery import _prepare
from tests.unit.test_collaboration_projections import (
    DOWNSTREAM_REQUEST_ID,
    _facts,
)

pytestmark = pytest.mark.integration

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
DECISION_ONE_ID = UUID("80000000-0000-0000-0000-000000000001")
DECISION_TWO_ID = UUID("80000000-0000-0000-0000-000000000002")
PROJECTION_ID = UUID("90000000-0000-0000-0000-000000000001")
INTENT_ONE_ID = UUID("a0000000-0000-0000-0000-000000000001")
INTENT_TWO_ID = UUID("a0000000-0000-0000-0000-000000000002")


class _FactsStore:
    def __init__(self, facts) -> None:
        self.facts = facts

    def load(self, workspace_id: UUID, root_task_id: UUID):
        return self.facts


def _decision(decision_id: UUID, sequence: int, created_at):
    return CollaborationDecisionProjectionFact(
        id=decision_id,
        sequence=sequence,
        outcome="intents",
        reason_code="eligible",
        summary=f"放行第 {sequence} 个执行请求",
        projection_id=PROJECTION_ID,
        projection_hash="a" * 64,
        created_at=created_at,
    )


@contextmanager
def _client(database_url: str, facts) -> Iterator[TestClient]:
    app = create_app(Settings(database_url=database_url))
    app.dependency_overrides[get_collaboration_query_service] = lambda: CollaborationQueryService(
        _FactsStore(facts)
    )
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_decision_history_is_workspace_scoped_and_redacted(
    database_url: str,
) -> None:
    facts = _facts()
    now = facts.run.updated_at
    assert now is not None
    facts = replace(
        facts,
        decisions=(
            _decision(DECISION_ONE_ID, 1, now),
            _decision(DECISION_TWO_ID, 2, now + timedelta(seconds=1)),
        ),
        intents=(
            CollaborationIntentProjectionFact(
                id=INTENT_ONE_ID,
                decision_id=DECISION_ONE_ID,
                target_request_id=DOWNSTREAM_REQUEST_ID,
                ordinal=0,
                summary="放行回调验证",
            ),
            CollaborationIntentProjectionFact(
                id=INTENT_TWO_ID,
                decision_id=DECISION_TWO_ID,
                target_request_id=DOWNSTREAM_REQUEST_ID,
                ordinal=0,
                summary="再次放行回调验证",
            ),
        ),
    )

    with _client(str(database_url), facts) as client:
        response = client.get(
            f"/api/v1/workspaces/{WORKSPACE_ID}/tasks/{ROOT_TASK_ID}/collaboration/decisions",
            params={"limit": 1},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["items"][0]["sequence"] == 2
        assert body["items"][0]["target_paths"][0]["title"] == "验证支付回调"
        assert body["next_cursor"]
        assert "payload_json" not in json.dumps(body)
        assert "provider-key" not in json.dumps(body)

        next_response = client.get(
            f"/api/v1/workspaces/{WORKSPACE_ID}/tasks/{ROOT_TASK_ID}/collaboration/decisions",
            params={"limit": 1, "cursor": body["next_cursor"]},
        )
        assert next_response.status_code == 200
        assert next_response.json()["items"][0]["sequence"] == 1


def test_collaboration_detail_rejects_cross_workspace_facts(
    database_url: str,
) -> None:
    with _client(str(database_url), _facts()) as client:
        response = client.get(
            "/api/v1/workspaces/20000000-0000-0000-0000-000000000099/"
            f"tasks/{ROOT_TASK_ID}/collaboration"
        )

        assert response.status_code == 404


def test_collaboration_detail_reads_the_current_run_from_sqlalchemy(
    database,
    database_url: str,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, snapshot, _reconciler, _prepared = _prepare(
        database, workspace_id, uow_factory
    )
    with database.session() as session:
        session.add(
            WorkerItemPlacementModel(
                plan_snapshot_id=snapshot.id,
                plan_generation=snapshot.generation,
                workspace_id=workspace_id,
                root_task_id=snapshot.root_task_id,
                worker_item_id=_ids["worker_item"],
                worker_item_key="backend",
                subtask_key="subtask",
                subtask_task_id=_ids["subtask"],
                role="worker",
                capability_requirements=["python"],
                worker_policy_ref=None,
                configuration_ref=None,
            )
        )
        session.commit()

    with TestClient(create_app(Settings(database_url=database_url))) as client:
        response = client.get(
            f"/api/v1/workspaces/{workspace_id}/tasks/{snapshot.root_task_id}/collaboration"
        )
        root_graph = client.get(
            f"/api/v1/tasks/{snapshot.root_task_id}/graph",
            params={"workspace_id": str(workspace_id)},
        )
        worker_workspace = client.get(
            f"/api/v1/workspaces/{workspace_id}/worker-items/{_ids['worker_item']}/workspace"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "reference"
    assert body["plan_generation"] == snapshot.generation
    assert body["enabled"] is True
    assert "runtime_capability_snapshot_json" not in json.dumps(body)
    assert root_graph.status_code == 200
    assert root_graph.json()["overview"]["collaboration"]["run_id"] == body["run_id"]
    assert worker_workspace.status_code == 200, worker_workspace.text
    assert worker_workspace.json()["collaboration"]["state"] == "waiting_release"
