from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Self
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.application.collaboration_governance_reconciler import (
    CollaborationGovernanceReconciler,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationRunStatus,
    RuntimeIntentApplication,
    RuntimeIntentApplicationStatus,
)
from sidera_control_plane.domain.collaboration_governance import (
    CollaborationGovernanceHold,
    CollaborationReleaseProposal,
    GovernanceHoldStatus,
    GovernanceTransport,
    ReleaseProposalStatus,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("21000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("22000000-0000-0000-0000-000000000001")
OLD_RUN_ID = UUID("23000000-0000-0000-0000-000000000001")
NEW_RUN_ID = UUID("23000000-0000-0000-0000-000000000002")
OTHER_ROOT_TASK_ID = UUID("22000000-0000-0000-0000-000000000002")
OTHER_RUN_ID = UUID("23000000-0000-0000-0000-000000000003")
OLD_SNAPSHOT_ID = UUID("24000000-0000-0000-0000-000000000001")
NEW_SNAPSHOT_ID = UUID("24000000-0000-0000-0000-000000000002")
OTHER_OLD_SNAPSHOT_ID = UUID("24000000-0000-0000-0000-000000000003")
WORKER_ITEM_ID = UUID("25000000-0000-0000-0000-000000000001")
OTHER_WORKER_ITEM_ID = UUID("25000000-0000-0000-0000-000000000002")
REQUEST_ID = UUID("26000000-0000-0000-0000-000000000001")
OTHER_REQUEST_ID = UUID("26000000-0000-0000-0000-000000000002")
PROJECTION_ID = UUID("27000000-0000-0000-0000-000000000001")
DECISION_ID = UUID("28000000-0000-0000-0000-000000000001")
HASH = "a" * 64


def _head(
    *,
    root_task_id: UUID = ROOT_TASK_ID,
    snapshot_id: UUID = NEW_SNAPSHOT_ID,
) -> SimpleNamespace:
    return SimpleNamespace(
        workspace_id=WORKSPACE_ID,
        root_task_id=root_task_id,
        active_snapshot_id=snapshot_id,
        active_generation=2,
        active_snapshot_hash=HASH,
        version=2,
    )


def _run(
    *,
    run_id: UUID = OLD_RUN_ID,
    root_task_id: UUID = ROOT_TASK_ID,
    snapshot_id: UUID = OLD_SNAPSHOT_ID,
    status: CollaborationRunStatus = CollaborationRunStatus.SUPERSEDED,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        workspace_id=WORKSPACE_ID,
        root_task_id=root_task_id,
        plan_snapshot_id=snapshot_id,
        plan_generation=1,
        plan_content_hash=HASH,
        status=status,
        resource_version=2,
    )


def _hold(
    *,
    hold_id: UUID | None = None,
    root_task_id: UUID = ROOT_TASK_ID,
    run_id: UUID = OLD_RUN_ID,
    snapshot_id: UUID = OLD_SNAPSHOT_ID,
    worker_item_id: UUID = WORKER_ITEM_ID,
) -> CollaborationGovernanceHold:
    return CollaborationGovernanceHold.create(
        id=hold_id or uuid4(),
        workspace_id=WORKSPACE_ID,
        root_task_id=root_task_id,
        run_id=run_id,
        plan_snapshot_id=snapshot_id,
        plan_generation=1,
        plan_content_hash=HASH,
        worker_item_id=worker_item_id,
        created_by_actor_id="aitejiu",
        created_via=GovernanceTransport.WEB,
        reason="review",
        now=NOW,
    )


def _proposal(
    hold: CollaborationGovernanceHold,
    *,
    proposal_id: UUID | None = None,
    status: ReleaseProposalStatus = ReleaseProposalStatus.PENDING,
    intent_id: UUID | None = None,
    request_id: UUID = REQUEST_ID,
) -> CollaborationReleaseProposal:
    proposal = CollaborationReleaseProposal.create(
        id=proposal_id or uuid4(),
        workspace_id=WORKSPACE_ID,
        hold_id=hold.id,
        run_id=hold.run_id,
        projection_snapshot_id=PROJECTION_ID,
        projection_hash=HASH,
        decision_id=DECISION_ID,
        intent_id=intent_id or uuid4(),
        execution_request_id=request_id,
        target_request_version=1,
        input_fingerprint_version=1,
        input_fingerprint=HASH,
        reason_code="eligible",
        summary="release request",
        now=NOW,
    )
    if status is ReleaseProposalStatus.APPROVED:
        return proposal.approve(now=NOW)
    if status is ReleaseProposalStatus.REJECTED:
        return proposal.reject(now=NOW)
    if status is ReleaseProposalStatus.STALE:
        return proposal.stale(now=NOW)
    return proposal


def _application(
    intent_id: UUID, *, status: RuntimeIntentApplicationStatus
) -> RuntimeIntentApplication:
    return RuntimeIntentApplication(
        id=uuid5(NAMESPACE_URL, f"sidera:collaboration-application:{intent_id}"),
        workspace_id=WORKSPACE_ID,
        intent_id=intent_id,
        status=status,
        expected_run_version=1,
        observed_run_version=1,
        expected_request_version=1,
        observed_request_version=1,
        release_id=None,
        result_code="governance_hold_active",
        result_summary="awaiting human decision",
        resource_version=1,
        created_at=NOW,
        applied_at=None,
    )


class Governance:
    def __init__(
        self,
        holds: tuple[CollaborationGovernanceHold, ...],
        proposals: tuple[CollaborationReleaseProposal, ...],
        candidates: list[CollaborationGovernanceHold] | None = None,
    ) -> None:
        self.holds = {item.id: item for item in holds}
        self.proposals = {item.id: item for item in proposals}
        self.candidates = candidates if candidates is not None else list(holds)

    def list_active_holds_for_run(self, workspace_id, run_id, *, for_update=False):
        return tuple(
            item
            for item in self.holds.values()
            if item.workspace_id == workspace_id
            and item.run_id == run_id
            and item.status is GovernanceHoldStatus.ACTIVE
        )

    def list_pending_proposals_for_run(self, workspace_id, run_id, *, for_update=False):
        return tuple(
            item
            for item in self.proposals.values()
            if item.workspace_id == workspace_id
            and item.run_id == run_id
            and item.status is ReleaseProposalStatus.PENDING
        )

    def list_supersede_candidates(self, *, now, limit, workspace_id=None):
        del now
        return tuple(
            item
            for item in self.candidates[:limit]
            if workspace_id is None or item.workspace_id == workspace_id
        )

    def get_hold(self, workspace_id, hold_id, *, for_update=False):
        item = self.holds.get(hold_id)
        return item if item is not None and item.workspace_id == workspace_id else None

    def get_proposal(self, workspace_id, proposal_id, *, for_update=False):
        item = self.proposals.get(proposal_id)
        return item if item is not None and item.workspace_id == workspace_id else None

    def save_hold(self, hold, previous_version):
        assert self.holds[hold.id].resource_version == previous_version
        self.holds[hold.id] = hold

    def save_proposal(self, proposal, previous_version):
        assert self.proposals[proposal.id].resource_version == previous_version
        self.proposals[proposal.id] = proposal


class Ledger:
    def __init__(self, applications: tuple[RuntimeIntentApplication, ...]) -> None:
        self.applications = {item.id: item for item in applications}

    def get_application(self, workspace_id, application_id, *, for_update=False):
        item = self.applications.get(application_id)
        return item if item is not None and item.workspace_id == workspace_id else None

    def save_application(self, application, previous_version):
        assert self.applications[application.id].resource_version == previous_version
        self.applications[application.id] = application


class Events:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def append_with_outbox(self, event: Any) -> None:
        self.events.append(event)


class Commands:
    def __init__(self) -> None:
        self.commands: list[Any] = []

    def record_applied(self, command: Any, request_hash: str, result: Any) -> None:
        self.commands.append(command)


class ProjectionChanges:
    def __init__(self) -> None:
        self.changes: list[dict[str, object]] = []

    def changed(self, **values: object) -> SimpleNamespace:
        self.changes.append(values)
        return SimpleNamespace(**values)


class Uow:
    def __init__(
        self,
        *,
        holds: tuple[CollaborationGovernanceHold, ...],
        proposals: tuple[CollaborationReleaseProposal, ...],
        applications: tuple[RuntimeIntentApplication, ...],
        candidates: list[CollaborationGovernanceHold] | None = None,
    ) -> None:
        self.head = _head()
        self.run = _run()
        self.runs = {self.run.id: self.run}
        self.plans = SimpleNamespace(
            get_head=lambda *_args, **_kwargs: self.head,
        )
        self.collaboration_runs = SimpleNamespace(get=self._get_run)
        self.worker_items = SimpleNamespace(get=self._get_worker_item)
        self.execution_requests = SimpleNamespace(get=self._get_request)
        self.collaboration_governance = Governance(
            holds,
            proposals,
            candidates=candidates,
        )
        self.collaboration_ledger = Ledger(applications)
        self.events = Events()
        self.commands = Commands()
        self.projection_changes = ProjectionChanges()
        self.flush_count = 0

    def _get_run(self, workspace_id, run_id, **_kwargs):
        run = self.runs.get(run_id)
        return run if run is not None and run.workspace_id == workspace_id else None

    def _get_worker_item(self, item_id, workspace_id, **_kwargs):
        if item_id != WORKER_ITEM_ID or workspace_id != WORKSPACE_ID:
            return None
        return SimpleNamespace(
            id=item_id,
            workspace_id=workspace_id,
            root_task_id=ROOT_TASK_ID,
            version=1,
        )

    def _get_request(self, request_id, workspace_id, **_kwargs):
        if request_id != REQUEST_ID or workspace_id != WORKSPACE_ID:
            return None
        return SimpleNamespace(
            id=request_id,
            workspace_id=workspace_id,
            root_task_id=ROOT_TASK_ID,
            worker_item_id=WORKER_ITEM_ID,
            version=1,
        )

    def flush(self) -> None:
        self.flush_count += 1

    @contextmanager
    def savepoint(self):
        yield

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _arrange(
    *, include_terminal: bool = True
) -> tuple[Uow, CollaborationGovernanceHold, CollaborationReleaseProposal]:
    hold = _hold()
    pending = _proposal(hold)
    proposals = [pending]
    if include_terminal:
        proposals.extend(
            (
                _proposal(hold, status=ReleaseProposalStatus.APPROVED),
                _proposal(hold, status=ReleaseProposalStatus.REJECTED),
            )
        )
    applications = (
        _application(pending.intent_id, status=RuntimeIntentApplicationStatus.DEFERRED),
    )
    return (
        Uow(holds=(hold,), proposals=tuple(proposals), applications=applications),
        hold,
        pending,
    )


def test_supersede_run_converges_only_open_governance_and_keeps_history() -> None:
    uow, hold, pending = _arrange()
    reconciler = CollaborationGovernanceReconciler()

    result = reconciler.supersede_run(
        uow,
        uow.run,
        now=NOW,
        actor_id="governance-reconciler",
        correlation_id=uow.run.id,
    )

    assert result.superseded_holds == 1
    assert result.staled_proposals == 1
    assert result.staled_applications == 1
    assert (
        uow.collaboration_governance.holds[hold.id].status
        is GovernanceHoldStatus.SUPERSEDED
    )
    assert (
        uow.collaboration_governance.proposals[pending.id].status
        is ReleaseProposalStatus.STALE
    )
    assert [event.event_type for event in uow.events.events] == [
        "CollaborationReleaseProposalStale",
        "CollaborationGovernanceHoldSuperseded",
    ]
    assert [item["change_type"] for item in uow.projection_changes.changes] == [
        "collaboration_release_proposal_stale",
        "collaboration_governance_hold_superseded",
    ]
    assert (
        sum(
            item.status is ReleaseProposalStatus.APPROVED
            for item in uow.collaboration_governance.proposals.values()
        )
        == 1
    )
    assert (
        sum(
            item.status is ReleaseProposalStatus.REJECTED
            for item in uow.collaboration_governance.proposals.values()
        )
        == 1
    )
    assert (
        not uow.collaboration_ledger.applications[
            uuid5(
                NAMESPACE_URL, f"sidera:collaboration-application:{pending.intent_id}"
            )
        ].status
        is RuntimeIntentApplicationStatus.DEFERRED
    )


def test_recovery_sweep_is_bounded_and_second_pass_is_a_noop() -> None:
    _first_uow, first_hold, first_pending = _arrange(include_terminal=False)
    second_hold = _hold(
        root_task_id=OTHER_ROOT_TASK_ID,
        run_id=OTHER_RUN_ID,
        snapshot_id=OTHER_OLD_SNAPSHOT_ID,
        worker_item_id=OTHER_WORKER_ITEM_ID,
    )
    second_pending = _proposal(second_hold, request_id=OTHER_REQUEST_ID)

    class SweepUow(Uow):
        def __init__(self) -> None:
            super().__init__(
                holds=(first_hold, second_hold),
                proposals=(first_pending, second_pending),
                applications=(
                    _application(
                        first_pending.intent_id,
                        status=RuntimeIntentApplicationStatus.DEFERRED,
                    ),
                    _application(
                        second_pending.intent_id,
                        status=RuntimeIntentApplicationStatus.DEFERRED,
                    ),
                ),
                candidates=[first_hold, second_hold],
            )
            self.runs = {
                OLD_RUN_ID: _run(),
                OTHER_RUN_ID: _run(
                    run_id=OTHER_RUN_ID,
                    root_task_id=OTHER_ROOT_TASK_ID,
                    snapshot_id=OTHER_OLD_SNAPSHOT_ID,
                ),
            }
            self.plans.get_head = lambda root_task_id, *_args, **_kwargs: _head(
                root_task_id=root_task_id
            )

        def _get_worker_item(self, item_id, workspace_id, **_kwargs):
            if workspace_id != WORKSPACE_ID:
                return None
            if item_id == WORKER_ITEM_ID:
                root_task_id = ROOT_TASK_ID
            elif item_id == OTHER_WORKER_ITEM_ID:
                root_task_id = OTHER_ROOT_TASK_ID
            else:
                return None
            return SimpleNamespace(
                id=item_id,
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                version=1,
            )

        def _get_request(self, request_id, workspace_id, **_kwargs):
            if workspace_id != WORKSPACE_ID:
                return None
            if request_id == REQUEST_ID:
                worker_item_id, root_task_id = WORKER_ITEM_ID, ROOT_TASK_ID
            elif request_id == OTHER_REQUEST_ID:
                worker_item_id, root_task_id = OTHER_WORKER_ITEM_ID, OTHER_ROOT_TASK_ID
            else:
                return None
            return SimpleNamespace(
                id=request_id,
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                worker_item_id=worker_item_id,
                version=1,
            )

    uow = SweepUow()
    # The first candidate is corrupt. A later valid candidate must still converge.
    uow.collaboration_ledger.applications.pop(
        uuid5(
            NAMESPACE_URL, f"sidera:collaboration-application:{first_pending.intent_id}"
        )
    )
    reconciler = CollaborationGovernanceReconciler()

    first = reconciler.recover(
        uow,
        now=NOW,
        actor_id="governance-reconciler",
        correlation_id=OLD_RUN_ID,
        limit=2,
    )

    assert first.repaired == 1
    assert first.failed == 1
    assert first.failure_codes == ("collaboration_governance_lineage_invalid",)
    assert (
        uow.collaboration_governance.holds[second_hold.id].status
        is GovernanceHoldStatus.SUPERSEDED
    )
    assert (
        uow.collaboration_governance.holds[first_hold.id].status
        is GovernanceHoldStatus.ACTIVE
    )

    uow.collaboration_governance.candidates = [first_hold]
    second = reconciler.recover(
        uow,
        now=NOW,
        actor_id="governance-reconciler",
        correlation_id=OLD_RUN_ID,
        limit=1,
    )

    assert second.repaired == 0
    assert second.failed == 1
    assert len(uow.events.events) == 2


def test_new_generation_does_not_inherit_old_hold() -> None:
    uow, hold, _pending = _arrange(include_terminal=False)
    new_run = _run(run_id=NEW_RUN_ID, status=CollaborationRunStatus.ACTIVE)
    new_run.plan_snapshot_id = NEW_SNAPSHOT_ID
    new_run.plan_generation = 2
    uow.runs[new_run.id] = new_run
    reconciler = CollaborationGovernanceReconciler()

    reconciler.supersede_run(
        uow,
        uow.run,
        now=NOW,
        actor_id="governance-reconciler",
        correlation_id=uow.run.id,
    )

    assert (
        uow.collaboration_governance.list_active_holds_for_run(WORKSPACE_ID, new_run.id)
        == ()
    )
    assert uow.collaboration_governance.holds[hold.id].run_id == OLD_RUN_ID
