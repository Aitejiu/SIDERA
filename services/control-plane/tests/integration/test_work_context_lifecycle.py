from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    EventModel,
    OutboxMessageModel,
    PlanningBriefRevisionModel,
    PlanningRequestModel,
    PlanningRunModel,
    ProjectionChangeModel,
    WorkContextModel,
    WorkContextRevisionModel,
)
from sidera_control_plane.persistence.work_context_repository import (
    WorkContextRepository,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _command(
    workspace_id: UUID,
    command_type: str,
    payload: dict[str, object],
    *,
    idempotency_key: str,
    target_type: str = "workspace",
    target_id: UUID | None = None,
    expected_version: int | None = None,
) -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": target_type,
        "actor": {"type": "local", "id": "operator"},
        "target_id": str(target_id) if target_id is not None else None,
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def _profile_id(client: TestClient, workspace_id: UUID, suffix: str) -> str:
    module = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "RegisterPlannerModule",
            {
                "provider": "fake",
                "interface_version": "sidera.planner.v1",
                "config_schema_version": 1,
                "capabilities": {
                    "structured_output": "json_schema",
                    "clarification": True,
                    "idempotency": "operation-id",
                    "cancellation": "run",
                    "usage_reporting": "tokens",
                    "max_context_bytes": 128000,
                    "stability": "stable",
                    "interface_version": "sidera.planner.v1",
                    "proposal_schema_versions": [2],
                },
                "instance_name": f"context-{suffix}",
                "config_namespace": "planning",
                "config": {"planning.mode": "test"},
                "secret_refs": [],
            },
            idempotency_key=f"context-module-{suffix}",
        ),
    )
    assert module.status_code == 201, module.text
    module_id = module.json()["data"]["module_instance_id"]
    profile = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "RegisterPlanningProfile",
            {
                "profile_key": f"context-{suffix}",
                "version": 1,
                "display_name": "Context test planner",
                "planner_module_instance_id": module_id,
                "planner_module_config_version": 1,
                "planner_interface_version": "sidera.planner.v1",
                "prompt_policy_version": "prompt.v1",
                "system_policy": {"approval": {"required": True}},
                "proposal_schema_version": 2,
                "allowed_capabilities": ["code"],
                "adapter_preferences": ["fake"],
                "adapter_forbidden": [],
                "budget": {
                    "max_context_bytes": 64000,
                    "max_tokens": 10000,
                    "max_cost_microunits": 1000000,
                    "max_runtime_seconds": 600,
                    "max_revisions": 3,
                    "max_nodes": 20,
                    "max_concurrency": 2,
                },
                "retry_policy": {
                    "max_retries": 1,
                    "backoff_seconds": [5],
                    "unknown_outcome_policy": "block",
                    "invalid_output_policy": "retry",
                },
                "default_priority": 50,
                "execution_max_attempts": 2,
                "execution_budget_level": "standard",
                "user_selectable": True,
                "planner_secret_refs": [],
            },
            idempotency_key=f"context-profile-{suffix}",
        ),
    )
    assert profile.status_code == 201, profile.text
    return str(profile.json()["data"]["planning_profile_id"])


def _submit(client: TestClient, workspace_id: UUID, profile_id: str, suffix: str):
    response = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "SubmitPlanningRequest",
            {
                "profile_id": profile_id,
                "profile_version": 1,
                "objective": "Persist an immutable work context",
                "constraints": ["No raw chat"],
                "acceptance_criteria": ["Context revision is queryable"],
                "context_refs": ["conversation:should-not-be-copied"],
                "requested_outputs": ["context manifest"],
                "source_type": "web",
                "source_ref": "conversation:private",
            },
            idempotency_key=f"context-submit-{suffix}",
        ),
    )
    assert response.status_code == 201, response.text
    return response


def _counts(database: Database) -> dict[str, int]:
    models = {
        "requests": PlanningRequestModel,
        "briefs": PlanningBriefRevisionModel,
        "runs": PlanningRunModel,
        "contexts": WorkContextModel,
        "revisions": WorkContextRevisionModel,
        "events": EventModel,
        "outbox": OutboxMessageModel,
    }
    with database.session() as session:
        return {
            name: session.scalar(select(func.count()).select_from(model)) or 0
            for name, model in models.items()
        }


def test_submit_creates_work_context_event_outbox_and_projection_atomically(
    client: TestClient, database: Database, workspace_id: UUID
) -> None:
    profile_id = _profile_id(client, workspace_id, "created")
    response = _submit(client, workspace_id, profile_id, "created")
    data = response.json()["data"]
    request_id = UUID(data["planning_request_id"])
    context_id = UUID(data["work_context_id"])

    with database.session() as session:
        context = session.scalar(
            select(WorkContextModel).where(WorkContextModel.id == context_id)
        )
        revision = session.scalar(
            select(WorkContextRevisionModel).where(
                WorkContextRevisionModel.work_context_id == context_id
            )
        )
        context_events = session.scalars(
            select(EventModel).where(
                EventModel.aggregate_type == "work_context",
                EventModel.aggregate_id == context_id,
            )
        ).all()
        context_outbox = session.scalars(select(OutboxMessageModel)).all()
        context_changes = session.scalars(
            select(ProjectionChangeModel).where(
                ProjectionChangeModel.projection_type == "work_context",
                ProjectionChangeModel.resource_id == context_id,
            )
        ).all()

    assert context is not None
    assert context.planning_request_id == request_id
    assert context.status == "active"
    assert context.current_revision_number == 1
    assert revision is not None
    assert revision.reason == "request_created"
    assert revision.brief_planning_request_id == request_id
    assert revision.brief_revision == 1
    assert revision.created_by == "operator"
    assert "source_ref" not in revision.snapshot
    assert "conversation:should-not-be-copied" not in str(revision.snapshot)
    assert [event.event_type for event in context_events] == ["WorkContextCreated"]
    assert context_events[0].payload["content_hash"] == revision.content_hash
    assert all("snapshot" not in event.payload for event in context_events)
    assert {
        row.topic for row in context_outbox if row.payload["aggregate_type"] == "work_context"
    } == {"work_context.created"}
    assert len(context_changes) == 1
    assert context_changes[0].change_type == "work_context_created"
    assert context_changes[0].resource_id == context_id


def test_work_context_insert_failure_rolls_back_planning_records_and_events(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_id = _profile_id(client, workspace_id, "rollback")
    before = _counts(database)

    def fail_add(
        self: WorkContextRepository, context: Any, first_revision: Any
    ) -> None:
        raise RuntimeError("simulated work context insert failure")

    monkeypatch.setattr(WorkContextRepository, "add", fail_add)

    with pytest.raises(RuntimeError, match="simulated work context insert failure"):
        _submit(client, workspace_id, profile_id, "rollback")

    assert _counts(database) == before
