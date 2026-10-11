from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Self
from uuid import UUID, uuid4

import pytest
from sidera_agents.contracts import ProfileRef

from sidera_control_plane.application.handoff_commands import (
    HandoffCommandError,
    HandoffCommandHandler,
)
from sidera_control_plane.application.handoff_recovery import (
    HandoffRecoveryError,
    HandoffRecoveryService,
)
from sidera_control_plane.domain.agent_bindings import AssigneeRef
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.execution import ExecutionSessionStatus
from sidera_control_plane.domain.gateway import (
    GatewayOperation,
    GatewayOperationStatus,
    GatewayOperationType,
    gateway_operation_id,
    handoff_step_operation_id,
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffInspectionOutcome,
    HandoffOperationInspection,
)
from sidera_control_plane.domain.handoff_recovery import (
    HandoffRecoveryDecision,
    HandoffRecoveryInspection,
    HandoffRecoveryStrategy,
    HandoffSafeConclusion,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffProposal,
    HandoffTransition,
    HandoffTransitionStatus,
    HandoffWriterState,
    NativeHandoffProposalPayload,
)
from tests.unit.test_handoff_commands import (
    MemoryEventStore,
    StaticRuntimeResolver,
    _confirm_envelope,
    _fixture,
    _handler,
    _prepare_envelope,
    _runtime,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")


class MemoryTransitionRepository:
    def __init__(self, transition: HandoffTransition) -> None:
        self.transition = transition
        self.recovery_saves = 0

    def get(
        self,
        transition_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffTransition | None:
        del for_update
        if transition_id != self.transition.id or workspace_id != WORKSPACE_ID:
            return None
        return self.transition

    def save_recovery(
        self, transition: HandoffTransition, previous_version: int
    ) -> None:
        assert previous_version < transition.version
        self.transition = transition
        self.recovery_saves += 1


class MemoryDecisionRepository:
    def __init__(self) -> None:
        self.decisions: dict[UUID, HandoffRecoveryDecision] = {}

    def get(
        self,
        decision_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> HandoffRecoveryDecision | None:
        del for_update
        decision = self.decisions.get(decision_id)
        return (
            decision if decision is not None and workspace_id == WORKSPACE_ID else None
        )

    def add_append_only(
        self, decision: HandoffRecoveryDecision, workspace_id: UUID
    ) -> None:
        assert workspace_id == WORKSPACE_ID
        existing = self.decisions.get(decision.id)
        if existing is not None and existing != decision:
            raise AssertionError("decision changed")
        self.decisions[decision.id] = decision


class StaticGatewayInspector:
    def __init__(self, outcome: HandoffInspectionOutcome) -> None:
        self.outcome = outcome
        self.calls: list[UUID] = []

    def inspect_handoff_operation(
        self,
        *,
        workspace_id: UUID,
        transition_id: UUID,
        operation_id: UUID,
        operation_type: GatewayOperationType,
        now: datetime,
    ) -> HandoffOperationInspection:
        assert workspace_id == WORKSPACE_ID
        self.calls.append(operation_id)
        return HandoffOperationInspection(
            operation_id=operation_id,
            operation_type=operation_type,
            outcome=self.outcome,
            execution_id=None,
            session_id=None,
            accepted_sequence=3
            if operation_type is GatewayOperationType.STOP_SESSION
            else None,
            context_binding=None,
            observed_at=now,
            evidence_refs=(f"adapter-inspection://{transition_id}/{operation_id}",),
        )


class RecoveryUnitOfWork:
    def __init__(
        self,
        transition: HandoffTransition,
        *,
        operation: GatewayOperation | None = None,
        source_session: object | None = None,
        source_execution: object | None = None,
    ) -> None:
        self.handoff_transitions = MemoryTransitionRepository(transition)
        self.handoff_recovery_decisions = MemoryDecisionRepository()
        operations = {operation.operation_id: operation} if operation else {}
        self.gateway_operations = SimpleNamespace(
            get_by_operation_id=lambda operation_id, _workspace_id, **_kwargs: (
                operations.get(operation_id)
            ),
            list_for_execution=lambda *_args, **_kwargs: tuple(operations.values()),
        )
        self.sessions = SimpleNamespace(
            get_any=lambda _session_id, _workspace_id, **_kwargs: source_session,
        )
        self.executions = SimpleNamespace(
            get=lambda _execution_id, _workspace_id, **_kwargs: source_execution,
        )
        self.leases = SimpleNamespace(
            get_active_for_execution=lambda *_args, **_kwargs: None,
        )
        self.workspaces = SimpleNamespace(
            get=lambda *_args, **_kwargs: None,
            get_for_execution=lambda *_args, **_kwargs: None,
        )
        self.handoff_workspace_transfers = SimpleNamespace(
            get_by_operation=lambda *_args, **_kwargs: None,
        )
        self.handoff_transfers = self.handoff_workspace_transfers
        self.events = MemoryEventStore()
        self.session = source_session
        self.execution = source_execution

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        yield


def _base_transition() -> tuple[HandoffTransition, RecoveryUnitOfWork]:
    uow, item, _source, target, _execution, _session = _fixture()
    prepared = _handler(uow).handle(uow, _prepare_envelope(item, target))
    proposal = uow.handoff_proposals.proposals[prepared.aggregate_id]
    confirmed = _handler(uow).handle(uow, _confirm_envelope(proposal))
    transition = next(iter(uow.handoff_transitions.transitions.values()))
    assert confirmed.aggregate_id == transition.id
    recovery_uow = RecoveryUnitOfWork(
        transition,
        source_session=uow.session,
        source_execution=uow.execution,
    )
    return transition, recovery_uow


def _blocked_transition(
    phase: HandoffPhase,
    operation_id: UUID,
    *,
    status: HandoffTransitionStatus = HandoffTransitionStatus.BLOCKED_UNKNOWN,
) -> tuple[HandoffTransition, RecoveryUnitOfWork]:
    transition, _ = _base_transition()
    transition.phase = phase
    transition.writer_state = HandoffWriterState.UNKNOWN
    transition.status = HandoffTransitionStatus.RUNNING
    transition.version += 1
    transition.block(
        failure_code="external_result_unknown",
        now=NOW,
        status=status,
        blocked_operation_id=operation_id,
        workspace_only=phase
        in {
            HandoffPhase.FREEZE_SOURCE_WORKSPACE,
            HandoffPhase.CREATE_TARGET_EXECUTION,
            HandoffPhase.ASSEMBLE_BUNDLE,
            HandoffPhase.TRANSFER_WORKSPACE,
        },
    )
    return transition, RecoveryUnitOfWork(transition)


def _decision(
    transition: HandoffTransition,
    strategy: HandoffRecoveryStrategy,
    evidence_refs: tuple[str, ...],
) -> HandoffRecoveryDecision:
    return HandoffRecoveryDecision(
        id=uuid4(),
        handoff_transition_id=transition.id,
        expected_transition_version=transition.version,
        strategy=strategy,
        actor_type="user",
        actor_id="user:test",
        source_channel="web",
        reason="operator verified the bounded recovery evidence",
        evidence_refs=evidence_refs,
        replacement_target_snapshot=None,
        resulting_operation_refs=(),
        created_at=NOW,
    )


def test_unknown_operation_without_boundary_evidence_has_no_retry_strategy() -> None:
    transition, _ = _base_transition()
    transition, uow = _blocked_transition(
        HandoffPhase.CHECKPOINT_SOURCE,
        handoff_step_operation_id(
            transition.id,
            HandoffPhase.CHECKPOINT_SOURCE,
            logical_attempt=transition.logical_attempt,
            action="checkpoint",
        ),
    )
    service = HandoffRecoveryService(lambda: uow)

    inspection = service.inspect_unknown(transition=transition, now=NOW)

    assert inspection.safe_conclusions == ()
    assert (
        service.available_strategies(
            transition=transition,
            inspection=inspection,
        )
        == ()
    )


def test_failed_derived_operation_creates_a_new_attempt_without_retrying_old_row() -> (
    None
):
    transition, _ = _base_transition()
    transition.phase = HandoffPhase.CHECKPOINT_SOURCE
    transition.writer_state = HandoffWriterState.SOURCE_ACTIVE
    transition.status = HandoffTransitionStatus.RUNNING
    transition.version += 1
    operation_id = handoff_step_operation_id(
        transition.id,
        HandoffPhase.CHECKPOINT_SOURCE,
        logical_attempt=transition.logical_attempt,
        action="checkpoint",
    )
    transition.block(
        failure_code="adapter_rejected",
        now=NOW,
        status=HandoffTransitionStatus.BLOCKED,
        blocked_operation_id=operation_id,
    )
    operation = GatewayOperation.create(
        id=uuid4(),
        operation_id=operation_id,
        workspace_id=WORKSPACE_ID,
        task_id=uuid4(),
        execution_id=transition.source_execution_id,
        session_id=transition.source_session_id,
        operation_type=GatewayOperationType.HANDOFF_CHECKPOINT,
        module_instance_id=None,
        config_version=None,
        lease_id=uuid4(),
        fencing_token=1,
        now=NOW,
    )
    operation.start(now=NOW)
    operation.fail(failure_code="adapter_rejected", now=NOW)
    uow = RecoveryUnitOfWork(transition, operation=operation)
    service = HandoffRecoveryService(lambda: uow)
    inspection = service.inspect_unknown(transition=transition, now=NOW)
    assert HandoffRecoveryStrategy.RETRY_CURRENT_PHASE in service.available_strategies(
        transition=transition,
        inspection=inspection,
    )

    result = service.apply_decision(
        transition=transition,
        decision=_decision(
            transition,
            HandoffRecoveryStrategy.RETRY_CURRENT_PHASE,
            inspection.evidence_refs,
        ),
        now=NOW,
    )

    assert result.logical_attempt == 2
    assert uow.handoff_transitions.transition.logical_attempt == 2
    assert operation.status is GatewayOperationStatus.FAILED
    assert uow.handoff_transitions.recovery_saves == 1
    assert len(uow.handoff_recovery_decisions.decisions) == 1


def test_retry_same_target_revalidates_the_current_runtime_snapshot() -> None:
    source_uow, item, _source, target, execution, _session = _fixture()
    prepared = _handler(source_uow).handle(source_uow, _prepare_envelope(item, target))
    proposal = source_uow.handoff_proposals.proposals[prepared.aggregate_id]
    transition = _handler(source_uow).handle(source_uow, _confirm_envelope(proposal))
    transition = source_uow.handoff_transitions.transitions[transition.aggregate_id]
    target_execution_id = uuid4()
    transition.phase = HandoffPhase.START_TARGET_SESSION
    transition.writer_state = HandoffWriterState.TARGET_STARTING
    transition.status = HandoffTransitionStatus.RUNNING
    transition.version += 1
    transition.target_execution_id = target_execution_id
    transition.target_runtime_snapshot = {"worker_id": str(target.id)}
    target.heartbeat(healthy=True, now=NOW)
    operation_id = gateway_operation_id(
        target_execution_id,
        GatewayOperationType.START_SESSION,
    )
    transition.block(
        failure_code="adapter_rejected",
        now=NOW,
        status=HandoffTransitionStatus.BLOCKED,
        blocked_operation_id=operation_id,
    )
    operation = GatewayOperation.create(
        id=uuid4(),
        operation_id=operation_id,
        workspace_id=WORKSPACE_ID,
        task_id=execution.task_id,
        execution_id=target_execution_id,
        session_id=None,
        operation_type=GatewayOperationType.START_SESSION,
        module_instance_id=None,
        config_version=None,
        lease_id=uuid4(),
        fencing_token=1,
        now=NOW,
    )
    operation.start(now=NOW)
    operation.fail(failure_code="adapter_rejected", now=NOW)
    uow = RecoveryUnitOfWork(
        transition,
        operation=operation,
        source_session=source_uow.session,
        source_execution=execution,
    )
    uow.executions = SimpleNamespace(
        get=lambda execution_id, _workspace_id, **_kwargs: (
            execution if execution_id == execution.id else None
        )
    )
    uow.workers = SimpleNamespace(
        get=lambda worker_id, _workspace_id, **_kwargs: (
            target if worker_id == target.id else None
        )
    )
    uow.handoff_proposals = SimpleNamespace(
        get=lambda proposal_id, _workspace_id, **_kwargs: (
            proposal if proposal_id == proposal.id else None
        )
    )
    resolver = StaticRuntimeResolver({target.id: _runtime(target)})
    service = HandoffRecoveryService(lambda: uow, runtime_resolver=resolver)

    inspection = service.inspect_unknown(transition=transition, now=NOW)
    assert HandoffRecoveryStrategy.RETRY_SAME_TARGET in service.available_strategies(
        transition=transition,
        inspection=inspection,
    )

    result = service.apply_decision(
        transition=transition,
        decision=_decision(
            transition,
            HandoffRecoveryStrategy.RETRY_SAME_TARGET,
            inspection.evidence_refs,
        ),
        now=NOW,
    )

    assert result.phase is HandoffPhase.CREATE_TARGET_EXECUTION
    assert transition.logical_attempt == 2
    assert transition.target_worker_id == target.id
    assert transition.target_runtime_snapshot["worker_id"] == str(target.id)


def test_confirm_source_stopped_requires_success_evidence_and_advances_without_replay() -> (
    None
):
    transition, base_uow = _base_transition()
    transition.start(now=NOW)
    transition.record_checkpoint(sequence=3, facts={})
    transition.phase = HandoffPhase.STOP_SOURCE
    transition.writer_state = HandoffWriterState.SOURCE_STOP_REQUESTED
    source_session = replace(base_uow.session, status=ExecutionSessionStatus.CLOSED)
    operation_id = gateway_operation_id(
        transition.source_execution_id,
        GatewayOperationType.STOP_SESSION,
    )
    transition.block(
        failure_code="stop_result_unknown",
        now=NOW,
        status=HandoffTransitionStatus.BLOCKED_UNKNOWN,
        blocked_operation_id=operation_id,
    )
    uow = RecoveryUnitOfWork(
        transition,
        source_session=source_session,
        source_execution=base_uow.execution,
    )
    inspector = StaticGatewayInspector(HandoffInspectionOutcome.SUCCEEDED)
    service = HandoffRecoveryService(lambda: uow, gateway=inspector)

    inspection = service.inspect_unknown(transition=transition, now=NOW)

    assert (
        HandoffRecoveryStrategy.CONFIRM_SOURCE_STOPPED
        in service.available_strategies(
            transition=transition,
            inspection=inspection,
        )
    )
    result = service.apply_decision(
        transition=transition,
        decision=_decision(
            transition,
            HandoffRecoveryStrategy.CONFIRM_SOURCE_STOPPED,
            inspection.evidence_refs,
        ),
        now=NOW,
    )

    assert result.phase is HandoffPhase.FREEZE_SOURCE_WORKSPACE
    assert result.writer_state is HandoffWriterState.WRITER_NONE
    assert uow.handoff_transitions.transition.source_stop_sequence == 3
    assert inspector.calls == [operation_id, operation_id]


def test_recovery_rejects_evidence_from_another_inspection() -> None:
    transition, uow = _blocked_transition(
        HandoffPhase.CHECKPOINT_SOURCE,
        handoff_step_operation_id(
            UUID("40000000-0000-0000-0000-000000000001"),
            HandoffPhase.CHECKPOINT_SOURCE,
            logical_attempt=1,
            action="checkpoint",
        ),
        status=HandoffTransitionStatus.BLOCKED,
    )
    service = HandoffRecoveryService(lambda: uow)
    inspection = service.inspect_unknown(transition=transition, now=NOW)
    decision = _decision(
        transition,
        HandoffRecoveryStrategy.RETRY_CURRENT_PHASE,
        ("handoff-inspection://unrelated",),
    )

    with pytest.raises(HandoffRecoveryError):
        service.apply_decision(
            transition=transition,
            decision=decision,
            now=NOW,
        )

    assert inspection.transition_id == transition.id


def test_recover_command_applies_inspected_strategy_and_emits_a_bounded_event() -> None:
    transition, _ = _base_transition()
    transition.phase = HandoffPhase.CHECKPOINT_SOURCE
    transition.writer_state = HandoffWriterState.SOURCE_ACTIVE
    transition.status = HandoffTransitionStatus.RUNNING
    transition.version += 1
    operation_id = handoff_step_operation_id(
        transition.id,
        HandoffPhase.CHECKPOINT_SOURCE,
        logical_attempt=transition.logical_attempt,
        action="checkpoint",
    )
    transition.block(
        failure_code="adapter_rejected",
        now=NOW,
        status=HandoffTransitionStatus.BLOCKED,
        blocked_operation_id=operation_id,
    )
    operation = GatewayOperation.create(
        id=uuid4(),
        operation_id=operation_id,
        workspace_id=WORKSPACE_ID,
        task_id=uuid4(),
        execution_id=transition.source_execution_id,
        session_id=transition.source_session_id,
        operation_type=GatewayOperationType.HANDOFF_CHECKPOINT,
        module_instance_id=None,
        config_version=None,
        lease_id=uuid4(),
        fencing_token=1,
        now=NOW,
    )
    operation.start(now=NOW)
    operation.fail(failure_code="adapter_rejected", now=NOW)
    uow = RecoveryUnitOfWork(transition, operation=operation)
    service = HandoffRecoveryService(lambda: uow)
    inspection = service.inspect_unknown(transition=transition, now=NOW)
    envelope = CommandEnvelope(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        command_type="RecoverWorkerHandoff",
        target_type="handoff_transition",
        actor_type="user",
        actor_id="user:test",
        target_id=transition.id,
        expected_version=transition.version,
        payload={
            "transition_id": str(transition.id),
            "expected_transition_version": transition.version,
            "strategy": HandoffRecoveryStrategy.RETRY_CURRENT_PHASE.value,
            "reason": "retry after the adapter reported a bounded failure",
            "evidence_refs": list(inspection.evidence_refs),
        },
        idempotency_key="recover-1",
        correlation_id=uuid4(),
    )

    handler = HandoffCommandHandler(
        lambda: NOW,
        candidate_service=SimpleNamespace(),
        recovery_service=service,
    )
    result = handler.handle(uow, envelope)

    assert result.aggregate_id == transition.id
    assert result.data["transition"]["logical_attempt"] == 2
    assert [event.event_type for event in uow.events.events] == ["HandoffRecovered"]
    assert uow.events.events[0].payload["evidence_refs"] == list(
        inspection.evidence_refs
    )
    assert operation.status is GatewayOperationStatus.FAILED


def test_recover_command_cannot_accept_a_client_declared_source_channel() -> None:
    transition, uow = _blocked_transition(
        HandoffPhase.CHECKPOINT_SOURCE,
        handoff_step_operation_id(
            UUID("40000000-0000-0000-0000-000000000001"),
            HandoffPhase.CHECKPOINT_SOURCE,
            logical_attempt=1,
            action="checkpoint",
        ),
        status=HandoffTransitionStatus.BLOCKED,
    )
    service = HandoffRecoveryService(lambda: uow)
    inspection = service.inspect_unknown(transition=transition, now=NOW)
    envelope = CommandEnvelope(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        command_type="RecoverWorkerHandoff",
        target_type="handoff_transition",
        actor_type="user",
        actor_id="user:test",
        target_id=transition.id,
        expected_version=transition.version,
        payload={
            "transition_id": str(transition.id),
            "expected_transition_version": transition.version,
            "strategy": HandoffRecoveryStrategy.RETRY_CURRENT_PHASE.value,
            "reason": "invalid channel injection",
            "evidence_refs": list(inspection.evidence_refs),
            "source_channel": "main_agent",
        },
        idempotency_key="recover-channel-1",
        correlation_id=uuid4(),
    )

    handler = HandoffCommandHandler(
        lambda: NOW,
        candidate_service=SimpleNamespace(),
        recovery_service=service,
    )
    with pytest.raises(HandoffCommandError) as error:
        handler.handle(uow, envelope)

    assert error.value.code == "validation_error"
    assert not uow.handoff_recovery_decisions.decisions


def _native_recovery_fixture():
    legacy_uow, item, _source, target, _execution, _session = _fixture()
    prepared = _handler(legacy_uow).handle(legacy_uow, _prepare_envelope(item, target))
    original = legacy_uow.handoff_proposals.proposals[prepared.aggregate_id]
    old = original.payload
    profile = ProfileRef(workspace_id=WORKSPACE_ID, profile_id=uuid4(), revision=1)
    values = {
        field.name: getattr(old, field.name)
        for field in fields(NativeHandoffProposalPayload)
        if hasattr(old, field.name)
    }
    values.update(
        proposal_schema_version=2,
        source_worker_item_version=1,
        source_run_id=uuid4(),
        source_authority_id=uuid4(),
        source_binding_version=1,
        source_profile_ref=profile,
        source_binding_target_type="worker_item",
        source_binding_target_id=item.id,
        work_context_id=uuid4(),
        work_context_revision_id=uuid4(),
        work_context_hash="c" * 64,
        work_context_version=1,
        target_assignee=AssigneeRef("profile", profile.profile_id),
        target_profile_ref=profile,
        impact_summary={"worker_item_context_version": 1},
    )
    proposal = HandoffProposal.create(
        id=original.id,
        payload=NativeHandoffProposalPayload(**values),
        created_by_type="user",
        created_by_id="unit",
        source_channel="web",
        created_at=original.created_at,
        expires_at=original.expires_at,
    )
    proposal.mark_confirmed(now=original.created_at)
    transition = HandoffTransition.create(
        id=uuid4(),
        proposal=proposal,
        confirmed_by_type="user",
        confirmed_by_id="unit",
        confirmed_at=original.created_at,
    )
    operation_id = handoff_step_operation_id(
        transition.id, transition.phase, logical_attempt=1, action="checkpoint"
    )
    transition.block(
        failure_code="adapter_rejected", now=NOW, blocked_operation_id=operation_id
    )
    operation = GatewayOperation.create(
        id=uuid4(),
        operation_id=operation_id,
        workspace_id=WORKSPACE_ID,
        task_id=uuid4(),
        execution_id=transition.source_execution_id,
        session_id=transition.source_session_id,
        operation_type=GatewayOperationType.HANDOFF_CHECKPOINT,
        module_instance_id=None,
        config_version=None,
        lease_id=uuid4(),
        fencing_token=1,
        now=NOW,
    )
    operation.start(now=NOW)
    operation.fail(failure_code="adapter_rejected", now=NOW)
    observation = HandoffOperationInspection(
        operation_id=operation_id,
        operation_type=operation.operation_type,
        outcome=HandoffInspectionOutcome.FAILED,
        execution_id=None,
        session_id=None,
        accepted_sequence=None,
        context_binding=None,
        observed_at=NOW,
        evidence_refs=("inspection:failed",),
    )
    inspection = HandoffRecoveryInspection(
        transition_id=transition.id,
        transition_version=transition.version,
        logical_attempt=1,
        phase=transition.phase,
        writer_state=transition.writer_state,
        blocked_operation_id=operation_id,
        operation_inspections=(observation,),
        workspace_inspection=None,
        source_session_state="active",
        target_session_state=None,
        target_manifest_state=None,
        safe_conclusions=(HandoffSafeConclusion.OPERATION_FAILED,),
        evidence_refs=("inspection:failed",),
        inspected_at=NOW,
    )
    uow = RecoveryUnitOfWork(transition, operation=operation)
    return transition, uow, inspection, operation


@pytest.mark.parametrize("boundary", ["inspect", "persist", "apply", "apply_in_uow"])
def test_legacy_recovery_rejects_native_before_inspection_receipts_or_writes(boundary):
    transition, uow, inspection, operation = _native_recovery_fixture()
    gateway = StaticGatewayInspector(HandoffInspectionOutcome.FAILED)
    service = HandoffRecoveryService(lambda: uow, gateway=gateway)
    decision = _decision(
        transition,
        HandoffRecoveryStrategy.RETRY_CURRENT_PHASE,
        inspection.evidence_refs,
    )
    saves = []
    receipts = []
    uow.gateway_operations.save = lambda *args: saves.append(args)
    get_receipt = uow.handoff_recovery_decisions.get
    uow.handoff_recovery_decisions.get = lambda *args, **kwargs: (
        receipts.append(args),
        get_receipt(*args, **kwargs),
    )[1]
    previous = operation.version
    with pytest.raises(HandoffRecoveryError):
        if boundary == "inspect":
            service.inspect_unknown(transition=transition, now=NOW)
        elif boundary == "persist":
            service.persist_operation_inspection(
                transition=transition, inspection=inspection
            )
        elif boundary == "apply":
            service.apply_decision(transition=transition, decision=decision, now=NOW)
        else:
            service.apply_decision_in_uow(
                uow,
                transition=transition,
                decision=decision,
                inspection=inspection,
                now=NOW,
            )
    assert gateway.calls == saves == receipts == []
    assert operation.version == previous
    assert (
        not uow.handoff_recovery_decisions.decisions
        and uow.handoff_transitions.recovery_saves == 0
    )


def test_legacy_recovery_advertises_no_native_strategy():
    transition, uow, inspection, _ = _native_recovery_fixture()
    service = HandoffRecoveryService(lambda: uow)
    assert (
        service.available_strategies(transition=transition, inspection=inspection) == ()
    )


@pytest.mark.parametrize("receipt", [False, True])
def test_legacy_recovery_rechecks_persisted_variant_before_decision_lookup(receipt):
    native, uow, inspection, _ = _native_recovery_fixture()
    legacy, _ = _base_transition()
    legacy.id = native.id
    legacy.version = native.version
    legacy.phase = native.phase
    legacy.status = native.status
    legacy.writer_state = native.writer_state
    legacy.blocked_operation_id = native.blocked_operation_id
    decision = _decision(
        legacy, HandoffRecoveryStrategy.RETRY_CURRENT_PHASE, inspection.evidence_refs
    )
    if receipt:
        uow.handoff_recovery_decisions.decisions[decision.id] = decision
    reads = []
    get_receipt = uow.handoff_recovery_decisions.get
    uow.handoff_recovery_decisions.get = lambda *args, **kwargs: (
        reads.append(args),
        get_receipt(*args, **kwargs),
    )[1]
    service = HandoffRecoveryService(lambda: uow)
    with pytest.raises(HandoffRecoveryError):
        service.apply_decision_in_uow(
            uow, transition=legacy, decision=decision, inspection=inspection, now=NOW
        )
    assert reads == []
    assert len(uow.handoff_recovery_decisions.decisions) == int(receipt)
    assert uow.handoff_transitions.recovery_saves == 0
