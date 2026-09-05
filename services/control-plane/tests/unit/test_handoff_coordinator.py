from __future__ import annotations

from copy import copy
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from sidera_control_plane.application.handoff_coordinator import (
    HandoffCoordinator,
    HandoffRunResult,
    HandoffStepResult,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffTransition,
    HandoffTransitionStatus,
    HandoffWriterState,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("20000000-0000-0000-0000-000000000001")
SOURCE_EXECUTION_ID = UUID("30000000-0000-0000-0000-000000000001")
SOURCE_SESSION_ID = UUID("40000000-0000-0000-0000-000000000001")
SOURCE_MANIFEST_ID = UUID("50000000-0000-0000-0000-000000000001")
TARGET_WORKER_ID = UUID("60000000-0000-0000-0000-000000000001")


def _transition() -> HandoffTransition:
    return HandoffTransition(
        id=UUID("70000000-0000-0000-0000-000000000001"),
        workspace_id=WORKSPACE_ID,
        worker_item_id=WORKER_ITEM_ID,
        proposal_id=uuid4(),
        source_execution_id=SOURCE_EXECUTION_ID,
        source_session_id=SOURCE_SESSION_ID,
        source_workspace_manifest_id=SOURCE_MANIFEST_ID,
        target_worker_id=TARGET_WORKER_ID,
        target_runtime_snapshot={"worker_id": str(TARGET_WORKER_ID)},
        target_execution_id=None,
        target_session_id=None,
        target_workspace_manifest_id=None,
        handoff_context_bundle_id=None,
        target_context_version=1,
        target_context_package_id=None,
        target_context_package_hash=None,
        status=HandoffTransitionStatus.PENDING,
        phase=HandoffPhase.CHECKPOINT_SOURCE,
        writer_state=HandoffWriterState.SOURCE_ACTIVE,
        source_checkpoint_sequence=None,
        source_checkpoint_facts=None,
        source_stop_sequence=None,
        source_stop_facts=None,
        source_workspace_freeze_operation_id=None,
        source_workspace_freeze_facts=None,
        source_input_cutoff_sequence=0,
        failure_code=None,
        blocked_operation_id=None,
        logical_attempt=1,
        lease_owner=None,
        lease_expires_at=None,
        next_attempt_at=None,
        version=1,
        confirmed_by_type="user",
        confirmed_by_id="test",
        confirmed_source_channel="web",
        confirmed_at=NOW,
        started_at=None,
        completed_at=None,
    )


class _TransitionRepository:
    def __init__(self, transition: HandoffTransition) -> None:
        self.item = copy(transition)

    def claim_next(
        self,
        *,
        owner_id: str,
        now: datetime,
        limit: int,
        statuses: tuple[HandoffTransitionStatus, ...],
        lease_ttl_seconds: int,
    ) -> tuple[HandoffTransition, ...]:
        if (
            self.item.status not in statuses
            or self.item.next_attempt_at is not None
            and self.item.next_attempt_at > now
            or self.item.lease_owner is not None
            and self.item.lease_expires_at is not None
            and self.item.lease_expires_at > now
            or limit < 1
        ):
            return ()
        self.item.lease_owner = owner_id
        self.item.lease_expires_at = now + timedelta(seconds=lease_ttl_seconds)
        self.item.next_attempt_at = None
        self.item.version += 1
        return (copy(self.item),)

    def get(
        self,
        transition_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffTransition | None:
        del for_update
        if self.item.id != transition_id or self.item.workspace_id != workspace_id:
            return None
        return copy(self.item)

    def save(self, transition: HandoffTransition, previous_version: int) -> None:
        if self.item.version != previous_version:
            raise AssertionError("unexpected transition version")
        self.item = copy(transition)


class _Events:
    def __init__(self) -> None:
        self.items: list[object] = []

    def append_with_outbox(self, event: object) -> None:
        self.items.append(event)


class _Commands:
    def __init__(self) -> None:
        self.items: list[object] = []

    def find_by_id(self, _workspace_id: UUID, _command_id: UUID) -> None:
        return None

    def record_applied(
        self, envelope: object, _request_hash: str, _result: object
    ) -> None:
        self.items.append(envelope)


class _UnitOfWork:
    def __init__(self, repository: _TransitionRepository, events: _Events) -> None:
        self.handoff_transitions = repository
        self.commands = _Commands()
        self.events = events

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


class _State:
    def __init__(self) -> None:
        self.repository = _TransitionRepository(_transition())
        self.events = _Events()

    def uow(self) -> _UnitOfWork:
        return _UnitOfWork(self.repository, self.events)


class _ScriptedCoordinator(HandoffCoordinator):
    @staticmethod
    def _lock_effective_result_mapping(
        _uow, _transition, *, require_target: bool
    ) -> None:
        del require_target

    def _validate_activation_facts(self, _uow, _transition, *, now) -> None:
        del now

    def _dispatch_phase(self, transition: HandoffTransition) -> HandoffStepResult:
        next_phase = {
            HandoffPhase.CHECKPOINT_SOURCE: HandoffPhase.STOP_SOURCE,
            HandoffPhase.STOP_SOURCE: HandoffPhase.FREEZE_SOURCE_WORKSPACE,
            HandoffPhase.FREEZE_SOURCE_WORKSPACE: HandoffPhase.CREATE_TARGET_EXECUTION,
            HandoffPhase.CREATE_TARGET_EXECUTION: HandoffPhase.ASSEMBLE_BUNDLE,
            HandoffPhase.ASSEMBLE_BUNDLE: HandoffPhase.TRANSFER_WORKSPACE,
            HandoffPhase.TRANSFER_WORKSPACE: HandoffPhase.START_TARGET_SESSION,
            HandoffPhase.START_TARGET_SESSION: HandoffPhase.ACKNOWLEDGE_CONTEXT,
            HandoffPhase.ACKNOWLEDGE_CONTEXT: HandoffPhase.TRANSFER_SAFE_INPUTS,
            HandoffPhase.TRANSFER_SAFE_INPUTS: HandoffPhase.ACTIVATE_TARGET,
            HandoffPhase.ACTIVATE_TARGET: HandoffPhase.COMPLETE,
        }
        if transition.phase is HandoffPhase.COMPLETE:
            return HandoffStepResult.converge(
                payload={"target_execution_id": None},
            )
        target = next_phase[transition.phase]
        return HandoffStepResult.advance(
            target,
            event_type={
                HandoffPhase.CHECKPOINT_SOURCE: "HandoffCheckpointCaptured",
                HandoffPhase.STOP_SOURCE: "HandoffSourceStopped",
                HandoffPhase.FREEZE_SOURCE_WORKSPACE: "HandoffWorkspaceFrozen",
                HandoffPhase.CREATE_TARGET_EXECUTION: "HandoffTargetExecutionCreated",
                HandoffPhase.ASSEMBLE_BUNDLE: "HandoffContextBundleCreated",
                HandoffPhase.TRANSFER_WORKSPACE: "HandoffWorkspaceTransferred",
                HandoffPhase.START_TARGET_SESSION: "HandoffTargetSessionStarted",
                HandoffPhase.ACKNOWLEDGE_CONTEXT: "HandoffContextAcknowledged",
                HandoffPhase.TRANSFER_SAFE_INPUTS: "HandoffInputTransferred",
                HandoffPhase.ACTIVATE_TARGET: "HandoffTargetActivated",
            }[transition.phase],
            payload={"phase": transition.phase.value},
        )


def test_coordinator_advances_exact_phase_order_and_writer_states() -> None:
    state = _State()
    coordinator = _ScriptedCoordinator(
        uow_factory=state.uow,
        clock=lambda: NOW,
        owner_id="test-coordinator",
        lease_ttl_seconds=30,
        backoff_max_seconds=60,
    )

    expected = (
        (HandoffPhase.STOP_SOURCE, HandoffWriterState.SOURCE_STOP_REQUESTED),
        (HandoffPhase.FREEZE_SOURCE_WORKSPACE, HandoffWriterState.WRITER_NONE),
        (HandoffPhase.CREATE_TARGET_EXECUTION, HandoffWriterState.WRITER_NONE),
        (HandoffPhase.ASSEMBLE_BUNDLE, HandoffWriterState.WRITER_NONE),
        (HandoffPhase.TRANSFER_WORKSPACE, HandoffWriterState.WRITER_NONE),
        (HandoffPhase.START_TARGET_SESSION, HandoffWriterState.TARGET_STARTING),
        (HandoffPhase.ACKNOWLEDGE_CONTEXT, HandoffWriterState.TARGET_STARTING),
        (HandoffPhase.TRANSFER_SAFE_INPUTS, HandoffWriterState.TARGET_ACTIVE),
        (HandoffPhase.ACTIVATE_TARGET, HandoffWriterState.TARGET_ACTIVE),
        (HandoffPhase.COMPLETE, HandoffWriterState.TARGET_ACTIVE),
    )

    for phase, writer_state in expected:
        result = coordinator.run_once(limit=1)
        assert result == HandoffRunResult(
            claimed=1,
            progressed=1,
            waiting=0,
            converged=0,
            blocked=0,
            failed=0,
            unknown=0,
        )
        current = state.repository.item
        assert current.phase is phase
        assert current.writer_state is writer_state

    result = coordinator.run_once(limit=1)
    assert result == HandoffRunResult(
        claimed=1,
        progressed=0,
        waiting=0,
        converged=1,
        blocked=0,
        failed=0,
        unknown=0,
    )
    assert state.repository.item.status is HandoffTransitionStatus.CONVERGED
    assert state.repository.item.phase is HandoffPhase.COMPLETE
    assert state.repository.item.writer_state is HandoffWriterState.TARGET_ACTIVE
    assert [event.event_type for event in state.events.items] == [
        "HandoffCheckpointCaptured",
        "HandoffSourceStopped",
        "HandoffWorkspaceFrozen",
        "HandoffTargetExecutionCreated",
        "HandoffContextBundleCreated",
        "HandoffWorkspaceTransferred",
        "HandoffTargetSessionStarted",
        "HandoffContextAcknowledged",
        "HandoffInputTransferred",
        "HandoffTargetActivated",
        "HandoffConverged",
    ]


def test_coordinator_waits_without_advancing_and_restarts_without_duplicate_event() -> (
    None
):
    state = _State()

    class WaitingCoordinator(_ScriptedCoordinator):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.wait_once = True

        def _dispatch_phase(self, transition: HandoffTransition) -> HandoffStepResult:
            if self.wait_once and transition.phase is HandoffPhase.CHECKPOINT_SOURCE:
                self.wait_once = False
                return HandoffStepResult.wait()
            return super()._dispatch_phase(transition)

    coordinator = WaitingCoordinator(
        uow_factory=state.uow,
        clock=lambda: NOW,
        owner_id="test-coordinator",
        lease_ttl_seconds=30,
        backoff_max_seconds=60,
    )

    waiting = coordinator.run_once(limit=1)
    assert waiting.claimed == 1
    assert waiting.waiting == 1
    assert waiting.progressed == 0
    assert state.repository.item.phase is HandoffPhase.CHECKPOINT_SOURCE
    assert state.events.items == []

    progressed = coordinator.run_once(limit=1)
    assert progressed.progressed == 1
    assert state.repository.item.phase is HandoffPhase.STOP_SOURCE
    assert len(state.events.items) == 1

    restarted = _ScriptedCoordinator(
        uow_factory=state.uow,
        clock=lambda: NOW,
        owner_id="test-coordinator-restarted",
        lease_ttl_seconds=30,
        backoff_max_seconds=60,
    )
    repeated = restarted.run_once(limit=1)
    assert repeated.progressed == 1
    assert state.repository.item.phase is HandoffPhase.FREEZE_SOURCE_WORKSPACE
    assert [event.event_type for event in state.events.items] == [
        "HandoffCheckpointCaptured",
        "HandoffSourceStopped",
    ]
