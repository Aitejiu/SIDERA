from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from sidera_control_plane.application.collaboration_governance_commands import (
    CollaborationGovernanceCommandHandler,
    GovernanceReleaseApprover,
)
from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    WorkerItemPlacement,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationRunStatus,
    RuntimeIntentApplication,
    RuntimeIntentApplicationStatus,
)
from sidera_control_plane.domain.collaboration_governance import (
    CollaborationGovernanceHold,
    CollaborationGovernanceResolution,
    CollaborationReleaseProposal,
    GovernanceHoldStatus,
    GovernanceResolutionAction,
    GovernanceResolutionResult,
    GovernanceTransport,
    ReleaseProposalStatus,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.worker_item import WorkerItem

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
RUN_ID = UUID("40000000-0000-0000-0000-000000000001")
PLAN_SNAPSHOT_ID = UUID("50000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("60000000-0000-0000-0000-000000000001")
REQUEST_ID = UUID("70000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("80000000-0000-0000-0000-000000000001")
DECISION_ID = UUID("90000000-0000-0000-0000-000000000001")
INTENT_ID = UUID("a0000000-0000-0000-0000-000000000001")


class FakePlans:
    def __init__(self, head: SimpleNamespace) -> None:
        self.head = head
        self.calls: list[tuple[str, bool]] = []

    def get_head(self, root_task_id, workspace_id, *, for_update=False):
        self.calls.append(("head", for_update))
        if root_task_id != ROOT_TASK_ID or workspace_id != WORKSPACE_ID:
            return None
        return self.head


class FakeRuns:
    def __init__(self, run: SimpleNamespace) -> None:
        self.run = run
        self.calls: list[tuple[str, bool]] = []

    def get_for_plan(
        self, workspace_id, plan_snapshot_id, plan_generation, *, for_update=False
    ):
        self.calls.append(("run", for_update))
        if (
            workspace_id != WORKSPACE_ID
            or plan_snapshot_id != self.run.plan_snapshot_id
            or plan_generation != self.run.plan_generation
        ):
            return None
        return self.run

    def get(self, workspace_id, run_id, *, for_update=False):
        self.calls.append(("run_by_id", for_update))
        if workspace_id != WORKSPACE_ID or run_id != self.run.id:
            return None
        return self.run


class FakeWorkerItems:
    def __init__(self, item: WorkerItem, placement: WorkerItemPlacement) -> None:
        self.item = item
        self.placement = placement
        self.calls: list[tuple[str, bool]] = []

    def get(self, item_id, workspace_id, *, for_update=False):
        self.calls.append(("worker_item", for_update))
        if item_id != self.item.id or workspace_id != self.item.workspace_id:
            return None
        return self.item

    def get_current_placement(self, item_id, workspace_id):
        self.calls.append(("placement", False))
        if item_id != self.item.id or workspace_id != self.item.workspace_id:
            return None
        return self.placement


class FakeExecutionRequests:
    def __init__(self, request: SimpleNamespace) -> None:
        self.request = request
        self.calls: list[tuple[str, bool]] = []

    def get(self, request_id, workspace_id, *, for_update=False):
        self.calls.append(("request", for_update))
        if request_id != self.request.id or workspace_id != self.request.workspace_id:
            return None
        return self.request


class FakeGovernance:
    def __init__(
        self,
        hold: CollaborationGovernanceHold,
        proposal: CollaborationReleaseProposal | None = None,
    ) -> None:
        self.holds = {hold.id: hold}
        self.proposals = {} if proposal is None else {proposal.id: proposal}
        self.resolutions: list[CollaborationGovernanceResolution] = []
        self.calls: list[tuple[str, bool]] = []

    def get_active_hold(
        self, workspace_id, run_id, worker_item_id, *, for_update=False
    ):
        self.calls.append(("active_hold", for_update))
        return next(
            (
                hold
                for hold in self.holds.values()
                if hold.workspace_id == workspace_id
                and hold.run_id == run_id
                and hold.worker_item_id == worker_item_id
                and hold.status is GovernanceHoldStatus.ACTIVE
            ),
            None,
        )

    def add_hold(self, hold):
        self.holds[hold.id] = hold

    def get_hold(self, workspace_id, hold_id, *, for_update=False):
        self.calls.append(("hold", for_update))
        hold = self.holds.get(hold_id)
        return hold if hold is not None and hold.workspace_id == workspace_id else None

    def save_hold(self, hold, previous_version):
        assert self.holds[hold.id].resource_version == previous_version
        self.holds[hold.id] = hold

    def get_proposal(self, workspace_id, proposal_id, *, for_update=False):
        self.calls.append(("proposal", for_update))
        proposal = self.proposals.get(proposal_id)
        return (
            proposal
            if proposal is not None and proposal.workspace_id == workspace_id
            else None
        )

    def list_pending_proposals_for_hold(
        self, workspace_id, hold_id, *, for_update=False
    ):
        self.calls.append(("pending_proposals", for_update))
        return tuple(
            proposal
            for proposal in self.proposals.values()
            if proposal.workspace_id == workspace_id
            and proposal.hold_id == hold_id
            and proposal.status is ReleaseProposalStatus.PENDING
        )

    def save_proposal(self, proposal, previous_version):
        assert self.proposals[proposal.id].resource_version == previous_version
        self.proposals[proposal.id] = proposal

    def add_resolution(self, resolution):
        self.resolutions.append(resolution)


class MutatingLockedHoldGovernance(FakeGovernance):
    def get_hold(self, workspace_id, hold_id, *, for_update=False):
        hold = super().get_hold(workspace_id, hold_id, for_update=for_update)
        if for_update and hold is not None:
            return replace(hold, plan_snapshot_id=uuid4())
        return hold


class FakeLedger:
    def __init__(self, application: RuntimeIntentApplication | None) -> None:
        self.application = application
        self.calls: list[tuple[str, bool]] = []

    def get_application(self, workspace_id, application_id, *, for_update=False):
        self.calls.append(("application", for_update))
        if (
            self.application is None
            or self.application.workspace_id != workspace_id
            or self.application.id != application_id
        ):
            return None
        return self.application

    def save_application(self, application, previous_version):
        assert self.application is not None
        assert self.application.resource_version == previous_version
        self.application = application


class FakeEvents:
    def __init__(self) -> None:
        self.events: list[EventData] = []

    def append_with_outbox(self, event: EventData) -> None:
        self.events.append(event)


class FakeProjectionChanges:
    def __init__(self) -> None:
        self.changes: list[dict[str, object]] = []

    def changed(self, **values):
        self.changes.append(values)
        return SimpleNamespace(**values)


class FakeUnitOfWork:
    def __init__(self, *, proposal: CollaborationReleaseProposal | None = None) -> None:
        self.head = SimpleNamespace(
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
            active_snapshot_id=PLAN_SNAPSHOT_ID,
            active_generation=3,
            active_snapshot_hash="a" * 64,
            version=4,
        )
        self.run = SimpleNamespace(
            id=RUN_ID,
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
            plan_snapshot_id=PLAN_SNAPSHOT_ID,
            plan_generation=3,
            plan_content_hash="a" * 64,
            status=CollaborationRunStatus.ACTIVE,
        )
        self.item = WorkerItem.create(
            item_id=WORKER_ITEM_ID,
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
            title="Build API",
            responsibility="Implement the API",
            now=NOW,
        )
        self.placement = WorkerItemPlacement(
            plan_snapshot_id=PLAN_SNAPSHOT_ID,
            plan_generation=3,
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
            worker_item_id=WORKER_ITEM_ID,
            worker_item_key="build.api",
            subtask_key="build",
            subtask_task_id=uuid4(),
            role="implementer",
            capability_requirements=("code",),
            worker_policy_ref=None,
            configuration_ref=None,
            created_at=NOW,
        )
        self.plans = FakePlans(self.head)
        self.collaboration_runs = FakeRuns(self.run)
        self.worker_items = FakeWorkerItems(self.item, self.placement)
        self.execution_requests = FakeExecutionRequests(
            SimpleNamespace(
                id=REQUEST_ID,
                workspace_id=WORKSPACE_ID,
                root_task_id=ROOT_TASK_ID,
                plan_snapshot_id=PLAN_SNAPSHOT_ID,
                plan_generation=3,
                worker_item_id=WORKER_ITEM_ID,
                version=1,
            )
        )
        self.hold = CollaborationGovernanceHold.create(
            id=uuid4(),
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
            run_id=RUN_ID,
            plan_snapshot_id=PLAN_SNAPSHOT_ID,
            plan_generation=3,
            plan_content_hash="a" * 64,
            worker_item_id=WORKER_ITEM_ID,
            created_by_actor_id="aitejiu",
            created_via=GovernanceTransport.WEB,
            reason="existing hold",
            now=NOW,
        )
        self.governance = FakeGovernance(self.hold, proposal)
        self.collaboration_governance = self.governance
        self.application: RuntimeIntentApplication | None = None
        self.collaboration_ledger = FakeLedger(self.application)
        self.events = FakeEvents()
        self.projection_changes = FakeProjectionChanges()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def flush(self) -> None:
        return None

    @contextmanager
    def savepoint(self):
        yield


class RecordingApprover:
    def __init__(self) -> None:
        self.commands: list[CommandEnvelope] = []

    def approve(self, command: CommandEnvelope, *, now: datetime) -> CommandResult:
        self.commands.append(command)
        return CommandResult(
            command_id=command.id,
            aggregate_id=command.target_id or uuid4(),
            aggregate_version=command.expected_version or 1,
            event_ids=(),
            data={"approved_by": "fake"},
        )


def envelope(
    command_type: str,
    *,
    target_type: str,
    target_id: UUID | None,
    expected_version: int | None,
    payload: dict[str, object] | None = None,
    actor_type: str = "local",
    actor_id: str = "aitejiu",
) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        command_type=command_type,
        target_type=target_type,
        actor_type=actor_type,
        actor_id=actor_id,
        target_id=target_id,
        payload=payload or {"transport": "web"},
        expected_version=expected_version,
        idempotency_key=str(uuid4()),
        correlation_id=uuid4(),
    )


def handler(*, approver: GovernanceReleaseApprover | None = None, enabled: bool = True):
    return CollaborationGovernanceCommandHandler(
        clock=lambda: NOW,
        local_actor_type="local",
        local_actor_id="aitejiu",
        enabled=enabled,
        approver=approver,
    )


def assert_code(callable_obj, expected: str) -> None:
    with pytest.raises(DomainError) as caught:
        callable_obj()
    assert caught.value.code == expected


def test_public_commands_are_exact_and_approval_is_delegated() -> None:
    approver = RecordingApprover()
    command = envelope(
        "ApproveCollaborationReleaseProposal",
        target_type="collaboration_release_proposal",
        target_id=PROPOSAL_ID,
        expected_version=1,
        payload={"transport": "main_agent", "reason": "ship it"},
    )

    result = handler(approver=approver).handle(
        cast(ControlPlaneUnitOfWork, FakeUnitOfWork()), command
    )

    assert CollaborationGovernanceCommandHandler.command_types == frozenset(
        {
            "PauseCollaborationRelease",
            "ApproveCollaborationReleaseProposal",
            "RejectCollaborationReleaseProposal",
            "ResumeAutomaticCollaboration",
        }
    )
    assert approver.commands == [command]
    assert result.data == {"approved_by": "fake"}


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (
            lambda command: replace(command, target_id=None),
            "collaboration_governance_target_invalid",
        ),
        (
            lambda command: replace(command, expected_version=None),
            "collaboration_governance_target_invalid",
        ),
        (
            lambda command: replace(command, target_type="task"),
            "collaboration_governance_target_invalid",
        ),
        (
            lambda command: replace(command, actor_id="other"),
            "collaboration_governance_actor_forbidden",
        ),
        (
            lambda command: replace(command, actor_type="worker"),
            "collaboration_governance_actor_forbidden",
        ),
        (
            lambda command: replace(command, payload={"transport": "telegram"}),
            "collaboration_governance_target_invalid",
        ),
        (
            lambda command: replace(
                command, payload={"transport": "web", "reason": "x" * 2001}
            ),
            "collaboration_governance_target_invalid",
        ),
        (
            lambda command: replace(
                command, payload={"transport": "web", "reason": "bad\x00reason"}
            ),
            "collaboration_governance_target_invalid",
        ),
    ],
)
def test_command_shape_and_actor_policy_return_stable_codes(mutator, code) -> None:
    command = envelope(
        "PauseCollaborationRelease",
        target_type="worker_item",
        target_id=WORKER_ITEM_ID,
        expected_version=1,
        payload={"transport": "web", "target_plan_generation": 3},
    )
    assert_code(
        lambda: handler().handle(
            cast(ControlPlaneUnitOfWork, FakeUnitOfWork()), mutator(command)
        ),
        code,
    )


def test_disabled_governance_fails_closed() -> None:
    command = envelope(
        "PauseCollaborationRelease",
        target_type="worker_item",
        target_id=WORKER_ITEM_ID,
        expected_version=1,
        payload={"transport": "web", "target_plan_generation": 3},
    )
    assert_code(
        lambda: handler(enabled=False).handle(
            cast(ControlPlaneUnitOfWork, FakeUnitOfWork()), command
        ),
        "collaboration_governance_disabled",
    )


def test_pause_rejects_stale_generation_and_non_current_placement() -> None:
    uow = FakeUnitOfWork()
    uow.governance.holds = {}
    command = envelope(
        "PauseCollaborationRelease",
        target_type="worker_item",
        target_id=WORKER_ITEM_ID,
        expected_version=uow.item.version,
        payload={
            "transport": "web",
            "target_plan_generation": uow.head.active_generation + 1,
        },
    )
    assert_code(
        lambda: handler().handle(cast(ControlPlaneUnitOfWork, uow), command),
        "collaboration_governance_generation_stale",
    )

    uow = FakeUnitOfWork()
    uow.governance.holds = {}
    uow.worker_items.placement = replace(
        uow.worker_items.placement,
        plan_snapshot_id=uuid4(),
    )
    current_command = replace(
        command,
        payload={
            "transport": "web",
            "target_plan_generation": uow.head.active_generation,
        },
    )
    assert_code(
        lambda: handler().handle(cast(ControlPlaneUnitOfWork, uow), current_command),
        "collaboration_governance_target_invalid",
    )


def test_pause_creates_active_hold_and_does_not_pause_worker() -> None:
    uow = FakeUnitOfWork()
    uow.governance.holds = {}
    command = envelope(
        "PauseCollaborationRelease",
        target_type="worker_item",
        target_id=WORKER_ITEM_ID,
        expected_version=uow.item.version,
        payload={
            "transport": "web",
            "target_plan_generation": uow.head.active_generation,
            "reason": "review the next release",
        },
    )

    result = handler().handle(cast(ControlPlaneUnitOfWork, uow), command)

    holds = [hold for hold in uow.governance.holds.values() if hold.id != uow.hold.id]
    assert len(holds) == 1
    assert holds[0].status is GovernanceHoldStatus.ACTIVE
    assert holds[0].reason == "review the next release"
    assert result.data["governance_mode"] == "paused"
    assert result.data["hold_id"] == str(holds[0].id)
    assert uow.item.status.value == "active"
    assert len(uow.events.events) == 1
    assert uow.events.events[0].event_type == "CollaborationReleasePaused"
    assert "review the next release" not in str(uow.events.events[0].payload)

    assert_code(
        lambda: handler().handle(
            cast(ControlPlaneUnitOfWork, uow),
            replace(command, id=uuid4(), idempotency_key=str(uuid4())),
        ),
        "collaboration_governance_hold_exists",
    )


def _proposal() -> CollaborationReleaseProposal:
    return CollaborationReleaseProposal.create(
        id=PROPOSAL_ID,
        workspace_id=WORKSPACE_ID,
        hold_id=UUID("b0000000-0000-0000-0000-000000000001"),
        run_id=RUN_ID,
        projection_snapshot_id=PLAN_SNAPSHOT_ID,
        projection_hash="b" * 64,
        decision_id=DECISION_ID,
        intent_id=INTENT_ID,
        execution_request_id=REQUEST_ID,
        target_request_version=1,
        input_fingerprint_version=1,
        input_fingerprint="c" * 64,
        reason_code="eligible",
        summary="release API request",
        now=NOW,
    )


def _application() -> RuntimeIntentApplication:
    return RuntimeIntentApplication(
        id=uuid5(NAMESPACE_URL, f"sidera:collaboration-application:{INTENT_ID}"),
        workspace_id=WORKSPACE_ID,
        intent_id=INTENT_ID,
        status=RuntimeIntentApplicationStatus.DEFERRED,
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


def test_reject_resolves_proposal_and_application_without_runtime_wakeup() -> None:
    proposal = _proposal()
    uow = FakeUnitOfWork(proposal=proposal)
    uow.hold = replace(uow.hold, id=proposal.hold_id)
    uow.governance.holds = {uow.hold.id: uow.hold}
    uow.application = _application()
    uow.collaboration_ledger.application = uow.application
    command = envelope(
        "RejectCollaborationReleaseProposal",
        target_type="collaboration_release_proposal",
        target_id=proposal.id,
        expected_version=proposal.resource_version,
        payload={"transport": "web", "reason": "needs more evidence"},
    )

    result = handler().handle(cast(ControlPlaneUnitOfWork, uow), command)

    assert (
        uow.governance.proposals[proposal.id].status is ReleaseProposalStatus.REJECTED
    )
    assert uow.collaboration_ledger.application is not None
    assert (
        uow.collaboration_ledger.application.status
        is RuntimeIntentApplicationStatus.REJECTED
    )
    assert uow.governance.resolutions[0].action is GovernanceResolutionAction.REJECT
    assert uow.governance.resolutions[0].result is GovernanceResolutionResult.APPLIED
    assert uow.governance.resolutions[0].reason == "needs more evidence"
    assert uow.governance.holds[proposal.hold_id].status is GovernanceHoldStatus.ACTIVE
    assert [event.event_type for event in uow.events.events] == [
        "CollaborationReleaseProposalRejected"
    ]
    assert not any(
        change["projection_type"] == "collaboration_runtime"
        for change in uow.projection_changes.changes
    )
    assert result.data["proposal_status"] == "rejected"


def test_reject_fails_closed_when_proposal_and_hold_lineage_disagree() -> None:
    proposal = _proposal()
    uow = FakeUnitOfWork(proposal=proposal)
    uow.hold = replace(uow.hold, id=proposal.hold_id, run_id=uuid4())
    uow.governance.holds = {uow.hold.id: uow.hold}
    uow.application = _application()
    uow.collaboration_ledger.application = uow.application
    command = envelope(
        "RejectCollaborationReleaseProposal",
        target_type="collaboration_release_proposal",
        target_id=proposal.id,
        expected_version=proposal.resource_version,
        payload={"transport": "web"},
    )

    assert_code(
        lambda: handler().handle(cast(ControlPlaneUnitOfWork, uow), command),
        "collaboration_governance_lineage_invalid",
    )
    assert uow.governance.proposals[proposal.id].status is ReleaseProposalStatus.PENDING


def test_resume_fails_closed_when_locked_hold_lineage_changes() -> None:
    uow = FakeUnitOfWork()
    uow.governance = MutatingLockedHoldGovernance(uow.hold)
    uow.collaboration_governance = uow.governance
    command = envelope(
        "ResumeAutomaticCollaboration",
        target_type="collaboration_governance_hold",
        target_id=uow.hold.id,
        expected_version=uow.hold.resource_version,
        payload={"transport": "web"},
    )

    assert_code(
        lambda: handler().handle(cast(ControlPlaneUnitOfWork, uow), command),
        "collaboration_governance_lineage_invalid",
    )
    assert uow.governance.holds[uow.hold.id].status is GovernanceHoldStatus.ACTIVE


def test_resume_stales_pending_work_and_wakes_runtime_once() -> None:
    proposal = _proposal()
    uow = FakeUnitOfWork(proposal=proposal)
    uow.hold = replace(uow.hold, id=proposal.hold_id)
    uow.governance.holds = {uow.hold.id: uow.hold}
    uow.application = _application()
    uow.collaboration_ledger.application = uow.application
    command = envelope(
        "ResumeAutomaticCollaboration",
        target_type="collaboration_governance_hold",
        target_id=uow.hold.id,
        expected_version=uow.hold.resource_version,
        payload={"transport": "web"},
    )

    result = handler().handle(cast(ControlPlaneUnitOfWork, uow), command)

    assert uow.governance.holds[uow.hold.id].status is GovernanceHoldStatus.RESUMED
    assert uow.governance.proposals[proposal.id].status is ReleaseProposalStatus.STALE
    assert uow.collaboration_ledger.application is not None
    assert (
        uow.collaboration_ledger.application.status
        is RuntimeIntentApplicationStatus.STALE
    )
    assert [event.event_type for event in uow.events.events] == [
        "CollaborationAutomaticReleaseResumed",
        "CollaborationReleaseProposalStale",
    ]
    runtime_changes = [
        change
        for change in uow.projection_changes.changes
        if change["projection_type"] == "collaboration_runtime"
    ]
    assert len(runtime_changes) == 1
    assert runtime_changes[0]["resource_id"] == ROOT_TASK_ID
    assert result.data["staled_proposal_count"] == 1
