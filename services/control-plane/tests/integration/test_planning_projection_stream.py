import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.api.projection_stream import _event_stream
from sidera_control_plane.application.projection_changes import (
    ProjectionChangePublisher,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import CommandModel
from sidera_control_plane.persistence.projection_change_repository import (
    ProjectionChangeRepository,
)
from sidera_control_plane.persistence.repositories import EventRepository

pytestmark = pytest.mark.integration


def test_planning_event_is_replayed_by_existing_projection_stream(
    database: Database,
    database_url: str,
    workspace_id: UUID,
) -> None:
    request_id = uuid4()
    command_id = uuid4()
    event = EventData(
        id=uuid4(),
        workspace_id=workspace_id,
        aggregate_type="planning_request",
        aggregate_id=request_id,
        aggregate_version=1,
        event_type="PlanningRequestSubmitted",
        payload={"planning_request_id": str(request_id)},
        actor_type="test",
        actor_id="projection-stream-test",
        causation_id=command_id,
        correlation_id=uuid4(),
    )
    with database.session() as session:
        session.add(
            CommandModel(
                id=command_id,
                workspace_id=workspace_id,
                command_type="SubmitPlanningRequest",
                actor_type="test",
                actor_id="projection-stream-test",
                target_type="workspace",
                target_id=None,
                payload={},
                expected_version=None,
                idempotency_key="projection-stream-planning",
                request_hash="a" * 64,
                correlation_id=event.correlation_id,
                status="applied",
                result={},
                rejection_reason=None,
            )
        )
        publisher = ProjectionChangePublisher(ProjectionChangeRepository(session))
        EventRepository(session, publisher).append_with_outbox(event)
        session.commit()

    async def read_first_event() -> str:
        settings = Settings(
            database_url=database_url,
            projection_stream_poll_interval_seconds=0.01,
            projection_stream_heartbeat_seconds=0.02,
        )
        stream = _event_stream(
            database,
            settings,
            workspace_id,
            start_cursor=0,
            schema_version=1,
        )
        try:
            async for event in stream:
                if event.startswith("id: "):
                    return event
            raise AssertionError("projection stream ended before a change event")
        finally:
            await stream.aclose()

    payload = json.loads(asyncio.run(read_first_event()).splitlines()[2][6:])

    assert payload["projection_type"] == "planning_request"
    assert payload["resource_id"] == str(request_id)
    assert payload["change_type"] == "planning_request_updated"
    assert payload["occurred_at"]
    assert datetime.fromisoformat(payload["occurred_at"]).tzinfo == UTC
