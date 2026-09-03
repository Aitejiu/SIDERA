"""R6.3.2 human release governance vertical gate."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.collaboration_governance import GovernanceTransport
from sidera_control_plane.domain.execution_requests import ExecutionRequest
from sidera_control_plane.persistence.models import (
    CollaborationGovernanceHoldModel,
    CollaborationReleaseProposalModel,
    ExecutionModel,
    ExecutionRequestModel,
    RuntimeDecisionModel,
    RuntimeIntentApplicationModel,
    RuntimeIntentModel,
    RuntimeReleaseModel,
)
from tests.e2e.r6_3_1_fake_runtime import CollaborationScenario
from tests.e2e.seed_r6_3_2_governance import seed_governance_plan

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("clean_database"),
]

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
LOCAL_ACTOR_TYPE = "local"
LOCAL_ACTOR_ID = "aitejiu"


def _api(database_url: str) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                database_url=database_url,
                collaboration_governance_enabled=True,
                local_actor_type=LOCAL_ACTOR_TYPE,
                local_actor_id=LOCAL_ACTOR_ID,
            )
        )
    )


def _command_body(
    workspace_id: UUID,
    command_type: str,
    target_type: str,
    target_id: UUID,
    expected_version: int,
    payload: dict[str, object],
    *,
    command_id: UUID | None = None,
) -> dict[str, object]:
    command = command_id or uuid4()
    return {
        "command_id": str(command),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": target_type,
        "actor": {"type": LOCAL_ACTOR_TYPE, "id": LOCAL_ACTOR_ID},
        "target_id": str(target_id),
        "expected_version": expected_version,
        "idempotency_key": f"r6.3.2:e2e:{command}",
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def _post(client: TestClient, body: dict[str, object]) -> dict[str, Any]:
    response = client.post("/api/v1/commands", json=body)
    assert response.status_code in {200, 201}, response.text
    value = response.json()
    assert isinstance(value, dict)
    return value


def _add_follow_up_request(database: Any, seed: Any, *, now: datetime) -> UUID:
    """Add another request on the held WorkerItem without changing the plan graph."""

    request_id = uuid5(
        NAMESPACE_URL,
        f"r6.3.2:follow-up-request:{seed.request_ids['downstream']}",
    )
    with database.session() as session:
        if session.get(ExecutionRequestModel, request_id) is not None:
            return request_id

    from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

    with SqlAlchemyUnitOfWork(database) as uow:
        source = uow.execution_requests.get(
            seed.request_ids["downstream"], seed.workspace_id
        )
        if source is None:
            raise AssertionError("downstream source request is missing")
        follow_up = ExecutionRequest.create(
            request_id=request_id,
            workspace_id=source.workspace_id,
            planning_request_id=source.planning_request_id,
            proposal_revision_id=source.proposal_revision_id,
            plan_snapshot_id=source.plan_snapshot_id,
            plan_generation=source.plan_generation,
            root_task_id=source.root_task_id,
            subtask_id=source.subtask_id,
            worker_item_id=source.worker_item_id,
            worker_item_key=f"{source.worker_item_key}-follow-up",
            intent_key="intent:downstream-follow-up",
            purpose="Run the next downstream integration after human review.",
            capability_requirements=source.capability_requirements,
            adapter_preferences=source.adapter_preferences,
            budget_class=source.budget_class,
            not_before=now,
            max_attempts=source.max_attempts,
            expected_artifact_refs=source.expected_artifact_refs,
            expected_evidence_refs=source.expected_evidence_refs,
            expected_outputs=source.expected_outputs,
            adapter_forbidden=source.adapter_forbidden,
            collaboration_gate_mode=source.collaboration_gate_mode,
            now=now,
        )
        uow.execution_requests.add(follow_up)
        uow.flush()
        for dependency in uow.execution_requests.list_dependencies(
            source.id, source.workspace_id
        ):
            uow.execution_requests.add_dependency(
                replace(dependency, downstream_request_id=follow_up.id)
            )
        uow.projection_changes.changed(
            workspace_id=source.workspace_id,
            projection_type="collaboration_runtime",
            resource_id=source.root_task_id,
            change_type="r6.3.2.follow_up_request_added",
            resource_version=follow_up.version,
            correlation_id=follow_up.id,
        )
    return request_id


def _count(database: Any, model: Any, workspace_id: UUID) -> int:
    with database.session() as session:
        return int(
            session.scalar(
                select(func.count()).select_from(model).where(
                    model.workspace_id == workspace_id
                )
            )
            or 0
        )


def test_human_release_vertical_sequence_preserves_scope_and_recovery(
    database,
    database_url: str,
    workspace_id: UUID,
) -> None:
    seed = seed_governance_plan(database, workspace_id, now=NOW)
    scenario = CollaborationScenario(database, seed, now=NOW)

    scenario.confirm_v4_plan()
    scenario.run_collaboration_until_idle()
    assert scenario.released_keys() == {"upstream-a", "upstream-b"}

    # A running sibling is outside the downstream hold and must continue.
    scenario._materialize_and_schedule("upstream-a")
    running_execution_id, _session_id = scenario._start_browser_session("upstream-a")

    with _api(database_url) as client:
        workspace = client.get(
            f"/api/v1/workspaces/{workspace_id}/worker-items/"
            f"{seed.worker_item_ids['downstream']}/workspace"
        )
        assert workspace.status_code == 200, workspace.text
        downstream_version = workspace.json()["worker_item"]["version"]
        pause = _command_body(
            workspace_id,
            "PauseCollaborationRelease",
            "worker_item",
            seed.worker_item_ids["downstream"],
            downstream_version,
            {
                "transport": GovernanceTransport.WEB.value,
                "target_plan_generation": 1,
                "reason": "inspect downstream release before execution",
            },
        )
        _post(client, pause)

        with database.session() as session:
            running = session.scalar(
                select(ExecutionModel).where(
                    ExecutionModel.workspace_id == workspace_id,
                    ExecutionModel.id == running_execution_id,
                )
            )
        assert running is not None and running.status == "running"

        scenario.succeed("upstream-a")
        scenario.run_collaboration_until_idle()
        scenario.succeed("upstream-b", evidence=())
        scenario.run_collaboration_until_idle()
        assert scenario.worker_state("downstream") == "waiting_evidence"
        scenario.record_required_evidence("upstream-b", "verification-report")
        scenario.run_collaboration_until_idle()

        with database.session() as session:
            proposal = session.scalar(
                select(CollaborationReleaseProposalModel).where(
                    CollaborationReleaseProposalModel.workspace_id == workspace_id,
                    CollaborationReleaseProposalModel.status == "pending",
                    CollaborationReleaseProposalModel.execution_request_id
                    == seed.request_ids["downstream"],
                )
            )
        assert proposal is not None

        root = client.get(
            f"/api/v1/tasks/{seed.root_task_id}/overview",
            params={"workspace_id": str(workspace_id)},
        )
        assert root.status_code == 200, root.text
        root_body = root.json()
        root_collaboration = root_body["collaboration"]
        assert root_collaboration["attention_required"] is True
        assert root_collaboration["pending_release_proposal_count"] == 1
        attention = root_collaboration["attention_subtasks"]
        assert attention[0]["subtask_key"] == "subtask-downstream"
        assert attention[0]["deep_link"].endswith(
            f"/tasks/{seed.root_task_id}/subtasks/subtask-downstream"
        )

        subtask = client.get(
            f"/api/v1/tasks/{seed.root_task_id}/subtasks/subtask-downstream",
            params={"workspace_id": str(workspace_id)},
        )
        assert subtask.status_code == 200, subtask.text
        downstream_worker = next(
            item
            for item in subtask.json()["worker_items"]
            if item["worker_item_id"] == str(seed.worker_item_ids["downstream"])
        )
        assert downstream_worker["attention_state"] == "decision_required"
        assert downstream_worker["worker_item_href"] == (
            f"/workspaces/{workspace_id}/worker-items/"
            f"{seed.worker_item_ids['downstream']}"
        )

        governance_view = client.get(
            f"/api/v1/workspaces/{workspace_id}/collaboration-governance/"
            f"worker-items/{seed.worker_item_ids['downstream']}"
        )
        assert governance_view.status_code == 200, governance_view.text
        assert governance_view.json()["governance"]["active_proposal"]["proposal_id"] == str(
            proposal.id
        )

        proposal_view = client.get(
            f"/api/v1/workspaces/{workspace_id}/collaboration-governance/proposals/"
            f"{proposal.id}"
        )
        assert proposal_view.status_code == 200, proposal_view.text
        assert proposal_view.json()["deep_link"].endswith(
            f"/worker-items/{seed.worker_item_ids['downstream']}"
        )

        # The same HTTP command is safe to replay and still creates one Release.
        approve = _command_body(
            workspace_id,
            "ApproveCollaborationReleaseProposal",
            "collaboration_release_proposal",
            proposal.id,
            proposal.resource_version,
            {"transport": GovernanceTransport.WEB.value, "reason": "reviewed"},
            command_id=uuid4(),
        )
        first_approval = _post(client, approve)
        replayed_approval = _post(client, approve)
        assert replayed_approval == first_approval

        with database.session() as session:
            hold = session.scalar(
                select(CollaborationGovernanceHoldModel).where(
                    CollaborationGovernanceHoldModel.workspace_id == workspace_id,
                    CollaborationGovernanceHoldModel.worker_item_id
                    == seed.worker_item_ids["downstream"],
                )
            )
            releases = session.scalars(
                select(RuntimeReleaseModel).where(
                    RuntimeReleaseModel.workspace_id == workspace_id,
                    RuntimeReleaseModel.execution_request_id
                    == seed.request_ids["downstream"],
                )
            ).all()
        assert hold is not None and hold.status == "active"
        assert len(releases) == 1

        follow_up_id = _add_follow_up_request(database, seed, now=NOW)
        scenario.run_collaboration_until_idle()
        with database.session() as session:
            second_proposal = session.scalar(
                select(CollaborationReleaseProposalModel).where(
                    CollaborationReleaseProposalModel.workspace_id == workspace_id,
                    CollaborationReleaseProposalModel.execution_request_id == follow_up_id,
                    CollaborationReleaseProposalModel.status == "pending",
                )
            )
        assert second_proposal is not None

        rejected = _command_body(
            workspace_id,
            "RejectCollaborationReleaseProposal",
            "collaboration_release_proposal",
            second_proposal.id,
            second_proposal.resource_version,
            {"transport": GovernanceTransport.WEB.value, "reason": "needs revision"},
        )
        _post(client, rejected)
        decisions_after_reject = _count(database, RuntimeDecisionModel, workspace_id)
        scenario.run_collaboration_until_idle()
        assert _count(database, RuntimeDecisionModel, workspace_id) == decisions_after_reject
        with database.session() as session:
            rejected_releases = session.scalars(
                select(RuntimeReleaseModel).where(
                    RuntimeReleaseModel.workspace_id == workspace_id,
                    RuntimeReleaseModel.execution_request_id == follow_up_id,
                )
            ).all()
        assert rejected_releases == []

        # A relevant evidence change changes the projection fingerprint and opens
        # a fresh Proposal for the same held WorkerItem/request.
        scenario.record_required_evidence("upstream-a", "new-verification-fact")
        scenario.run_collaboration_until_idle()
        with database.session() as session:
            refreshed_proposal = session.scalar(
                select(CollaborationReleaseProposalModel).where(
                    CollaborationReleaseProposalModel.workspace_id == workspace_id,
                    CollaborationReleaseProposalModel.execution_request_id == follow_up_id,
                    CollaborationReleaseProposalModel.status == "pending",
                )
            )
        assert refreshed_proposal is not None
        assert refreshed_proposal.id != second_proposal.id
        assert refreshed_proposal.input_fingerprint != second_proposal.input_fingerprint

        resume = _command_body(
            workspace_id,
            "ResumeAutomaticCollaboration",
            "collaboration_governance_hold",
            hold.id,
            hold.resource_version,
            {"transport": GovernanceTransport.WEB.value, "reason": "evidence reviewed"},
        )
        _post(client, resume)
        scenario.run_collaboration_until_idle()

    with database.session() as session:
        stored_hold = session.scalar(
            select(CollaborationGovernanceHoldModel).where(
                CollaborationGovernanceHoldModel.workspace_id == workspace_id,
                CollaborationGovernanceHoldModel.id == hold.id,
            )
        )
        proposals = session.scalars(
            select(CollaborationReleaseProposalModel).where(
                CollaborationReleaseProposalModel.workspace_id == workspace_id,
                CollaborationReleaseProposalModel.execution_request_id == follow_up_id,
            )
        ).all()
        follow_up_intent_ids = {
            item.id
            for item in session.scalars(
                select(RuntimeIntentModel).where(
                    RuntimeIntentModel.workspace_id == workspace_id,
                    RuntimeIntentModel.target_execution_request_id == follow_up_id,
                )
            ).all()
        }
        applications = [
            item
            for item in session.scalars(
                select(RuntimeIntentApplicationModel).where(
                    RuntimeIntentApplicationModel.workspace_id == workspace_id
                )
            ).all()
            if item.intent_id in follow_up_intent_ids
        ]
        follow_up_releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id == follow_up_id,
            )
        ).all()
    assert stored_hold is not None and stored_hold.status == "resumed"
    stored_refreshed = next(
        item for item in proposals if item.id == refreshed_proposal.id
    )
    assert stored_refreshed.status == "stale"
    assert any(item.status == "rejected" for item in proposals)
    assert len(applications) == 3
    assert sum(item.status == "rejected" for item in applications) == 1
    assert any(item.status == "stale" for item in applications)
    assert any(item.status == "applied" for item in applications)
    assert len(follow_up_releases) == 1
