from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.collaboration_governance import (
    CollaborationGovernanceHold,
    CollaborationGovernanceResolution,
    CollaborationReleaseProposal,
    GovernanceResolutionAction,
    GovernanceResolutionResult,
    GovernanceTransport,
)
from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

from .test_collaboration_repository import (
    _decision,
    _evaluation,
    _intent,
    _projection,
    _run,
)
from .test_r5_1_execution_request_schema import (
    NOW,
    request_row,
    seed_execution_request_facts,
)

pytestmark = pytest.mark.integration

WORKSPACE_B = UUID("00000000-0000-0000-0000-000000000002")


def _hold(ids: dict[str, UUID], workspace_id: UUID) -> CollaborationGovernanceHold:
    return CollaborationGovernanceHold.create(
        id=uuid4(),
        workspace_id=workspace_id,
        root_task_id=ids["root_task"],
        run_id=ids["run"],
        plan_snapshot_id=ids["plan"],
        plan_generation=1,
        plan_content_hash="a" * 64,
        worker_item_id=ids["worker_item"],
        created_by_actor_id="developer",
        created_via=GovernanceTransport.WEB,
        reason="inspect output",
        now=NOW,
    )


def _proposal(
    hold: CollaborationGovernanceHold,
    ids: dict[str, UUID],
    decision_id: UUID,
    intent_id: UUID,
) -> CollaborationReleaseProposal:
    return CollaborationReleaseProposal.create(
        id=uuid4(),
        workspace_id=hold.workspace_id,
        hold_id=hold.id,
        run_id=hold.run_id,
        projection_snapshot_id=ids["projection"],
        projection_hash="b" * 64,
        decision_id=decision_id,
        intent_id=intent_id,
        execution_request_id=ids["request"],
        target_request_version=1,
        input_fingerprint_version=1,
        input_fingerprint="c" * 64,
        reason_code="release_ready",
        summary="release request",
        now=NOW,
    )


def test_governance_repository_round_trips_and_scopes_reads(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    with database.session() as session:
        session.add(request_row(ids, request_id=ids["request"]))
        session.commit()
    ids["run"] = uuid4()
    ids["worker_item"] = uuid4()
    run = _run({**ids, "run": ids["run"]}, workspace_id)
    projection = _projection(run)
    ids["projection"] = projection.id
    evaluation = _evaluation(run, projection)
    decision = _decision(run, projection, evaluation, sequence=1)
    intent = _intent(run, decision, ids["request"])
    hold = _hold(ids, workspace_id)
    proposal = _proposal(hold, ids, decision.id, intent.id)
    resolution = CollaborationGovernanceResolution.create(
        id=uuid4(),
        workspace_id=workspace_id,
        proposal_id=proposal.id,
        action=GovernanceResolutionAction.APPROVE,
        result=GovernanceResolutionResult.APPLIED,
        actor_id="developer",
        transport=GovernanceTransport.WEB,
        reason=None,
        command_id=uuid4(),
        now=NOW,
    )

    with uow_factory() as uow:
        assert isinstance(uow, SqlAlchemyUnitOfWork)
        uow.collaboration_runs.add(run)
        uow.flush()
        uow.collaboration_ledger.get_or_add_projection(projection)
        uow.collaboration_evaluations.get_or_add(evaluation)
        uow.collaboration_ledger.add_decision_bundle(decision, (intent,))
        uow.collaboration_governance.add_hold(hold)
        uow.collaboration_governance.add_proposal(proposal)
        uow.collaboration_governance.add_resolution(resolution)
        uow.flush()

    with uow_factory() as uow:
        loaded_hold = uow.collaboration_governance.get_hold(workspace_id, hold.id)
        assert loaded_hold == hold
        assert uow.collaboration_governance.get_hold(WORKSPACE_B, hold.id) is None
        loaded_proposal = uow.collaboration_governance.get_proposal_for_intent(
            workspace_id, intent.id
        )
        assert loaded_proposal == proposal
        assert uow.collaboration_governance.get_resolution_for_proposal(
            workspace_id, proposal.id
        ) == resolution


def test_governance_repository_uses_compare_and_set_for_hold(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    with database.session() as session:
        session.add(request_row(ids, request_id=ids["request"]))
        session.commit()
    ids["run"] = uuid4()
    ids["worker_item"] = uuid4()
    hold = _hold(ids, workspace_id)
    with uow_factory() as uow:
        uow.collaboration_governance.add_hold(hold)

    with uow_factory() as uow:
        stored = uow.collaboration_governance.get_hold(workspace_id, hold.id)
        assert stored is not None
        resumed = stored.resume(now=datetime(2026, 9, 2, 12, 1, tzinfo=UTC))
        uow.collaboration_governance.save_hold(resumed, hold.resource_version)

    with uow_factory() as uow:
        stored = uow.collaboration_governance.get_hold(workspace_id, hold.id)
        assert stored is not None
        assert stored.status.value == "resumed"
        with pytest.raises(VersionConflict):
            uow.collaboration_governance.save_hold(stored, hold.resource_version)
