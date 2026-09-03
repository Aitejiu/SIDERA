"""Failure and restart gates for R6.3.2 human release governance."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from sidera_control_plane.application.collaboration_governance_commands import (
    CollaborationGovernanceCommandError,
    CollaborationGovernanceCommandHandler,
)
from sidera_control_plane.application.collaboration_governance_reconciler import (
    CollaborationGovernanceReconciler,
)
from sidera_control_plane.application.collaboration_governance_release import (
    CollaborationGovernanceReleaseApplier,
)
from sidera_control_plane.application.collaboration_runtime_invoker import (
    RuntimeInvocationFailure,
)
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.domain.collaboration_governance import GovernanceTransport
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.models import (
    CollaborationGovernanceHoldModel,
    CollaborationGovernanceResolutionModel,
    CollaborationReleaseProposalModel,
    RuntimeIntentApplicationModel,
    RuntimeReleaseModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

from .r6_3_1_fake_runtime import CollaborationScenario
from .seed_r6_3_2_governance import seed_governance_plan

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("clean_database"),
]

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
LOCAL_ACTOR_TYPE = "local"
LOCAL_ACTOR_ID = "aitejiu"


class _TimeoutRuntime:
    def evaluate(self, *_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError("fixture Runtime deadline exceeded")


def _service(
    uow_factory: Any,
    *,
    plan_transition_allows: Any = None,
) -> CommandService:
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda uow: SqlAlchemyCollaborationFactStore(
            session=uow.session
        ),
        plan_transition_allows=plan_transition_allows,
    )
    handler = CollaborationGovernanceCommandHandler(
        clock=lambda: NOW,
        local_actor_type=LOCAL_ACTOR_TYPE,
        local_actor_id=LOCAL_ACTOR_ID,
        approver=applier,
    )
    return CommandService(uow_factory, (handler,))


def _command(
    workspace_id: UUID,
    command_type: str,
    target_type: str,
    target_id: UUID,
    expected_version: int,
    payload: dict[str, object],
) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=workspace_id,
        command_type=command_type,
        target_type=target_type,
        actor_type=LOCAL_ACTOR_TYPE,
        actor_id=LOCAL_ACTOR_ID,
        target_id=target_id,
        payload=payload,
        expected_version=expected_version,
        idempotency_key=f"r6.3.2:fault:{uuid4()}",
        correlation_id=uuid4(),
    )


def _held_proposal(database: Any, workspace_id: UUID) -> tuple[Any, Any, Any, Any]:
    seed = seed_governance_plan(database, workspace_id, now=NOW)
    scenario = CollaborationScenario(database, seed, now=NOW)
    scenario.confirm_v4_plan()
    scenario.run_collaboration_until_idle()

    with scenario.uow_factory() as uow:
        worker_item = uow.worker_items.get(
            seed.worker_item_ids["downstream"], workspace_id
        )
        head = uow.plans.get_head(seed.root_task_id, workspace_id)
        assert worker_item is not None
        assert head is not None
        pause = _command(
            workspace_id,
            "PauseCollaborationRelease",
            "worker_item",
            worker_item.id,
            worker_item.version,
            {
                "transport": GovernanceTransport.WEB.value,
                "target_plan_generation": head.active_generation,
                "reason": "fault fixture hold",
            },
        )
    _service(scenario.uow_factory).handle(pause)

    scenario.succeed("upstream-a")
    scenario.run_collaboration_until_idle()
    scenario.succeed("upstream-b", evidence=("verification-report",))
    scenario.run_collaboration_until_idle()

    with database.session() as session:
        proposal = session.scalar(
            select(CollaborationReleaseProposalModel).where(
                CollaborationReleaseProposalModel.workspace_id == workspace_id,
                CollaborationReleaseProposalModel.execution_request_id
                == seed.request_ids["downstream"],
                CollaborationReleaseProposalModel.status == "pending",
            )
        )
        hold = session.scalar(
            select(CollaborationGovernanceHoldModel).where(
                CollaborationGovernanceHoldModel.workspace_id == workspace_id,
                CollaborationGovernanceHoldModel.worker_item_id
                == seed.worker_item_ids["downstream"],
            )
        )
    assert proposal is not None
    assert hold is not None
    return seed, scenario, hold, proposal


def _counts(database: Any, workspace_id: UUID) -> tuple[int, int, int]:
    with database.session() as session:
        return (
            len(
                session.scalars(
                    select(RuntimeReleaseModel).where(
                        RuntimeReleaseModel.workspace_id == workspace_id
                    )
                ).all()
            ),
            len(
                session.scalars(
                    select(RuntimeIntentApplicationModel).where(
                        RuntimeIntentApplicationModel.workspace_id == workspace_id
                    )
                ).all()
            ),
            len(
                session.scalars(
                    select(CollaborationGovernanceResolutionModel).where(
                        CollaborationGovernanceResolutionModel.workspace_id
                        == workspace_id
                    )
                ).all()
            ),
        )


def test_restart_after_proposal_insert_approves_exactly_once(
    database: Any,
    workspace_id: UUID,
) -> None:
    seed, _scenario, hold, proposal = _held_proposal(database, workspace_id)
    restarted_service = _service(lambda: SqlAlchemyUnitOfWork(database))

    result = restarted_service.handle(
        _command(
            workspace_id,
            "ApproveCollaborationReleaseProposal",
            "collaboration_release_proposal",
            proposal.id,
            proposal.resource_version,
            {"transport": GovernanceTransport.WEB.value, "reason": "after restart"},
        )
    )

    with database.session() as session:
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id
                == seed.request_ids["downstream"],
            )
        ).all()
        stored_proposal = session.get(CollaborationReleaseProposalModel, proposal.id)
        stored_hold = session.get(CollaborationGovernanceHoldModel, hold.id)
    assert result.data["result"] == "applied"
    assert len(releases) == 1
    assert stored_proposal is not None and stored_proposal.status == "approved"
    assert stored_hold is not None and stored_hold.status == "active"


def test_restart_after_resume_invalidation_releases_only_after_durable_recheck(
    database: Any,
    workspace_id: UUID,
) -> None:
    seed, scenario, hold, proposal = _held_proposal(database, workspace_id)
    _service(scenario.uow_factory).handle(
        _command(
            workspace_id,
            "ResumeAutomaticCollaboration",
            "collaboration_governance_hold",
            hold.id,
            hold.resource_version,
            {"transport": GovernanceTransport.WEB.value, "reason": "resume"},
        )
    )

    restarted_scenario = CollaborationScenario(database, seed, now=NOW)
    restarted_scenario.run_collaboration_until_idle()

    with database.session() as session:
        stored_proposal = session.get(CollaborationReleaseProposalModel, proposal.id)
        stored_hold = session.get(CollaborationGovernanceHoldModel, hold.id)
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id
                == seed.request_ids["downstream"],
            )
        ).all()
    assert stored_proposal is not None and stored_proposal.status == "stale"
    assert stored_hold is not None and stored_hold.status == "resumed"
    assert len(releases) == 1


def test_approval_transaction_crash_rolls_back_release_and_resolution(
    database: Any,
    workspace_id: UUID,
) -> None:
    _seed, scenario, _hold, proposal = _held_proposal(database, workspace_id)

    def crash(_uow: Any, _request: Any) -> bool:
        raise RuntimeError("fixture crash during approval transaction")

    crashing_service = _service(
        scenario.uow_factory,
        plan_transition_allows=crash,
    )
    before = _counts(database, workspace_id)
    with pytest.raises(RuntimeError, match="approval transaction"):
        crashing_service.handle(
            _command(
                workspace_id,
                "ApproveCollaborationReleaseProposal",
                "collaboration_release_proposal",
                proposal.id,
                proposal.resource_version,
                {"transport": GovernanceTransport.WEB.value, "reason": "crash"},
            )
        )

    after = _counts(database, workspace_id)
    with database.session() as session:
        stored_proposal = session.get(CollaborationReleaseProposalModel, proposal.id)
        application = session.scalar(
            select(RuntimeIntentApplicationModel).where(
                RuntimeIntentApplicationModel.workspace_id == workspace_id,
                RuntimeIntentApplicationModel.intent_id == proposal.intent_id,
            )
        )
    assert after == before
    assert stored_proposal is not None and stored_proposal.status == "pending"
    assert application is not None and application.status == "deferred"


def test_plan_revision_supersedes_hold_and_open_proposal_without_release(
    database: Any,
    workspace_id: UUID,
) -> None:
    seed, scenario, hold, proposal = _held_proposal(database, workspace_id)
    with scenario.uow_factory() as uow:
        head = uow.plans.get_head(seed.root_task_id, workspace_id, for_update=True)
        current = uow.plans.get(head.active_snapshot_id, workspace_id) if head else None
        assert head is not None and current is not None
        next_snapshot = replace(
            current,
            id=uuid4(),
            generation=head.active_generation + 1,
            parent_snapshot_id=current.id,
            source_ref="r6.3.2-fault-plan-revision",
            published_at=NOW + timedelta(seconds=1),
        )
        uow.plans.add_snapshot_and_advance_head(next_snapshot, expected_head=head)
        run = uow.collaboration_runs.get(
            workspace_id,
            hold.run_id,
            for_update=True,
        )
        assert run is not None
        previous_version = run.resource_version
        run.supersede(now=NOW + timedelta(seconds=1))
        uow.collaboration_runs.save(run, previous_version)
        result = CollaborationGovernanceReconciler().supersede_run(
            uow,
            run,
            now=NOW + timedelta(seconds=1),
            actor_id="r6.3.2-fault-reconciler",
            correlation_id=uuid4(),
        )

    with database.session() as session:
        stored_hold = session.get(CollaborationGovernanceHoldModel, hold.id)
        stored_proposal = session.get(CollaborationReleaseProposalModel, proposal.id)
        application = session.scalar(
            select(RuntimeIntentApplicationModel).where(
                RuntimeIntentApplicationModel.workspace_id == workspace_id,
                RuntimeIntentApplicationModel.intent_id == proposal.intent_id,
            )
        )
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id
                == seed.request_ids["downstream"],
            )
        ).all()
    assert result.superseded_holds == 1
    assert result.staled_proposals == 1
    assert result.staled_applications == 1
    assert stored_hold is not None and stored_hold.status == "superseded"
    assert stored_proposal is not None and stored_proposal.status == "stale"
    assert application is not None and application.status == "stale"
    assert releases == []


def test_corrupt_proposal_lineage_fails_closed_without_release(
    database: Any,
    workspace_id: UUID,
) -> None:
    seed, scenario, _hold, proposal = _held_proposal(database, workspace_id)
    with database.session() as session:
        session.execute(
            update(CollaborationReleaseProposalModel)
            .where(CollaborationReleaseProposalModel.id == proposal.id)
            .values(hold_id=uuid4())
        )
        session.commit()

    with pytest.raises(CollaborationGovernanceCommandError) as caught:
        _service(scenario.uow_factory).handle(
            _command(
                workspace_id,
                "ApproveCollaborationReleaseProposal",
                "collaboration_release_proposal",
                proposal.id,
                proposal.resource_version,
                {"transport": GovernanceTransport.WEB.value, "reason": "corrupt"},
            )
        )

    with database.session() as session:
        stored_proposal = session.get(CollaborationReleaseProposalModel, proposal.id)
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id
                == seed.request_ids["downstream"],
            )
        ).all()
    assert caught.value.code == "collaboration_governance_lineage_invalid"
    assert stored_proposal is not None and stored_proposal.status == "pending"
    assert releases == []


def test_runtime_failure_keeps_existing_pending_proposal(
    database: Any,
    workspace_id: UUID,
) -> None:
    seed, scenario, _hold, proposal = _held_proposal(database, workspace_id)
    with scenario.uow_factory() as uow:
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="collaboration_runtime",
            resource_id=scenario.seed.root_task_id,
            change_type="r6.3.2.runtime-failure",
            resource_version=1,
            correlation_id=uuid4(),
        )
    prepared = scenario.collaboration_reconciler.prepare_next(
        owner_id="r6.3.2-runtime-failure",
        now=NOW + timedelta(seconds=1),
    )
    assert prepared is not None
    failure = scenario.collaboration_invoker.invoke(
        prepared,
        now=NOW + timedelta(seconds=1),
        runtime=_TimeoutRuntime(),
    )
    assert isinstance(failure, RuntimeInvocationFailure)
    assert failure.code == "runtime_timeout"
    scenario.collaboration_reconciler.complete_failure(
        prepared,
        failure,
        now=NOW + timedelta(seconds=1),
        max_attempts=2,
        backoff_seconds=1,
        backoff_max_seconds=1,
    )

    with database.session() as session:
        stored_proposal = session.get(CollaborationReleaseProposalModel, proposal.id)
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id
                == seed.request_ids["downstream"],
            )
        ).all()
    assert stored_proposal is not None and stored_proposal.status == "pending"
    assert releases == []
