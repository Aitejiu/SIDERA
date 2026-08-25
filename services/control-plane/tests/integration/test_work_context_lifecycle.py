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


def test_revise_work_context_appends_typed_patch_and_replays_without_new_revision(
    client: TestClient, database: Database, workspace_id: UUID
) -> None:
    profile_id = _profile_id(client, workspace_id, "revise")
    submitted = _submit(client, workspace_id, profile_id, "revise")
    data = submitted.json()["data"]
    context_id = UUID(data["work_context_id"])
    revision_hash = data["work_context_content_hash"]
    request = _command(
        workspace_id,
        "ReviseWorkContext",
        {
            "base_revision": 1,
            "base_content_hash": revision_hash,
            "reason": "user_intervention",
            "patch": {
                "objective": "Refine the implementation objective",
                "constraints": ["No raw chat", "Keep the audit trail"],
                "acceptance_criteria": ["The revised context is queryable"],
                "requested_outputs": ["context manifest", "revision summary"],
                "accepted_decisions": [
                    {
                        "key": "decision:operator-scope",
                        "statement": "Keep the scope inside the control plane",
                        "source": "operator",
                        "ordinal": 0,
                    }
                ],
                "unresolved_questions": [
                    {
                        "key": "question:package-budget",
                        "question": "What is the package budget?",
                        "blocking": False,
                        "ordinal": 0,
                    }
                ],
                "artifact_refs": [
                    {
                        "ref_id": str(uuid4()),
                        "ref_type": "design_note",
                        "ordinal": 0,
                    }
                ],
            },
        },
        idempotency_key="context-revise-once",
        target_type="work_context",
        target_id=context_id,
        expected_version=1,
    )

    first = client.post("/api/v1/commands", json=request)
    assert first.status_code == 200, first.text
    replay = client.post("/api/v1/commands", json=request)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()

    with database.session() as session:
        context = session.get(WorkContextModel, context_id)
        revisions = session.scalars(
            select(WorkContextRevisionModel)
            .where(WorkContextRevisionModel.work_context_id == context_id)
            .order_by(WorkContextRevisionModel.revision_number)
        ).all()
        events = session.scalars(
            select(EventModel)
            .where(
                EventModel.aggregate_type == "work_context",
                EventModel.aggregate_id == context_id,
            )
            .order_by(EventModel.aggregate_version)
        ).all()

    assert context is not None
    assert context.current_revision_number == 2
    assert context.version == 2
    assert len(revisions) == 2
    assert revisions[0].snapshot["objective"] == "Persist an immutable work context"
    assert revisions[1].snapshot["objective"] == "Refine the implementation objective"
    assert revisions[1].reason == "user_intervention"
    assert revisions[1].snapshot["accepted_decisions"][-1]["key"] == (
        "decision:operator-scope"
    )
    assert revisions[1].snapshot["unresolved_questions"][0]["key"] == (
        "question:package-budget"
    )
    assert [event.event_type for event in events] == [
        "WorkContextCreated",
        "WorkContextRevised",
    ]


def test_revise_work_context_rejects_stale_base_without_mutating_context(
    client: TestClient, database: Database, workspace_id: UUID
) -> None:
    profile_id = _profile_id(client, workspace_id, "base-mismatch")
    submitted = _submit(client, workspace_id, profile_id, "base-mismatch")
    data = submitted.json()["data"]
    context_id = UUID(data["work_context_id"])

    response = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "ReviseWorkContext",
            {
                "base_revision": 1,
                "base_content_hash": "f" * 64,
                "reason": "replanned",
                "patch": {"objective": "Should not apply"},
            },
            idempotency_key="context-base-mismatch",
            target_type="work_context",
            target_id=context_id,
            expected_version=1,
        ),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "work_context_base_mismatch"
    with database.session() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(WorkContextRevisionModel).where(
                    WorkContextRevisionModel.work_context_id == context_id
                )
            )
            == 1
        )


def test_attach_knowledge_revision_appends_exact_ref_without_cross_database_lookup(
    client: TestClient, database: Database, workspace_id: UUID
) -> None:
    profile_id = _profile_id(client, workspace_id, "knowledge")
    submitted = _submit(client, workspace_id, profile_id, "knowledge")
    data = submitted.json()["data"]
    context_id = UUID(data["work_context_id"])
    page_id = uuid4()
    revision_id = uuid4()

    response = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "AttachKnowledgeRevision",
            {
                "selection_reason": "operator selected the exact section",
                "knowledge_ref": {
                    "page_id": str(page_id),
                    "revision_id": str(revision_id),
                    "revision_hash": "a" * 64,
                    "section_key": "api-contract",
                    "section_hash": "b" * 64,
                    "source_ids": [str(uuid4())],
                    "ordinal": 0,
                },
            },
            idempotency_key="context-knowledge-attach",
            target_type="work_context",
            target_id=context_id,
            expected_version=1,
        ),
    )

    assert response.status_code == 200, response.text
    with database.session() as session:
        revision = session.scalar(
            select(WorkContextRevisionModel).where(
                WorkContextRevisionModel.work_context_id == context_id,
                WorkContextRevisionModel.revision_number == 2,
            )
        )

    assert revision is not None
    assert revision.reason == "knowledge_attached"
    assert revision.snapshot["knowledge_refs"][0] == {
        "page_id": str(page_id),
        "revision_id": str(revision_id),
        "revision_hash": "a" * 64,
        "section_key": "api-contract",
        "section_hash": "b" * 64,
        "source_ids": revision.snapshot["knowledge_refs"][0]["source_ids"],
        "ordinal": 0,
    }
    assert revision.snapshot["accepted_decisions"][-1]["key"].startswith(
        "knowledge:selection:"
    )


def test_archive_work_context_requires_terminal_state_and_is_idempotent(
    client: TestClient, database: Database, workspace_id: UUID
) -> None:
    profile_id = _profile_id(client, workspace_id, "archive")
    submitted = _submit(client, workspace_id, profile_id, "archive")
    data = submitted.json()["data"]
    context_id = UUID(data["work_context_id"])
    revision_hash = data["work_context_content_hash"]

    not_archivable = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "ArchiveWorkContext",
            {"current_revision": 1, "current_content_hash": revision_hash},
            idempotency_key="context-archive-too-early",
            target_type="work_context",
            target_id=context_id,
            expected_version=1,
        ),
    )
    assert not_archivable.status_code == 409, not_archivable.text
    assert not_archivable.json()["error"]["code"] == "work_context_not_archivable"

    request_id = UUID(data["planning_request_id"])
    cancelled = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "CancelPlanningRequest",
            {},
            idempotency_key="context-cancel-before-archive",
            target_type="planning_request",
            target_id=request_id,
            expected_version=1,
        ),
    )
    assert cancelled.status_code == 200, cancelled.text

    archive = _command(
        workspace_id,
        "ArchiveWorkContext",
        {"current_revision": 1, "current_content_hash": revision_hash},
        idempotency_key="context-archive-once",
        target_type="work_context",
        target_id=context_id,
        expected_version=1,
    )
    first = client.post("/api/v1/commands", json=archive)
    assert first.status_code == 200, first.text
    replay = client.post("/api/v1/commands", json=archive)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()

    with database.session() as session:
        context = session.get(WorkContextModel, context_id)
        events = session.scalars(
            select(EventModel).where(
                EventModel.aggregate_type == "work_context",
                EventModel.aggregate_id == context_id,
            )
        ).all()
        outbox = session.scalars(select(OutboxMessageModel)).all()

    assert context is not None
    assert context.status == "archived"
    assert context.version == 2
    assert events[-1].event_type == "WorkContextArchived"
    assert "work_context.archived" in {
        row.topic
        for row in outbox
        if row.payload["aggregate_id"] == str(context_id)
    }
