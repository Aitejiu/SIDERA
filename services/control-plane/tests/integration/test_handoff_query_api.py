from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    HandoffProposalModel,
    HandoffTransitionModel,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url))) as test_client:
        yield test_client


def test_handoff_query_routes_are_workspace_scoped_and_typed(
    client: TestClient,
) -> None:
    workspace_id = uuid4()
    worker_item_id = uuid4()

    candidates = client.get(
        f"/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/"
        "handoff-candidates"
    )
    proposal = client.get(
        f"/api/v1/workspaces/{workspace_id}/handoff-proposals/{uuid4()}"
    )
    transition = client.get(
        f"/api/v1/workspaces/{workspace_id}/handoff-transitions/{uuid4()}"
    )
    active = client.get(
        f"/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/handoff"
    )

    assert candidates.status_code == 404
    assert proposal.status_code == 404
    assert transition.status_code == 404
    assert active.status_code == 404
    assert all(
        response.json()["error"]["code"] == "not_found"
        for response in (candidates, proposal, transition, active)
    )


def test_handoff_query_routes_return_bounded_real_views(
    database: Database,
    database_url: str,
    workspace_id: UUID,
) -> None:
    from test_handoff_target_execution import _seed

    fixture, _runtime = _seed(database, workspace_id)
    with database.session() as session:
        proposal_id = session.scalar(select(HandoffProposalModel.id))
        worker_item_id = session.scalar(select(HandoffTransitionModel.worker_item_id))
    assert proposal_id is not None
    assert worker_item_id is not None
    with TestClient(
        create_app(
            Settings(database_url=database_url)
        )
    ) as seeded_client:
        proposal = seeded_client.get(
            f"/api/v1/workspaces/{workspace_id}/handoff-proposals/"
            f"{proposal_id}"
        )
        transition = seeded_client.get(
            f"/api/v1/workspaces/{workspace_id}/handoff-transitions/"
            f"{fixture.transition_id}"
        )
        active = seeded_client.get(
            f"/api/v1/workspaces/{workspace_id}/worker-items/"
            f"{worker_item_id}/handoff"
        )

    assert proposal.status_code == 200, proposal.text
    assert transition.status_code == 200, transition.text
    assert active.status_code == 200, active.text
    transition_body = transition.json()
    assert transition_body["status"] == "running"
    assert transition_body["phase"] == "start_target_session"
    assert transition_body["source"]["execution"]["trace_descriptor"]["route"]
    assert transition_body["target"]["execution"]["trace_descriptor"] is None
    assert "target_runtime_snapshot" not in transition_body
