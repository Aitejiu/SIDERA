from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from tests.integration.test_plan_revision_commands import _command, _seed_revision_base

from sidera_control_plane.adapters.fake_planner import (
    FakePlannerAdapter,
    FakePlannerConfig,
)
from sidera_control_plane.api.app import create_app
from sidera_control_plane.api.schemas import CommandResponse
from sidera_control_plane.application.planning_runner import PlanningRunner
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.planning import (
    PlanningRequestKind,
    PlanningRequestStatus,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
)
from sidera_control_plane.domain.revision_diff import NormalizedRevisionDiff
from sidera_control_plane.persistence.database import Database

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


class StaticFactory:
    def __init__(self, adapter: FakePlannerAdapter) -> None:
        self._adapter = adapter

    def create(
        self, definition: Any, instance: Any, profile_snapshot: Any
    ) -> FakePlannerAdapter:
        return self._adapter


def test_revision_runner_builds_typed_context_and_persists_v3_proposal(
    client: TestClient,
    database: Database,
    uow_factory,
    workspace_id: UUID,
) -> None:
    root_task, snapshot, profile = _seed_revision_base(database, workspace_id)
    response = client.post(
        "/api/v1/commands",
        json=_command(workspace_id, root_task, snapshot, profile),
    )
    assert response.status_code == 201, response.text
    command = CommandResponse.model_validate(response.json())
    planning_request_id = UUID(command.data["planning_request_id"])

    batch = PlanningRunner(
        uow_factory=uow_factory,
        adapter_factory=StaticFactory(
            FakePlannerAdapter(FakePlannerConfig(scenario="revision"))
        ),
        owner_id="planning-worker:revision-integration",
        lease_ttl_seconds=60,
        lease_renew_interval_seconds=10,
        clock=lambda: datetime.now(UTC),
    ).run_once(2)

    assert batch.claimed == 2
    assert batch.succeeded == 1
    assert batch.failed == 1
    with uow_factory() as uow:
        request = uow.planning_requests.get(planning_request_id, workspace_id)
        assert request is not None
        assert request.kind is PlanningRequestKind.REVISION
        assert request.status is PlanningRequestStatus.PROPOSAL_READY
        proposals = uow.planning_records.list_proposals(
            planning_request_id,
            workspace_id,
        )
        assert len(proposals) == 1
        proposal = proposals[0]
        assert isinstance(proposal.payload, PlanningRevisionProposalPayloadV3)
        assert proposal.payload.base_descriptor.plan_snapshot_id == snapshot.id
        assert proposal.payload.base_descriptor.plan_generation == snapshot.generation
        assert isinstance(proposal.derived_diff, NormalizedRevisionDiff)
        assert len(proposal.derived_diff.diff_hash) == 64
        assert any(
            item.key == "revision-fan-in" for item in proposal.payload.subtasks
        )
