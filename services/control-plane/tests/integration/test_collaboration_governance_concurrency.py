"""Concurrency and restart fences for the R6.3.2 release-governance ledger."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.application.collaboration_decisions import (
    CollaborationDecisionConflict,
)
from sidera_control_plane.application.collaboration_governance_commands import (
    CollaborationGovernanceCommandHandler,
)
from sidera_control_plane.application.collaboration_governance_reconciler import (
    CollaborationGovernanceReconciler,
)
from sidera_control_plane.application.collaboration_governance_release import (
    CollaborationGovernanceReleaseApplier,
)
from sidera_control_plane.application.collaboration_runtime_invoker import (
    RuntimeInvocationResult,
)
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.outbox_dispatcher import (
    DispatchReport,
    OutboxDispatcher,
    OutboxMessage,
)
from sidera_control_plane.domain.collaboration_governance import GovernanceTransport
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.models import (
    CollaborationGovernanceHoldModel,
    CollaborationGovernanceResolutionModel,
    CollaborationReleaseProposalModel,
    ExecutionRequestModel,
    OutboxMessageModel,
    RuntimeDecisionModel,
    RuntimeIntentApplicationModel,
    RuntimeReleaseModel,
)
from sidera_control_plane.persistence.outbox_store import SqlAlchemyOutboxStore

from .test_collaboration_decision_application import _service
from .test_collaboration_evaluation_recovery import _invoker, _prepare
from .test_collaboration_governance_commands import (
    _add_current_placement,
    _prepare_held_proposal,
)
from .test_collaboration_governance_reconciliation import _advance_generation

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("clean_database"),
]

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
LOCAL_ACTOR_TYPE = "local"
LOCAL_ACTOR_ID = "aitejiu"


def _governance_service(uow_factory: Any) -> CommandService:
    applier = CollaborationGovernanceReleaseApplier(
        fact_store_factory=lambda uow: SqlAlchemyCollaborationFactStore(
            session=uow.session
        )
    )
    handler = CollaborationGovernanceCommandHandler(
        clock=lambda: NOW,
        local_actor_type=LOCAL_ACTOR_TYPE,
        local_actor_id=LOCAL_ACTOR_ID,
        approver=applier,
    )
    return CommandService(uow_factory, (handler,))


def _command(
    command_type: str,
    workspace_id: UUID,
    target_id: UUID,
    expected_version: int,
    payload: dict[str, object],
    *,
    target_type: str = "collaboration_release_proposal",
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
        idempotency_key=f"r6.3.2:{command_type}:{uuid4()}",
        correlation_id=uuid4(),
    )


def _pause_command(
    workspace_id: UUID, worker_item_id: UUID, worker_version: int, generation: int
) -> CommandEnvelope:
    return _command(
        "PauseCollaborationRelease",
        workspace_id,
        worker_item_id,
        worker_version,
        {
            "transport": GovernanceTransport.WEB.value,
            "target_plan_generation": generation,
            "reason": "pause for deterministic concurrency test",
        },
        target_type="worker_item",
    )


def _approval_command(
    workspace_id: UUID, proposal_id: UUID, proposal_version: int
) -> CommandEnvelope:
    return _command(
        "ApproveCollaborationReleaseProposal",
        workspace_id,
        proposal_id,
        proposal_version,
        {"transport": GovernanceTransport.WEB.value, "reason": "approved in test"},
    )


def _rejection_command(
    workspace_id: UUID, proposal_id: UUID, proposal_version: int
) -> CommandEnvelope:
    return _command(
        "RejectCollaborationReleaseProposal",
        workspace_id,
        proposal_id,
        proposal_version,
        {"transport": GovernanceTransport.WEB.value, "reason": "needs revision"},
    )


def _resume_command(
    workspace_id: UUID, hold_id: UUID, hold_version: int
) -> CommandEnvelope:
    return _command(
        "ResumeAutomaticCollaboration",
        workspace_id,
        hold_id,
        hold_version,
        {"transport": GovernanceTransport.WEB.value, "reason": "resume in test"},
        target_type="collaboration_governance_hold",
    )


def _race(
    *operations: Callable[[], object],
) -> tuple[tuple[str, object], ...]:
    """Start operations together and return values/errors without hiding races."""

    barrier = threading.Barrier(len(operations))

    def run(operation: Callable[[], object]) -> tuple[str, object]:
        barrier.wait(timeout=10)
        try:
            return "ok", operation()
        except Exception as exc:  # noqa: BLE001 - the test asserts the loser
            return "error", exc

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(run, operation) for operation in operations]
        return tuple(future.result(timeout=30) for future in futures)


def _assert_no_pending_governance(
    database: Any,
    workspace_id: UUID,
    *,
    proposal_id: UUID | None = None,
) -> tuple[list[Any], list[Any], list[Any], list[Any]]:
    with database.session() as session:
        holds = session.scalars(
            select(CollaborationGovernanceHoldModel).where(
                CollaborationGovernanceHoldModel.workspace_id == workspace_id
            )
        ).all()
        proposals_query = select(CollaborationReleaseProposalModel).where(
            CollaborationReleaseProposalModel.workspace_id == workspace_id
        )
        if proposal_id is not None:
            proposals_query = proposals_query.where(
                CollaborationReleaseProposalModel.id == proposal_id
            )
        proposals = session.scalars(proposals_query).all()
        applications = session.scalars(
            select(RuntimeIntentApplicationModel).where(
                RuntimeIntentApplicationModel.workspace_id == workspace_id
            )
        ).all()
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id
            )
        ).all()
    return holds, proposals, applications, releases


def test_pause_and_automatic_release_converge_to_one_legal_state(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, snapshot, _reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    _add_current_placement(database, workspace_id, ids, snapshot)
    invocation = _invoker(ReferenceCollaborationRuntime(clock=lambda: NOW)).invoke(
        prepared, now=NOW
    )
    assert isinstance(invocation, RuntimeInvocationResult)
    pause = _pause_command(workspace_id, ids["worker_item"], 1, snapshot.generation)

    results = _race(
        lambda: _governance_service(uow_factory).handle(pause),
        lambda: _service(uow_factory).apply(prepared, invocation, now=NOW),
    )

    holds, proposals, applications, releases = _assert_no_pending_governance(
        database, workspace_id
    )
    assert len(holds) == 1
    assert holds[0].status == "active"
    assert len(releases) <= 1
    assert len([item for item in proposals if item.status == "pending"]) <= 1
    assert len(applications) == 1
    assert not (releases and any(item.status == "pending" for item in proposals))
    assert len(results) == 2


def test_two_approvals_have_one_release_and_one_resolution(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, _prepared, hold, proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )
    del ids, hold
    results = _race(
        lambda: _governance_service(uow_factory).handle(
            _approval_command(workspace_id, proposal.id, proposal.resource_version)
        ),
        lambda: _governance_service(uow_factory).handle(
            _approval_command(workspace_id, proposal.id, proposal.resource_version)
        ),
    )

    ok = [value for status, value in results if status == "ok"]
    errors = [value for status, value in results if status == "error"]
    assert len(ok) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], Exception)
    with database.session() as session:
        stored = session.scalar(
            select(CollaborationReleaseProposalModel).where(
                CollaborationReleaseProposalModel.workspace_id == workspace_id,
                CollaborationReleaseProposalModel.id == proposal.id,
            )
        )
        resolutions = session.scalars(
            select(CollaborationGovernanceResolutionModel).where(
                CollaborationGovernanceResolutionModel.workspace_id == workspace_id,
                CollaborationGovernanceResolutionModel.proposal_id == proposal.id,
            )
        ).all()
        applications = session.scalars(
            select(RuntimeIntentApplicationModel).where(
                RuntimeIntentApplicationModel.workspace_id == workspace_id
            )
        ).all()
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id
            )
        ).all()
    assert stored is not None and stored.status == "approved"
    assert len(resolutions) == 1
    assert len(applications) == 1 and applications[0].status == "applied"
    assert len(releases) == 1


def test_approval_and_rejection_have_one_terminal_governance_resolution(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, _snapshot, _prepared, _hold, proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )
    results = _race(
        lambda: _governance_service(uow_factory).handle(
            _approval_command(workspace_id, proposal.id, proposal.resource_version)
        ),
        lambda: _governance_service(uow_factory).handle(
            _rejection_command(workspace_id, proposal.id, proposal.resource_version)
        ),
    )

    assert sum(status == "ok" for status, _value in results) == 1
    with database.session() as session:
        stored = session.scalar(
            select(CollaborationReleaseProposalModel).where(
                CollaborationReleaseProposalModel.workspace_id == workspace_id,
                CollaborationReleaseProposalModel.id == proposal.id,
            )
        )
        resolutions = session.scalars(
            select(CollaborationGovernanceResolutionModel).where(
                CollaborationGovernanceResolutionModel.workspace_id == workspace_id,
                CollaborationGovernanceResolutionModel.proposal_id == proposal.id,
            )
        ).all()
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id
            )
        ).all()
    assert stored is not None and stored.status in {"approved", "rejected"}
    assert len(resolutions) == 1
    assert len(releases) in {0, 1}
    if stored.status == "approved":
        assert len(releases) == 1
    else:
        assert releases == []


def test_approval_and_resume_never_leave_a_pending_proposal(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, _snapshot, _prepared, hold, proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )
    results = _race(
        lambda: _governance_service(uow_factory).handle(
            _approval_command(workspace_id, proposal.id, proposal.resource_version)
        ),
        lambda: _governance_service(uow_factory).handle(
            _resume_command(workspace_id, hold.id, hold.resource_version)
        ),
    )

    holds, proposals, applications, releases = _assert_no_pending_governance(
        database, workspace_id, proposal_id=proposal.id
    )
    assert sum(status == "ok" for status, _value in results) >= 1
    assert len(holds) == 1 and holds[0].status == "resumed"
    assert len(proposals) == 1 and proposals[0].status != "pending"
    assert len(applications) == 1 and applications[0].status != "deferred"
    assert len(releases) <= 1


def test_resume_and_runtime_decision_application_converge_without_duplicate_release(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, snapshot, _reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    _add_current_placement(database, workspace_id, ids, snapshot)
    from sidera_control_plane.domain.collaboration_governance import (
        CollaborationGovernanceHold,
    )

    with uow_factory() as uow:
        hold = CollaborationGovernanceHold.create(
            id=uuid4(),
            workspace_id=workspace_id,
            root_task_id=prepared.run.root_task_id,
            run_id=prepared.run.id,
            plan_snapshot_id=prepared.run.plan_snapshot_id,
            plan_generation=prepared.run.plan_generation,
            plan_content_hash=prepared.run.plan_content_hash,
            worker_item_id=ids["worker_item"],
            created_by_actor_id=LOCAL_ACTOR_ID,
            created_via=GovernanceTransport.WEB,
            reason="resume race",
            now=NOW,
        )
        uow.collaboration_governance.add_hold(hold)
    invocation = _invoker(ReferenceCollaborationRuntime(clock=lambda: NOW)).invoke(
        prepared, now=NOW
    )
    assert isinstance(invocation, RuntimeInvocationResult)

    results = _race(
        lambda: _governance_service(uow_factory).handle(
            _resume_command(workspace_id, hold.id, hold.resource_version)
        ),
        lambda: _service(uow_factory).apply(prepared, invocation, now=NOW),
    )

    holds, proposals, applications, releases = _assert_no_pending_governance(
        database, workspace_id
    )
    assert len(holds) == 1 and holds[0].status == "resumed"
    assert not any(item.status == "pending" for item in proposals)
    assert len(applications) == 1
    assert len(releases) <= 1
    assert len(results) == 2


def test_two_workers_applying_one_intent_create_one_decision_and_release(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, _snapshot, _reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    invocation = _invoker(ReferenceCollaborationRuntime(clock=lambda: NOW)).invoke(
        prepared, now=NOW
    )
    assert isinstance(invocation, RuntimeInvocationResult)
    results = _race(
        lambda: _service(uow_factory).apply(prepared, invocation, now=NOW),
        lambda: _service(uow_factory).apply(prepared, invocation, now=NOW),
    )

    assert sum(status == "ok" for status, _value in results) == 1
    errors = [value for status, value in results if status == "error"]
    assert len(errors) == 1
    assert isinstance(errors[0], CollaborationDecisionConflict)
    with database.session() as session:
        decisions = session.scalars(
            select(RuntimeDecisionModel).where(
                RuntimeDecisionModel.workspace_id == workspace_id
            )
        ).all()
        applications = session.scalars(
            select(RuntimeIntentApplicationModel).where(
                RuntimeIntentApplicationModel.workspace_id == workspace_id
            )
        ).all()
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id
            )
        ).all()
    assert len(decisions) == 1
    assert len(applications) == 1
    assert len(releases) == 1


def test_upstream_fact_revision_fences_a_concurrent_approval(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, _prepared, _hold, proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )
    committed = threading.Event()

    def revise_fact() -> None:
        with database.session() as session:
            session.execute(
                update(ExecutionRequestModel)
                .where(
                    ExecutionRequestModel.workspace_id == workspace_id,
                    ExecutionRequestModel.id == ids["request"],
                )
                .values(expected_evidence_refs=["acceptance:changed-by-upstream"])
            )
            session.commit()
        committed.set()

    def approve_after_fact_revision() -> object:
        if not committed.wait(timeout=10):
            raise AssertionError("fact revision did not commit")
        return _governance_service(uow_factory).handle(
            _approval_command(workspace_id, proposal.id, proposal.resource_version)
        )

    results = _race(revise_fact, approve_after_fact_revision)
    assert results[0][0] == "ok"
    assert results[1][0] == "ok"
    assert getattr(results[1][1], "data", {}).get("result") == "stale"
    with database.session() as session:
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id == ids["request"],
            )
        ).all()
        stored = session.scalar(
            select(CollaborationReleaseProposalModel).where(
                CollaborationReleaseProposalModel.workspace_id == workspace_id,
                CollaborationReleaseProposalModel.id == proposal.id,
            )
        )
    assert releases == []
    assert stored is not None and stored.status == "stale"


def test_plan_revision_supersedes_hold_before_approval_can_release(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, snapshot, prepared, hold, proposal = _prepare_held_proposal(
        database, workspace_id, uow_factory
    )
    next_snapshot = _advance_generation(database, uow_factory, snapshot)
    governance = CollaborationGovernanceReconciler()
    with uow_factory() as uow:
        run = uow.collaboration_runs.get(
            workspace_id, prepared.run.id, for_update=True
        )
        assert run is not None
        previous_version = run.resource_version
        run.supersede(now=NOW)
        uow.collaboration_runs.save(run, previous_version)
        governance.supersede_run(
            uow,
            run,
            now=NOW,
            actor_id="r6.3.2-concurrency-test",
            correlation_id=run.id,
        )

    with pytest.raises(Exception) as approval_error:
        _governance_service(uow_factory).handle(
            _approval_command(workspace_id, proposal.id, proposal.resource_version)
        )
    with database.session() as session:
        stored_hold = session.scalar(
            select(CollaborationGovernanceHoldModel).where(
                CollaborationGovernanceHoldModel.workspace_id == workspace_id,
                CollaborationGovernanceHoldModel.id == hold.id,
            )
        )
        stored_proposal = session.scalar(
            select(CollaborationReleaseProposalModel).where(
                CollaborationReleaseProposalModel.workspace_id == workspace_id,
                CollaborationReleaseProposalModel.id == proposal.id,
            )
        )
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id
            )
        ).all()
    assert next_snapshot.generation == 2
    assert getattr(approval_error.value, "code", None) in {
        "collaboration_governance_proposal_not_pending",
        "version_conflict",
    }
    assert stored_hold is not None and stored_hold.status == "superseded"
    assert stored_proposal is not None and stored_proposal.status == "stale"
    assert releases == []


class _RecordingSink:
    def __init__(self) -> None:
        self.event_ids: list[UUID] = []

    def publish(self, message: OutboxMessage) -> None:
        self.event_ids.append(message.event_id)


def test_outbox_restart_reclaims_expired_claim_and_acknowledges_once(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _prepare_held_proposal(database, workspace_id, uow_factory)
    crashed_store = SqlAlchemyOutboxStore(database, claim_seconds=30)
    claimed = crashed_store.claim(1)
    assert len(claimed) == 1
    crashed = claimed[0]
    with database.session() as session:
        session.execute(
            update(OutboxMessageModel)
            .where(OutboxMessageModel.id == crashed.id)
            .values(claimed_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        session.commit()

    sink = _RecordingSink()
    report = OutboxDispatcher(SqlAlchemyOutboxStore(database), sink).dispatch_once(
        batch_size=1
    )
    assert report == DispatchReport(claimed=1, published=1, failed=0)
    assert sink.event_ids == [crashed.event_id]
    assert crashed_store.acknowledge(crashed.id, crashed.claim_token) is False
    with database.session() as session:
        stored = session.scalar(
            select(OutboxMessageModel).where(OutboxMessageModel.id == crashed.id)
        )
    assert stored is not None
    assert stored.published_at is not None
    assert stored.attempts == 2


def test_runtime_response_survives_worker_restart_before_decision_apply(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, _snapshot, reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    invocation = _invoker(ReferenceCollaborationRuntime(clock=lambda: NOW)).invoke(
        prepared, now=NOW
    )
    assert isinstance(invocation, RuntimeInvocationResult)
    assert reconciler.complete_success(prepared, invocation, now=NOW)

    recovered = reconciler.prepare_next(
        owner_id="r6.3.2-restarted-worker",
        now=NOW + timedelta(seconds=6),
        workspace_id=workspace_id,
        root_task_id=prepared.run.root_task_id,
    )
    assert recovered is not None
    assert recovered.operation_id == prepared.operation_id
    result = _service(uow_factory).apply(recovered, invocation, now=NOW + timedelta(seconds=6))
    assert result.status == "applied"
    with database.session() as session:
        decisions = session.scalars(
            select(RuntimeDecisionModel).where(
                RuntimeDecisionModel.workspace_id == workspace_id
            )
        ).all()
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id
            )
        ).all()
    assert len(decisions) == 1
    assert len(releases) == 1
