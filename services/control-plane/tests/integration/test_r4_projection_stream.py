import asyncio
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from sidera_control_plane.api.app import create_app
from sidera_control_plane.api.projection_stream import _event_stream
from sidera_control_plane.application.projection_changes import ProjectionChangePublisher
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    CommandModel,
    ProjectionChangeModel,
    WorkspaceModel,
)
from sidera_control_plane.persistence.projection_change_repository import (
    ProjectionChangeRepository,
)
from sidera_control_plane.persistence.repositories import EventRepository

from test_worker_workspace_api import (
    EXECUTION_TWO_ID,
    ROOT_TASK_ID,
    WORKER_ITEM_ID,
    _seed as seed_worker_workspace,
)

pytestmark = pytest.mark.integration


WORKSPACE_B_ID = UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(
        database_url=database_url,
        local_actor_type="local",
        local_actor_id="aitejiu",
        projection_stream_poll_interval_seconds=0.01,
        projection_stream_heartbeat_seconds=0.02,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def append_change(
    database: Database,
    workspace_id: UUID,
    *,
    resource_id: UUID | None = None,
    correlation_id: UUID | None = None,
) -> int:
    with database.session() as session:
        change = ProjectionChangeRepository(session).append(
            workspace_id=workspace_id,
            schema_version=1,
            projection_type="task_overview",
            resource_id=resource_id,
            change_type="upsert",
            resource_version=1,
            occurred_at=datetime(2026, 8, 21, tzinfo=UTC),
            correlation_id=correlation_id or uuid4(),
        )
        session.commit()
        return change.cursor


def test_concurrent_cursor_allocation_is_unique_and_workspace_scoped(
    database: Database,
    workspace_id: UUID,
) -> None:
    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=WORKSPACE_B_ID,
                name="Workspace B",
                status="active",
                version=1,
            )
        )
        session.commit()

    def allocate(_: int) -> int:
        return append_change(database, workspace_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        cursors = sorted(executor.map(allocate, range(2)))

    assert cursors == [1, 2]
    assert append_change(database, WORKSPACE_B_ID) == 1

    with database.session() as session:
        changes = ProjectionChangeRepository(session).list_after(
            workspace_id, cursor=1, limit=100
        )
    assert [change.cursor for change in changes] == [2]


def test_cursor_allocation_reconciles_legacy_projection_rows(
    database: Database,
    workspace_id: UUID,
) -> None:
    with database.session() as session:
        session.add(
            ProjectionChangeModel(
                workspace_id=workspace_id,
                cursor=7,
                schema_version=1,
                projection_type="task_overview",
                resource_id=None,
                change_type="legacy.seed",
                resource_version=1,
                occurred_at=datetime(2026, 8, 21, tzinfo=UTC),
                correlation_id=uuid4(),
            )
        )
        session.commit()

    assert append_change(database, workspace_id) == 8

    with database.session() as session:
        assert ProjectionChangeRepository(session).latest_cursor(workspace_id) == 8


def test_sse_replays_after_cursor_and_emits_only_invalidation_metadata(
    database: Database,
    database_url: str,
    workspace_id: UUID,
) -> None:
    append_change(database, workspace_id, resource_id=uuid4())
    append_change(database, workspace_id, resource_id=uuid4())

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
            start_cursor=1,
            schema_version=1,
        )
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    payload_text = asyncio.run(read_first_event())
    lines = payload_text.splitlines()

    assert lines[0] == "id: 2"
    assert lines[1] == "event: projection.changed"
    payload = json.loads(lines[2].removeprefix("data: "))
    assert payload["cursor"] == 2
    assert payload["projection_type"] == "task_overview"
    assert "projection" not in payload


def test_sse_emits_heartbeat_when_no_change_arrives(
    database: Database,
    database_url: str,
    workspace_id: UUID,
) -> None:
    async def read_heartbeat() -> str:
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
            return await anext(stream)
        finally:
            await stream.aclose()

    first_line = asyncio.run(read_heartbeat()).splitlines()[0]

    assert first_line.startswith(": heartbeat")


def test_sse_requests_resync_for_unsupported_schema_or_expired_cursor(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    first = append_change(database, workspace_id)
    append_change(database, workspace_id)
    with database.session() as session:
        session.execute(
            delete(ProjectionChangeModel).where(
                ProjectionChangeModel.workspace_id == workspace_id,
                ProjectionChangeModel.cursor == first,
            )
        )
        session.commit()

    for params in (
        {"after": 0, "schema_version": 1},
        {"after": 0, "schema_version": 99},
    ):
        with client.stream(
            "GET",
            f"/api/v1/workspaces/{workspace_id}/projection-changes",
            params=params,
        ) as response:
            lines_iter = response.iter_lines()
            lines = [next(lines_iter) for _ in range(2)]
        assert response.status_code == 200
        assert lines[0] == "event: resync_required"
        assert json.loads(lines[1].removeprefix("data: "))["reason"] in {
            "cursor_expired",
            "unsupported_schema",
        }


def test_local_actor_identity_is_checked_before_command_dispatch(
    client: TestClient,
    workspace_id: UUID,
) -> None:
    response = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "CreateTask",
            "target_type": "task",
            "actor": {"type": "local", "id": "another-user"},
            "target_id": None,
            "expected_version": None,
            "idempotency_key": "identity-check",
            "correlation_id": str(uuid4()),
            "payload": {
                "title": "Rejected",
                "description": "Rejected local identity",
                "acceptance_criteria": ["never created"],
                "priority": 50,
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_event_and_projection_change_commit_atomically(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    response = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "CreateTask",
            "target_type": "task",
            "actor": {"type": "user", "id": "user:test"},
            "target_id": None,
            "expected_version": None,
            "idempotency_key": "projection-atomic-task",
            "correlation_id": str(uuid4()),
            "payload": {
                "title": "Projection task",
                "description": "Creates one durable invalidation",
                "acceptance_criteria": ["event and change commit together"],
                "priority": 50,
            },
        },
    )

    assert response.status_code == 201, response.text
    task_id = UUID(response.json()["aggregate_id"])
    with database.session() as session:
        changes = ProjectionChangeRepository(session).list_after(
            workspace_id, cursor=0, limit=10
        )

    assert len(changes) == 2
    assert {change.projection_type for change in changes} == {
        "task_overview",
        "task_graph",
    }
    assert {change.resource_id for change in changes} == {task_id}


def test_execution_event_targets_stable_worker_item_resources(
    database: Database,
    workspace_id: UUID,
) -> None:
    seed_worker_workspace(database)
    event = EventData(
        id=UUID("b3000000-0000-0000-0000-000000000001"),
        workspace_id=workspace_id,
        aggregate_type="execution",
        aggregate_id=EXECUTION_TWO_ID,
        aggregate_version=4,
        event_type="ExecutionStarted",
        payload={"status": "running"},
        actor_type="test",
        actor_id="projection-test",
        causation_id=UUID("b3000000-0000-0000-0000-000000000002"),
        correlation_id=UUID("b3000000-0000-0000-0000-000000000003"),
    )

    with database.session() as session:
        session.add(
            CommandModel(
                id=event.causation_id,
                workspace_id=event.workspace_id,
                command_type="ProjectionTest",
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                target_type="execution",
                target_id=event.aggregate_id,
                payload={},
                expected_version=None,
                idempotency_key="projection-event-test",
                request_hash="a" * 64,
                correlation_id=event.correlation_id,
                status="applied",
                result={},
                rejection_reason=None,
            )
        )
        session.flush()
        EventRepository(
            session,
            ProjectionChangePublisher(ProjectionChangeRepository(session)),
        ).append_with_outbox(event)
        session.commit()
        changes = ProjectionChangeRepository(session).list_after(
            event.workspace_id, cursor=0, limit=100
        )

    event_changes = [
        change for change in changes if change.correlation_id == event.correlation_id
    ]
    assert {change.projection_type for change in event_changes} == {
        "task_graph",
        "subtask_workspace",
        "worker_item_workspace",
        "worker_item_activity",
    }
    assert {
        change.resource_id
        for change in event_changes
        if change.projection_type == "subtask_workspace"
    } == {ROOT_TASK_ID}
    assert {
        change.resource_id
        for change in event_changes
        if change.projection_type.startswith("worker_item_")
    } == {WORKER_ITEM_ID}
