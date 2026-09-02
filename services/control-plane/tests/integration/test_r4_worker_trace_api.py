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
    ExecutionEventModel,
    ExecutionSessionModel,
)
from tests.integration.test_r4_task_first_api import (
    BACKEND_EXECUTION_ID,
    ROOT_TASK_ID,
    SESSION_ID,
    WORKSPACE_ID,
    _seed,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url))) as test_client:
        yield test_client


def _add_trace_events(database: Database) -> None:
    with database.session() as session:
        session.add_all(
            [
                ExecutionEventModel(
                    id=uuid4(),
                    workspace_id=WORKSPACE_ID,
                    execution_id=BACKEND_EXECUTION_ID,
                    session_id=SESSION_ID,
                    sequence=3,
                    event_type="progress.updated",
                    payload={
                        "sessionUpdate": "tool_call",
                        "tool": "pytest",
                        "duration_ms": 12,
                        "authorization": "must not be shown",
                    },
                    occurred_at=NOW + timedelta(seconds=4),
                    raw_ref="provider-session-raw-ref",
                ),
                ExecutionEventModel(
                    id=uuid4(),
                    workspace_id=WORKSPACE_ID,
                    execution_id=BACKEND_EXECUTION_ID,
                    session_id=SESSION_ID,
                    sequence=4,
                    event_type="assistant.summary",
                    payload={"summary": "Tests are green", "duration_ms": 20},
                    occurred_at=NOW + timedelta(seconds=5),
                    raw_ref=None,
                ),
            ]
        )
        session.execute(
            update(ExecutionSessionModel)
            .where(ExecutionSessionModel.id == SESSION_ID)
            .values(last_sequence=4, last_activity_at=NOW + timedelta(seconds=5))
        )
        session.commit()


def test_trace_checks_lineage_and_supports_reverse_pagination(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    assert workspace_id == WORKSPACE_ID
    _seed(database)
    _add_trace_events(database)
    params = {
        "workspace_id": str(WORKSPACE_ID),
        "limit": 2,
    }

    first = client.get(
        f"/api/v1/tasks/{ROOT_TASK_ID}/executions/{BACKEND_EXECUTION_ID}/sessions/{SESSION_ID}/trace",
        params=params,
    )
    assert first.status_code == 200, first.text
    assert [event["sequence"] for event in first.json()["events"]] == [3, 4]
    before = first.json()["previous_before_sequence"]
    assert before == 3
    assert first.json()["timing"]["tools_ms"] == 12
    assert first.json()["timing"]["total_ms"] == 12
    assert "provider-session-raw-ref" not in first.text
    assert "authorization" not in first.text

    older = client.get(
        f"/api/v1/tasks/{ROOT_TASK_ID}/executions/{BACKEND_EXECUTION_ID}/sessions/{SESSION_ID}/trace",
        params={**params, "before_sequence": before},
    )
    assert older.status_code == 200, older.text
    assert [event["sequence"] for event in older.json()["events"]] == [1, 2]
    assert not {
        event["sequence"] for event in first.json()["events"]
    } & {event["sequence"] for event in older.json()["events"]}
    assert older.json()["previous_before_sequence"] is None
    assert older.json()["context_branch"]["replay_capability"] == "portable_replay"

    wrong_session = client.get(
        f"/api/v1/tasks/{ROOT_TASK_ID}/executions/{BACKEND_EXECUTION_ID}/sessions/{uuid4()}/trace",
        params=params,
    )
    assert wrong_session.status_code == 404
    assert wrong_session.json()["error"]["code"] == "not_found"
