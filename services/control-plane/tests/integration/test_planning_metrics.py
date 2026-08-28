from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_planning_command_api import _seed_waiting_request
from test_revision_confirmation import _confirm_command, _seed_revision_confirmation

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as test_client:
        yield test_client


def test_planning_metrics_are_low_cardinality_and_workspace_scoped(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    _seed_waiting_request(database, workspace_id)

    response = client.get(f"/api/v1/workspaces/{workspace_id}/planning-metrics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runs"]["queued"] == 0
    assert body["runs"]["running"] == 0
    assert body["runs"]["blocked"] == 0
    assert body["runs"]["needs_clarification"] == 1
    assert body["usage"]["input_tokens"] == 0
    assert body["usage"]["output_tokens"] == 0
    assert body["planning"]["clarifications"] == 1
    revision = body["revision"]
    assert revision["plan_revisions_active"] == 0
    assert revision["plan_revision_conflicts_total"] == []
    assert revision["plan_transitions_active"] == 0
    assert revision["worker_transitions_total"] == []
    assert revision["context_patch_delivery_total"] == []
    assert revision["revision_confirmation_blocked_total"] == []
    assert "objective" not in body
    assert "prompt" not in body
    assert "provider_raw_response" not in body


def test_revision_metrics_reflect_active_transition_without_uuid_labels(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    seed = _seed_revision_confirmation(database, workspace_id)

    before = client.get(f"/api/v1/workspaces/{workspace_id}/planning-metrics")
    assert before.status_code == 200, before.text
    assert before.json()["revision"]["plan_revisions_active"] == 1

    confirmed = client.post(
        "/api/v1/commands",
        json=_confirm_command(seed, idempotency_key="metrics-confirm-revision"),
    )
    assert confirmed.status_code == 201, confirmed.text

    after = client.get(f"/api/v1/workspaces/{workspace_id}/planning-metrics")
    assert after.status_code == 200, after.text
    revision = after.json()["revision"]
    assert revision["plan_revisions_active"] == 0
    assert revision["plan_transitions_active"] == 1
    assert revision["worker_transitions_total"]
    assert any(
        item["outcome"] == "in_flight"
        for item in revision["worker_transitions_total"]
    )
    assert any(
        item["reason"] in {"active_fence", "in_flight"}
        for item in revision["revision_confirmation_blocked_total"]
    )
    assert all(
        set(item) == {"action", "outcome", "count"}
        for item in revision["worker_transitions_total"]
    )
    assert all(
        "00000000-" not in str(item)
        for item in (
            revision["plan_revision_conflicts_total"],
            revision["worker_transitions_total"],
            revision["context_patch_delivery_total"],
            revision["revision_confirmation_blocked_total"],
        )
    )
