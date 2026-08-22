from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_planning_command_api import _seed_waiting_request

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
    assert "objective" not in body
    assert "prompt" not in body
    assert "provider_raw_response" not in body
