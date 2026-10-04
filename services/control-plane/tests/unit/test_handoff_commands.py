from __future__ import annotations

from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Self, cast
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.handoff_candidates import HandoffCandidateService
from sidera_control_plane.application.handoff_commands import (
    HandoffCommandError,
    HandoffCommandHandler,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_runtime import (
    ResolvedWorkerRuntime,
    RuntimeCandidateInspection,
)
from sidera_control_plane.domain.commands import (
    CommandEnvelope,
    CommandResult,
    StoredCommand,
)
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextPackageStatus,
    ContextPurpose,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSession,
    ExecutionStatus,
)
from sidera_control_plane.domain.handoffs import (
    HandoffCandidateInspection,
    HandoffCandidateList,
    HandoffPhase,
    HandoffProposal,
    HandoffProposalStatus,
    HandoffSourceChannel,
    HandoffTransition,
    HandoffTransitionStatus,
    HandoffWriterState,
)
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    HandoffCheckpointMode,
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.worker import Worker, WorkerStatus
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.domain.workspace import ExecutionWorkspaceManifest

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")


def _adapter_profile() -> WorkerAdapterCapabilityProfile:
    return WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.DURABLE,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
        context_package_versions=(1,),
        handoff_checkpoint_mode=HandoffCheckpointMode.NATIVE,
        handoff_checkpoint_versions=(1,),
        handoff_session_context_versions=(1,),
        handoff_writer_gate_versions=(1,),
        handoff_inspection=True,
    )


def _workspace_profile() -> WorkspaceProviderCapabilityProfile:
    from sidera_control_plane.domain.handoffs import HandoffTransferMode

    return WorkspaceProviderCapabilityProfile(
        isolation=SandboxCapability.ISOLATED_RUNTIME,
        writable=True,
        git_worktree=True,
        stability=StabilityLevel.STABLE,
        workspace_handoff_versions=(1,),
        workspace_transfer_modes=(HandoffTransferMode.REUSE_EXACT,),
        handoff_inspection=True,
    )


def _worker(name: str, *, status: WorkerStatus, load: int) -> Worker:
    worker = Worker.register(
        worker_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        adapter_type="opencode",
        display_name=name,
        capabilities=frozenset({"python"}),
        capacity=1,
        cost_per_hour_microunits=1,
        startup_latency_ms=1,
        now=NOW,
    )
    return replace(worker, status=status, current_load=load)


def _runtime(worker: Worker) -> ResolvedWorkerRuntime:
    return ResolvedWorkerRuntime(
        worker_id=worker.id,
        adapter_type=worker.adapter_type,
        adapter_module_instance_id=UUID("30000000-0000-0000-0000-000000000001"),
        adapter_module_config_version=1,
        workspace_module_instance_id=UUID("40000000-0000-0000-0000-000000000001"),
        workspace_module_config_version=1,
        adapter_definition_id=UUID("50000000-0000-0000-0000-000000000001"),
        workspace_definition_id=UUID("60000000-0000-0000-0000-000000000001"),
        capability_snapshot=_adapter_profile(),
        workspace_capability_snapshot=_workspace_profile(),
        resolution_facts={
            "workspace_trust_boundary_hash": "trust",
            "workspace_repository_scope_hash": "repository",
            "workspace_path_scope_hash": "paths",
        },
    )


class StaticRuntimeResolver:
    def __init__(self, runtimes: dict[UUID, ResolvedWorkerRuntime]) -> None:
        self.runtimes = runtimes
        self.calls = 0

    def inspect_candidates(
        self,
        _uow: ControlPlaneUnitOfWork,
        workers: Collection[Worker],
        _execution: Execution,
    ) -> tuple[RuntimeCandidateInspection, ...]:
        self.calls += 1
        return tuple(
            RuntimeCandidateInspection(
                worker=worker,
                resolved=self.runtimes.get(worker.id),
                issue=None,
            )
            for worker in workers
        )

    def lock_and_resolve(
        self,
        _uow: ControlPlaneUnitOfWork,
        worker: Worker,
        _execution: Execution,
    ) -> ResolvedWorkerRuntime:
        return self.runtimes[worker.id]


class StaticCandidateService:
    def __init__(self, result: HandoffCandidateList) -> None:
        self.result = result
        self.calls = 0

    def inspect_in_uow(
        self,
        _uow: ControlPlaneUnitOfWork,
        *,
        workspace_id: UUID,
        worker_item_id: UUID,
        now: datetime,
    ) -> HandoffCandidateList:
        self.calls += 1
        assert workspace_id == self.result.workspace_id
        assert worker_item_id == self.result.worker_item_id
        assert now.tzinfo is not None
        return self.result


class MemoryProposalRepository:
    def __init__(self, proposals: tuple[HandoffProposal, ...] = ()) -> None:
        self.proposals = {proposal.id: proposal for proposal in proposals}

    def get(
        self,
        proposal_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffProposal | None:
        del for_update
        proposal = self.proposals.get(proposal_id)
        return (
            replace(proposal)
            if proposal is not None and proposal.payload.workspace_id == workspace_id
            else None
        )

    def find_prepared_for_worker_item(
        self,
        worker_item_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffProposal | None:
        del for_update
        proposal = next(
            (
                proposal
                for proposal in self.proposals.values()
                if proposal.payload.worker_item_id == worker_item_id
                and proposal.payload.workspace_id == workspace_id
                and proposal.status is HandoffProposalStatus.PREPARED
            ),
            None,
        )
        return replace(proposal) if proposal is not None else None

    def add(self, proposal: HandoffProposal) -> None:
        self.proposals[proposal.id] = proposal

    def save_status(self, proposal: HandoffProposal, previous_version: int) -> None:
        current = self.proposals[proposal.id]
        assert current.version == previous_version
        self.proposals[proposal.id] = proposal


class MemoryTransitionRepository:
    def __init__(self) -> None:
        self.transitions: dict[UUID, HandoffTransition] = {}

    def get(
        self,
        transition_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffTransition | None:
        del for_update
        transition = self.transitions.get(transition_id)
        return (
            transition
            if transition is not None and transition.workspace_id == workspace_id
            else None
        )

    def get_active_for_worker_item(
        self,
        worker_item_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffTransition | None:
        del for_update
        return next(
            (
                transition
                for transition in self.transitions.values()
                if transition.worker_item_id == worker_item_id
                and transition.workspace_id == workspace_id
                and transition.status
                in {
                    HandoffTransitionStatus.PENDING,
                    HandoffTransitionStatus.RUNNING,
                    HandoffTransitionStatus.BLOCKED,
                    HandoffTransitionStatus.BLOCKED_UNKNOWN,
                    HandoffTransitionStatus.FAILED,
                }
            ),
            None,
        )

    def get_active_for_worker_items(
        self,
        worker_item_ids: tuple[UUID, ...],
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[HandoffTransition, ...]:
        del for_update
        return tuple(
            transition
            for transition in self.transitions.values()
            if transition.worker_item_id in worker_item_ids
            and transition.workspace_id == workspace_id
            and transition.status
            in {
                HandoffTransitionStatus.PENDING,
                HandoffTransitionStatus.RUNNING,
                HandoffTransitionStatus.BLOCKED,
                HandoffTransitionStatus.BLOCKED_UNKNOWN,
                HandoffTransitionStatus.FAILED,
            }
        )

    def add(self, transition: HandoffTransition) -> None:
        self.transitions[transition.id] = transition

    def save(self, _transition: HandoffTransition, _previous_version: int) -> None:
        raise NotImplementedError


class MemoryEventStore:
    def __init__(self) -> None:
        self.events: list[EventData] = []

    def append_with_outbox(self, event: EventData) -> None:
        self.events.append(event)


class MemoryCommandRepository:
    def __init__(self) -> None:
        self.records: dict[tuple[UUID, str, str, str], StoredCommand] = {}

    def find_by_id(self, workspace_id: UUID, command_id: UUID) -> StoredCommand | None:
        return next(
            (
                record
                for record in self.records.values()
                if record.workspace_id == workspace_id and record.id == command_id
            ),
            None,
        )

    def find_duplicate(
        self,
        workspace_id: UUID,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> StoredCommand | None:
        return self.records.get((workspace_id, actor_type, actor_id, idempotency_key))

    def record_applied(
        self,
        envelope: CommandEnvelope,
        request_hash: str,
        result: CommandResult,
    ) -> None:
        self.records[
            (
                envelope.workspace_id,
                envelope.actor_type,
                envelope.actor_id,
                envelope.idempotency_key,
            )
        ] = StoredCommand(
            id=envelope.id,
            workspace_id=envelope.workspace_id,
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            idempotency_key=envelope.idempotency_key,
            target_type=envelope.target_type,
            request_hash=request_hash,
            status="applied",
            result={
                "command_id": str(result.command_id),
                "aggregate_id": str(result.aggregate_id),
                "aggregate_version": result.aggregate_version,
                "event_ids": [str(event_id) for event_id in result.event_ids],
                "data": result.data,
            },
            rejection_reason=None,
        )

    def record_rejected(
        self,
        envelope: CommandEnvelope,
        request_hash: str,
        error_code: str,
        error_result: dict[str, object],
    ) -> None:
        self.records[
            (
                envelope.workspace_id,
                envelope.actor_type,
                envelope.actor_id,
                envelope.idempotency_key,
            )
        ] = StoredCommand(
            id=envelope.id,
            workspace_id=envelope.workspace_id,
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            idempotency_key=envelope.idempotency_key,
            target_type=envelope.target_type,
            request_hash=request_hash,
            status="rejected",
            result=error_result,
            rejection_reason=error_code,
        )


class HandoffUnitOfWork:
    def __init__(
        self,
        *,
        worker_item: WorkerItem,
        execution: Execution,
        session: ExecutionSession,
        source: Worker,
        target: Worker,
        candidate_service: StaticCandidateService,
        prior: HandoffProposal | None = None,
    ) -> None:
        manifest = ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=WORKSPACE_ID,
            task_id=execution.task_id,
            execution_id=execution.id,
            checkouts=(),
            location_ref="workspace-location",
            retention_seconds=3600,
            fencing_token=1,
            created_at=NOW,
        )
        manifest.mark_preparing()
        manifest.mark_ready()
        manifest.mark_active()
        self.worker_item = worker_item
        self.execution = execution
        self.session = session
        self.source = source
        self.target = target
        self.root_task = SimpleNamespace(id=worker_item.root_task_id, version=1)
        self.subtask = SimpleNamespace(id=execution.task_id, version=1)
        self.worker_items = SimpleNamespace(get=self._get_worker_item)
        self.executions = SimpleNamespace(get=self._get_execution)
        self.execution_requests = SimpleNamespace(
            get_effective_by_execution_lineage=lambda *_args, **_kwargs: None
        )
        self.tasks = SimpleNamespace(get=self._get_task)
        self.sessions = SimpleNamespace(get=self._get_session)
        self.workers = SimpleNamespace(
            get=self._get_worker, list_for_workspace=lambda _: (source, target)
        )
        self.workspaces = SimpleNamespace(
            get_for_execution=lambda *_args, **_kwargs: manifest
        )
        context_package = SimpleNamespace(
            package_id=uuid4(),
            package_hash="a" * 64,
            purpose=ContextPurpose.EXECUTION,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=execution.id,
            worker_item_id=worker_item.id,
            context_version=worker_item.context_version,
            status=ContextPackageStatus.ACTIVE,
        )
        self.context_packages = SimpleNamespace(
            get_for_consumer=lambda *_args, **_kwargs: context_package
        )
        self.plans = SimpleNamespace(
            get_head=lambda *_args, **_kwargs: SimpleNamespace(
                active_snapshot_id=UUID("70000000-0000-0000-0000-000000000001"),
                active_generation=1,
                version=1,
            )
        )
        self.plan_transitions = SimpleNamespace(
            list_for_root=lambda *_args, **_kwargs: ()
        )
        self.execution_inputs = SimpleNamespace(
            get_pending_for_session=lambda *_args, **_kwargs: None
        )
        self.gateway_operations = SimpleNamespace(
            list_for_execution=lambda *_args, **_kwargs: ()
        )
        self.handoff_proposals = MemoryProposalRepository((prior,) if prior else ())
        self.handoff_transitions = MemoryTransitionRepository()
        self.events = MemoryEventStore()
        self.commands = MemoryCommandRepository()
        self.candidate_service = candidate_service
        self.adapter_calls = 0
        self.workspace_provider_calls = 0

    def _get_worker_item(
        self, item_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> WorkerItem | None:
        del for_update
        return (
            self.worker_item
            if item_id == self.worker_item.id and workspace_id == WORKSPACE_ID
            else None
        )

    def _get_task(
        self, task_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> object | None:
        del for_update
        if workspace_id != WORKSPACE_ID:
            return None
        if task_id == self.root_task.id:
            return self.root_task
        if task_id == self.subtask.id:
            return self.subtask
        return None

    def _get_execution(
        self, execution_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> Execution | None:
        del for_update
        return (
            self.execution
            if execution_id == self.execution.id and workspace_id == WORKSPACE_ID
            else None
        )

    def _get_session(
        self, session_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ExecutionSession | None:
        del for_update
        return (
            self.session
            if session_id == self.session.id and workspace_id == WORKSPACE_ID
            else None
        )

    def _get_worker(
        self, worker_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> Worker | None:
        del for_update
        return next(
            (
                worker
                for worker in (self.source, self.target)
                if worker.id == worker_id and worker.workspace_id == workspace_id
            ),
            None,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        yield


def _fixture(
    *,
    eligible: bool = True,
    prior: HandoffProposal | None = None,
) -> tuple[HandoffUnitOfWork, WorkerItem, Worker, Worker, Execution, ExecutionSession]:
    item = WorkerItem.create(
        item_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        root_task_id=uuid4(),
        title="Handoff item",
        responsibility="Continue the implementation",
        now=NOW,
    )
    source = _worker("source", status=WorkerStatus.BUSY, load=1)
    target = _worker("target", status=WorkerStatus.IDLE, load=0)
    execution = Execution.create(
        execution_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        task_id=uuid4(),
        worker_item_id=item.id,
        lineage_id=uuid4(),
        attempt=1,
        max_attempts=2,
        purpose="implement handoff",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=None,
        not_before=NOW,
    )
    execution = replace(
        execution,
        status=ExecutionStatus.RUNNING,
        assigned_worker_id=source.id,
        started_at=NOW,
    )
    session = ExecutionSession.start(
        session_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        execution_id=execution.id,
        worker_id=source.id,
        adapter_session_id="source-session",
        now=NOW,
    )
    target_snapshot = {
        "worker_id": str(target.id),
        "adapter_type": target.adapter_type,
        "runtime_version": 1,
        "capability_snapshot_hash": "b" * 64,
    }
    candidate = HandoffCandidateInspection(
        worker_id=target.id,
        display_name=target.display_name,
        adapter_type=target.adapter_type,
        eligible=eligible,
        recommended=eligible,
        ineligibility_code=None if eligible else "handoff_target_unhealthy",
        runtime_snapshot=target_snapshot,
        capability_snapshot_hash="b" * 64,
        policy_reason={"policy": "lowest-cost-v1", "selected": eligible},
        workspace_compatibility={"transfer_mode": "reuse_exact"},
    )
    candidates = HandoffCandidateList(
        workspace_id=WORKSPACE_ID,
        worker_item_id=item.id,
        source_execution_id=execution.id,
        source_session_id=session.id,
        source_worker_id=source.id,
        source_worker_item_version=item.version,
        candidates=(candidate,),
        recommended_worker_id=target.id if eligible else None,
        blocking_codes=(),
        inspected_at=NOW,
    )
    candidate_service = StaticCandidateService(candidates)
    uow = HandoffUnitOfWork(
        worker_item=item,
        execution=execution,
        session=session,
        source=source,
        target=target,
        candidate_service=candidate_service,
        prior=prior,
    )
    return uow, item, source, target, execution, session


def _prepare_envelope(
    item: WorkerItem, target: Worker, *, key: str = "prepare-1"
) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        command_type="PrepareWorkerHandoff",
        target_type="worker_item",
        actor_type="user",
        actor_id="user:test",
        target_id=item.id,
        expected_version=item.version,
        payload={
            "target_worker_id": str(target.id),
            "handoff_reason": "source needs a replacement",
            "handoff_instruction": "continue from the accepted checkpoint",
            "input_plan": {"preserve": ["accepted-inputs"]},
            "proposal_ttl_seconds": 900,
        },
        idempotency_key=key,
        correlation_id=uuid4(),
    )


def _handler(uow: HandoffUnitOfWork) -> HandoffCommandHandler:
    resolver = StaticRuntimeResolver(
        {uow.source.id: _runtime(uow.source), uow.target.id: _runtime(uow.target)}
    )
    return HandoffCommandHandler(
        lambda: NOW,
        candidate_service=uow.candidate_service,
        runtime_resolver=cast(Any, resolver),
    )


def _confirm_envelope(
    proposal: HandoffProposal,
    *,
    key: str = "confirm-1",
    proposal_version: int | None = None,
    proposal_hash: str | None = None,
    worker_item_version: int | None = None,
) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        command_type="ConfirmWorkerHandoff",
        target_type="handoff_proposal",
        actor_type="user",
        actor_id="user:test",
        target_id=proposal.id,
        expected_version=proposal.version
        if proposal_version is None
        else proposal_version,
        payload={
            "proposal_id": str(proposal.id),
            "expected_proposal_version": proposal.version
            if proposal_version is None
            else proposal_version,
            "expected_proposal_hash": proposal.proposal_hash
            if proposal_hash is None
            else proposal_hash,
            "expected_worker_item_version": (
                proposal.payload.impact_summary["worker_item_version"]
                if worker_item_version is None
                else worker_item_version
            ),
        },
        idempotency_key=key,
        correlation_id=uuid4(),
    )


def test_prepare_captures_web_facts_and_leaves_source_running() -> None:
    uow, item, source, target, execution, session = _fixture()
    before_execution = replace(execution)
    before_session = replace(session)

    result = _handler(uow).handle(uow, _prepare_envelope(item, target))

    proposal = uow.handoff_proposals.proposals[result.aggregate_id]
    assert proposal.status is HandoffProposalStatus.PREPARED
    assert proposal.source_channel is HandoffSourceChannel.WEB
    assert proposal.payload.target_worker_id == target.id
    assert proposal.payload.source_execution_id == execution.id
    assert proposal.payload.source_session_last_sequence == session.last_sequence
    assert execution == before_execution
    assert session == before_session
    assert source.status is WorkerStatus.BUSY
    assert uow.adapter_calls == 0
    assert uow.workspace_provider_calls == 0
    assert [event.event_type for event in uow.events.events] == [
        "HandoffProposalPrepared"
    ]


def test_same_command_identity_replays_the_same_proposal() -> None:
    uow, item, _source, target, _execution, _session = _fixture()
    handler = _handler(uow)
    service = CommandService(lambda: cast(ControlPlaneUnitOfWork, uow), (handler,))
    envelope = _prepare_envelope(item, target)

    first = service.handle(envelope)
    replay = service.handle(replace(envelope, correlation_id=uuid4()))

    assert replay == first
    assert len(uow.handoff_proposals.proposals) == 1
    assert len(uow.events.events) == 1


def test_new_prepare_stales_the_previous_prepared_proposal() -> None:
    uow, item, _source, target, _execution, _session = _fixture()
    first = _handler(uow).handle(
        uow, _prepare_envelope(item, target, key="prepare-old")
    )
    second = _handler(uow).handle(
        uow,
        replace(_prepare_envelope(item, target, key="prepare-new"), id=uuid4()),
    )

    old = uow.handoff_proposals.proposals[first.aggregate_id]
    assert old.status is HandoffProposalStatus.STALE
    assert old.stale_reason == "superseded_by_new_proposal"
    assert (
        uow.handoff_proposals.proposals[second.aggregate_id].status
        is HandoffProposalStatus.PREPARED
    )
    assert [event.event_type for event in uow.events.events] == [
        "HandoffProposalPrepared",
        "HandoffProposalStale",
        "HandoffProposalPrepared",
    ]


def test_prepare_rejects_an_ineligible_target_with_a_stable_code() -> None:
    uow, item, _source, target, _execution, _session = _fixture(eligible=False)

    with pytest.raises(HandoffCommandError) as error:
        _handler(uow).handle(uow, _prepare_envelope(item, target))

    assert error.value.code == "handoff_target_ineligible"
    assert not uow.handoff_proposals.proposals
    assert not uow.events.events


def test_prepare_payload_cannot_override_the_web_source_channel() -> None:
    uow, item, _source, target, _execution, _session = _fixture()
    envelope = _prepare_envelope(item, target)
    envelope = replace(
        envelope, payload={**envelope.payload, "source_channel": "main_agent"}
    )

    with pytest.raises(HandoffCommandError) as error:
        _handler(uow).handle(uow, envelope)

    assert error.value.code == "validation_error"


def test_cancel_only_accepts_a_prepared_proposal() -> None:
    uow, item, _source, target, _execution, _session = _fixture()
    prepared = _handler(uow).handle(uow, _prepare_envelope(item, target))
    cancel = CommandEnvelope(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        command_type="CancelWorkerHandoff",
        target_type="handoff_proposal",
        actor_type="user",
        actor_id="user:test",
        target_id=prepared.aggregate_id,
        expected_version=prepared.aggregate_version,
        payload={"reason": "the replacement is no longer needed"},
        idempotency_key="cancel-1",
        correlation_id=uuid4(),
    )

    result = _handler(uow).handle(uow, cancel)
    assert result.aggregate_version == 2
    assert (
        uow.handoff_proposals.proposals[prepared.aggregate_id].status
        is HandoffProposalStatus.CANCELLED
    )
    assert uow.events.events[-1].event_type == "HandoffProposalCancelled"

    with pytest.raises(HandoffCommandError) as error:
        _handler(uow).handle(uow, replace(cancel, expected_version=2))
    assert error.value.code == "handoff_proposal_not_prepared"


def test_confirm_creates_one_pending_transition_without_external_operations() -> None:
    uow, item, _source, target, execution, session = _fixture()
    prepared = _handler(uow).handle(uow, _prepare_envelope(item, target))
    proposal = uow.handoff_proposals.proposals[prepared.aggregate_id]
    handler = _handler(uow)
    service = CommandService(lambda: cast(ControlPlaneUnitOfWork, uow), (handler,))
    confirm = _confirm_envelope(proposal)

    first = service.handle(confirm)
    replay = service.handle(replace(confirm, correlation_id=uuid4()))

    assert replay == first
    assert len(uow.handoff_transitions.transitions) == 1
    transition = next(iter(uow.handoff_transitions.transitions.values()))
    assert transition.proposal_id == proposal.id
    assert transition.phase is HandoffPhase.CHECKPOINT_SOURCE
    assert transition.writer_state is HandoffWriterState.SOURCE_ACTIVE
    assert transition.status is HandoffTransitionStatus.PENDING
    assert transition.source_input_cutoff_sequence == session.last_sequence
    assert (
        uow.handoff_proposals.proposals[proposal.id].status
        is HandoffProposalStatus.CONFIRMED
    )
    assert getattr(execution, "handoff_transition_id", None) is None
    assert transition.target_execution_id is None
    assert transition.target_session_id is None
    assert uow.adapter_calls == 0
    assert uow.workspace_provider_calls == 0
    assert [event.event_type for event in uow.events.events] == [
        "HandoffProposalPrepared",
        "HandoffConfirmed",
    ]


def test_confirm_rejects_an_exact_hash_mismatch_as_stale() -> None:
    uow, item, _source, target, _execution, _session = _fixture()
    prepared = _handler(uow).handle(uow, _prepare_envelope(item, target))
    proposal = uow.handoff_proposals.proposals[prepared.aggregate_id]

    with pytest.raises(HandoffCommandError) as error:
        _handler(uow).handle(
            uow,
            _confirm_envelope(proposal, proposal_hash="0" * 64),
        )

    assert error.value.code == "handoff_proposal_stale"
    assert not uow.handoff_transitions.transitions
    assert (
        uow.handoff_proposals.proposals[proposal.id].status
        is HandoffProposalStatus.PREPARED
    )


def test_prepare_ttl_must_be_positive_and_bounded() -> None:
    uow, item, _source, target, _execution, _session = _fixture()
    envelope = _prepare_envelope(item, target)

    with pytest.raises(HandoffCommandError) as zero:
        _handler(uow).handle(
            uow,
            replace(envelope, payload={**envelope.payload, "proposal_ttl_seconds": 0}),
        )
    assert zero.value.code == "validation_error"

    with pytest.raises(HandoffCommandError) as too_long:
        _handler(uow).handle(
            uow,
            replace(
                envelope, payload={**envelope.payload, "proposal_ttl_seconds": 86_401}
            ),
        )
    assert too_long.value.code == "validation_error"


def test_handler_exposes_the_handoff_command_surface() -> None:
    assert HandoffCommandHandler.command_types == frozenset(
        {
            "PrepareProfileHandoff",
            "ConfirmProfileHandoff",
            "PrepareWorkerHandoff",
            "CancelWorkerHandoff",
            "ConfirmWorkerHandoff",
            "RecoverWorkerHandoff",
        }
    )
    assert HandoffCandidateService is not None
