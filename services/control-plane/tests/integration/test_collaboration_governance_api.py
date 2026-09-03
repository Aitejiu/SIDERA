from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.models import (
    CollaborationGovernanceHoldModel,
    EventModel,
    ProjectionChangeModel,
    WorkspaceModel,
)

from .test_collaboration_evaluation_recovery import _prepare
from .test_collaboration_governance_commands import (
    _add_current_placement,
    _prepare_held_proposal,
)

pytestmark = pytest.mark.integration

LOCAL_ACTOR = {"type": "local", "id": "aitejiu"}
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")


@contextmanager
def _client(database_url: str, *, enabled: bool) -> Iterator[TestClient]:
    app = create_app(
        Settings(
            database_url=database_url,
            collaboration_governance_enabled=enabled,
        )
    )
    with TestClient(app) as client:
        yield client


def _request(
    *,
    workspace_id: UUID,
    command_type: str,
    target_type: str,
    target_id: UUID,
    expected_version: int,
    actor: dict[str, str] = LOCAL_ACTOR,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": target_type,
        "actor": actor,
        "target_id": str(target_id),
        "expected_version": expected_version,
        "idempotency_key": str(uuid4()),
        "correlation_id": str(uuid4()),
        "payload": payload or {"transport": "web"},
    }


def test_governance_commands_are_local_only_and_version_scoped(
    database,
    database_url: str,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, snapshot, _reconciler, _prepared = _prepare(
        database, workspace_id, uow_factory
    )
    _add_current_placement(database, workspace_id, ids, snapshot)
    actors = [
        {"type": "local", "id": "wrong"},
        *[
            {"type": actor_type, "id": "aitejiu"}
            for actor_type in (
                "integration",
                "system",
                "runtime",
                "worker",
                "planner",
                "adapter",
                "dsh",
            )
        ],
    ]
    other_workspace_id = UUID("20000000-0000-0000-0000-000000000099")
    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=other_workspace_id,
                name="Other workspace",
                status="active",
                version=1,
            )
        )
        session.commit()

    with _client(str(database_url), enabled=True) as client:
        for actor in actors:
            response = client.post(
                "/api/v1/commands",
                json=_request(
                    workspace_id=workspace_id,
                    command_type="PauseCollaborationRelease",
                    target_type="worker_item",
                    target_id=ids["worker_item"],
                    expected_version=1,
                    actor=actor,
                    payload={
                        "transport": "web",
                        "target_plan_generation": snapshot.generation,
                    },
                ),
            )
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == (
                "collaboration_governance_actor_forbidden"
            )

        target_conflict = client.post(
            "/api/v1/commands",
            json=_request(
                workspace_id=workspace_id,
                command_type="PauseCollaborationRelease",
                target_type="collaboration_release_proposal",
                target_id=ids["worker_item"],
                expected_version=1,
                payload={
                    "transport": "web",
                    "target_plan_generation": snapshot.generation,
                },
            ),
        )
        assert target_conflict.status_code == 422
        assert target_conflict.json()["error"]["code"] == "validation_error"

        workspace_conflict = client.post(
            "/api/v1/commands",
            json=_request(
                workspace_id=other_workspace_id,
                command_type="PauseCollaborationRelease",
                target_type="worker_item",
                target_id=ids["worker_item"],
                expected_version=1,
                payload={
                    "transport": "web",
                    "target_plan_generation": snapshot.generation,
                },
            ),
        )
        assert workspace_conflict.status_code == 409
        assert workspace_conflict.json()["error"]["code"] == (
            "collaboration_governance_target_invalid"
        )

        version_conflict = client.post(
            "/api/v1/commands",
            json=_request(
                workspace_id=workspace_id,
                command_type="PauseCollaborationRelease",
                target_type="worker_item",
                target_id=ids["worker_item"],
                expected_version=99,
                payload={
                    "transport": "web",
                    "target_plan_generation": snapshot.generation,
                },
            ),
        )
        assert version_conflict.status_code == 409
        assert version_conflict.json()["error"]["code"] == "version_conflict"

        success = client.post(
            "/api/v1/commands",
            json=_request(
                workspace_id=workspace_id,
                command_type="PauseCollaborationRelease",
                target_type="worker_item",
                target_id=ids["worker_item"],
                expected_version=1,
                payload={
                    "transport": "web",
                    "target_plan_generation": snapshot.generation,
                },
            ),
        )
        assert success.status_code == 200, success.text

    with database.session() as session:
        holds = session.scalars(
            select(CollaborationGovernanceHoldModel).where(
                CollaborationGovernanceHoldModel.workspace_id == workspace_id
            )
        ).all()
        events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.event_type == "CollaborationReleasePaused",
            )
        ).all()
        changes = session.scalars(
            select(ProjectionChangeModel).where(
                ProjectionChangeModel.workspace_id == workspace_id,
                ProjectionChangeModel.projection_type == "collaboration_governance",
            )
        ).all()
    assert len(holds) == 1
    assert len(events) == 1
    assert len(changes) >= 1


def test_approve_collaboration_release_proposal_is_published_via_http(
    database,
    database_url: str,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, _snapshot, _prepared, _hold, proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )

    with _client(str(database_url), enabled=True) as client:
        response = client.post(
            "/api/v1/commands",
            json=_request(
                workspace_id=workspace_id,
                command_type="ApproveCollaborationReleaseProposal",
                target_type="collaboration_release_proposal",
                target_id=proposal.id,
                expected_version=proposal.resource_version,
            ),
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["proposal_id"] == str(proposal.id)


def test_reject_collaboration_release_proposal_is_published_via_http(
    database,
    database_url: str,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, _snapshot, _prepared, _hold, proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )

    with _client(str(database_url), enabled=True) as client:
        response = client.post(
            "/api/v1/commands",
            json=_request(
                workspace_id=workspace_id,
                command_type="RejectCollaborationReleaseProposal",
                target_type="collaboration_release_proposal",
                target_id=proposal.id,
                expected_version=proposal.resource_version,
                payload={"transport": "web", "reason": "needs evidence"},
            ),
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["proposal_status"] == "rejected"


def test_resume_automatic_collaboration_is_published_via_http(
    database,
    database_url: str,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, _snapshot, _prepared, hold, _proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )

    with _client(str(database_url), enabled=True) as client:
        response = client.post(
            "/api/v1/commands",
            json=_request(
                workspace_id=workspace_id,
                command_type="ResumeAutomaticCollaboration",
                target_type="collaboration_governance_hold",
                target_id=hold.id,
                expected_version=hold.resource_version,
            ),
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["governance_mode"] == "automatic"


def test_governance_queries_are_bounded_and_feature_switch_hides_actions(
    database,
    database_url: str,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, _prepared, hold, proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )
    worker_path = (
        f"/api/v1/workspaces/{workspace_id}/collaboration-governance/"
        f"worker-items/{ids['worker_item']}"
    )
    proposal_path = (
        f"/api/v1/workspaces/{workspace_id}/collaboration-governance/"
        f"proposals/{proposal.id}"
    )

    with _client(str(database_url), enabled=True) as client:
        worker_response = client.get(worker_path)
        proposal_response = client.get(proposal_path)

    assert worker_response.status_code == 200, worker_response.text
    worker_body = worker_response.json()
    assert worker_body["worker_item_id"] == str(ids["worker_item"])
    assert worker_body["governance"]["active_hold_id"] == str(hold.id)
    assert "review before release" in worker_body["governance"]["hold_reason"]
    assert worker_body["governance"]["available_actions"] == [
        "approve",
        "reject",
        "resume_automatic",
    ]
    assert "runtime_capability_snapshot_json" not in json.dumps(worker_body)

    assert proposal_response.status_code == 200, proposal_response.text
    proposal_body = proposal_response.json()
    assert proposal_body["proposal_id"] == str(proposal.id)
    assert proposal_body["status"] == "pending"
    assert proposal_body["hold_id"] == str(hold.id)
    assert proposal_body["governance_mode"] == "paused"
    assert proposal_body["available_actions"] == [
        "approve",
        "reject",
        "resume_automatic",
    ]
    assert "review before release" not in json.dumps(proposal_body)
    assert "payload_json" not in json.dumps(proposal_body)
    assert "trace" not in json.dumps(proposal_body).lower()

    with _client(str(database_url), enabled=False) as client:
        disabled = client.get(
            f"/api/v1/workspaces/{workspace_id}/worker-items/{ids['worker_item']}/workspace"
        )

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["collaboration"]["available_actions"] == []


def test_disabled_governance_command_is_rejected_without_ledger_writes(
    database,
    database_url: str,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, snapshot, _reconciler, _prepared = _prepare(
        database, workspace_id, uow_factory
    )
    _add_current_placement(database, workspace_id, ids, snapshot)

    with _client(str(database_url), enabled=False) as client:
        response = client.post(
            "/api/v1/commands",
            json=_request(
                workspace_id=workspace_id,
                command_type="PauseCollaborationRelease",
                target_type="worker_item",
                target_id=ids["worker_item"],
                expected_version=1,
                payload={
                    "transport": "web",
                    "target_plan_generation": snapshot.generation,
                },
            ),
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "collaboration_governance_disabled"
    with database.session() as session:
        assert (
            session.scalars(
                select(CollaborationGovernanceHoldModel).where(
                    CollaborationGovernanceHoldModel.workspace_id == workspace_id
                )
            ).all()
            == []
        )
