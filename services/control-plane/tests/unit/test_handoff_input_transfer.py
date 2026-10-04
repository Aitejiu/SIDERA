from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.handoff_input_transfer import (
    HandoffInputTransferError,
    HandoffInputTransferService,
)
from sidera_control_plane.domain.gateway import (
    GatewayOperationStatus,
    GatewayOperationType,
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffInputAction,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffTransitionStatus,
)
from sidera_control_plane.domain.inputs import (
    ExecutionInput,
    ExecutionInputStatus,
    input_operation_id,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")


class InputRepository:
    def __init__(self, items: tuple[ExecutionInput, ...] = ()) -> None:
        self.items = {item.id: item for item in items}
        self.added: list[ExecutionInput] = []
        self.saved: list[ExecutionInput] = []

    def get(self, item_id: UUID, workspace_id: UUID, **_kwargs: object) -> ExecutionInput | None:
        item = self.items.get(item_id)
        return item if item is not None and item.workspace_id == workspace_id else None

    def list_for_session(
        self,
        session_id: UUID,
        workspace_id: UUID,
        *,
        before: datetime | None = None,
        **_kwargs: object,
    ) -> tuple[ExecutionInput, ...]:
        values = (
            item
            for item in self.items.values()
            if item.session_id == session_id
            and item.workspace_id == workspace_id
            and (before is None or item.created_at <= before)
        )
        return tuple(sorted(values, key=lambda item: (item.created_at, item.id)))

    def list_for_handoff_transition(
        self, transition_id: UUID, workspace_id: UUID, **_kwargs: object
    ) -> tuple[ExecutionInput, ...]:
        values = (
            item
            for item in self.items.values()
            if item.handoff_transition_id == transition_id
            and item.workspace_id == workspace_id
        )
        return tuple(sorted(values, key=lambda item: (item.created_at, item.id)))

    def find_by_client_idempotency_key(
        self,
        session_id: UUID,
        workspace_id: UUID,
        client_idempotency_key: str,
        **_kwargs: object,
    ) -> ExecutionInput | None:
        return next(
            (
                item
                for item in self.items.values()
                if item.session_id == session_id
                and item.workspace_id == workspace_id
                and item.client_idempotency_key == client_idempotency_key
            ),
            None,
        )

    def add(self, item: ExecutionInput) -> None:
        if item.id in self.items:
            raise AssertionError("duplicate input identity")
        self.items[item.id] = item
        self.added.append(item)

    def save(self, item: ExecutionInput, _previous_version: int) -> None:
        self.items[item.id] = item
        self.saved.append(item)


class ExecutionRepository:
    def __init__(self, *executions: Any) -> None:
        self.items = {item.id: item for item in executions}

    def get(self, execution_id: UUID, workspace_id: UUID, **_kwargs: object) -> Any | None:
        item = self.items.get(execution_id)
        return item if item is not None and item.workspace_id == workspace_id else None


class SessionRepository:
    def __init__(self, *sessions: Any) -> None:
        self.items = {item.id: item for item in sessions}

    def get_any(self, session_id: UUID, workspace_id: UUID, **_kwargs: object) -> Any | None:
        item = self.items.get(session_id)
        return item if item is not None and item.workspace_id == workspace_id else None


class GatewayRepository:
    def __init__(self, operations: tuple[Any, ...] = ()) -> None:
        self.operations = {item.operation_id: item for item in operations}

    def get_by_operation_id(
        self, operation_id: UUID, workspace_id: UUID, **_kwargs: object
    ) -> Any | None:
        item = self.operations.get(operation_id)
        return item if item is not None and item.workspace_id == workspace_id else None


class ResultRepository:
    def get_operation(
        self, _operation_id: UUID, _workspace_id: UUID, **_kwargs: object
    ) -> None:
        return None


class TransitionRepository:
    def __init__(self, transition: Any) -> None:
        self.transition = transition

    def get(self, transition_id: UUID, workspace_id: UUID, **_kwargs: object) -> Any | None:
        if (
            self.transition.id == transition_id
            and self.transition.workspace_id == workspace_id
        ):
            return self.transition
        return None


class UnitOfWork:
    def __init__(
        self,
        inputs: InputRepository,
        executions: ExecutionRepository,
        sessions: SessionRepository,
        gateway_operations: GatewayRepository,
        transition: Any,
        *,
        bundles: dict[UUID, Any] | None = None,
    ) -> None:
        self.execution_inputs = inputs
        self.executions = executions
        self.sessions = sessions
        self.results = ResultRepository()
        self.execution_authorities = SimpleNamespace(get_link=lambda *args: None)
        self.gateway_operations = gateway_operations
        self.handoff_transitions = TransitionRepository(transition)
        self.handoff_context_bundles = SimpleNamespace(
            get=lambda bundle_id, workspace_id, **_kwargs: (
                (bundles or {}).get(bundle_id)
                if (bundles or {}).get(bundle_id) is not None
                and (bundles or {}).get(bundle_id).workspace_id == workspace_id
                else None
            )
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def flush(self) -> None:
        return None


def _input(
    session_id: UUID,
    execution_id: UUID,
    *,
    status: ExecutionInputStatus = ExecutionInputStatus.QUEUED,
) -> ExecutionInput:
    item = ExecutionInput.accept(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        execution_id=execution_id,
        session_id=session_id,
        client_idempotency_key=f"source-{uuid4()}",
        text="continue the existing task",
        attachment_refs=("artifact://handoff",),
        now=NOW,
    )
    if status is not ExecutionInputStatus.ACCEPTED:
        item.queue()
    if status is ExecutionInputStatus.DISPATCHING:
        item.begin_dispatch(operation_id=input_operation_id(item.id))
    elif status is ExecutionInputStatus.DELIVERED:
        item.begin_dispatch(operation_id=input_operation_id(item.id))
        item.deliver()
    elif status is ExecutionInputStatus.FAILED:
        item.begin_dispatch(operation_id=input_operation_id(item.id))
        item.fail(failure_code="adapter_rejected")
    elif status is ExecutionInputStatus.CANCELLED:
        item.cancel(now=NOW)
    elif status is ExecutionInputStatus.BLOCKED:
        item.begin_dispatch(operation_id=input_operation_id(item.id))
        item.block_unknown(failure_code="adapter_unknown")
    elif status is ExecutionInputStatus.SUPERSEDED:
        item.supersede(now=NOW, reason="manual_supersede")
    return item


def _operation(
    item: ExecutionInput,
    *,
    status: GatewayOperationStatus,
    execution_id: UUID,
    session_id: UUID,
    result_ref: str | None = None,
) -> Any:
    return SimpleNamespace(
        operation_id=input_operation_id(item.id),
        workspace_id=WORKSPACE_ID,
        execution_id=execution_id,
        session_id=session_id,
        operation_type=GatewayOperationType.SEND_INPUT,
        status=status,
        result_ref=result_ref,
    )


def _transition(
    *,
    source_execution_id: UUID,
    source_session_id: UUID,
    target_execution_id: UUID | None = None,
    target_session_id: UUID | None = None,
    bundle_id: UUID | None = None,
) -> Any:
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        worker_item_id=uuid4(),
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
        source_input_cutoff_sequence=3,
        target_execution_id=target_execution_id,
        target_session_id=target_session_id,
        handoff_context_bundle_id=bundle_id,
        status=HandoffTransitionStatus.RUNNING,
        phase=HandoffPhase.TRANSFER_SAFE_INPUTS,
        logical_attempt=1,
    )


def _uow(
    source_execution: Any,
    target_execution: Any | None,
    source_session: Any,
    target_session: Any | None,
    inputs: tuple[ExecutionInput, ...],
    transition: Any,
    operations: tuple[Any, ...] = (),
    *,
    bundles: dict[UUID, Any] | None = None,
) -> UnitOfWork:
    return UnitOfWork(
        InputRepository(inputs),
        ExecutionRepository(*(item for item in (source_execution, target_execution) if item)),
        SessionRepository(*(item for item in (source_session, target_session) if item)),
        GatewayRepository(operations),
        transition,
        bundles=bundles,
    )


def test_classify_source_inputs_applies_exact_state_matrix() -> None:
    source_execution_id = uuid4()
    source_session_id = uuid4()
    transition = _transition(
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
    )
    delivered = _input(
        source_session_id,
        source_execution_id,
        status=ExecutionInputStatus.DELIVERED,
    )
    queued = _input(source_session_id, source_execution_id)
    dispatching = _input(
        source_session_id,
        source_execution_id,
        status=ExecutionInputStatus.DISPATCHING,
    )
    failed = _input(
        source_session_id,
        source_execution_id,
        status=ExecutionInputStatus.FAILED,
    )
    cancelled = _input(
        source_session_id,
        source_execution_id,
        status=ExecutionInputStatus.CANCELLED,
    )
    source_execution = SimpleNamespace(
        id=source_execution_id,
        workspace_id=WORKSPACE_ID,
        worker_item_id=transition.worker_item_id,
    )
    source_session = SimpleNamespace(
        id=source_session_id,
        workspace_id=WORKSPACE_ID,
        execution_id=source_execution_id,
        last_sequence=4,
    )
    transition.phase = HandoffPhase.ASSEMBLE_BUNDLE
    operations = (
        _operation(
            delivered,
            status=GatewayOperationStatus.SUCCEEDED,
            execution_id=source_execution_id,
            session_id=source_session_id,
            result_ref="sequence:4",
        ),
    )
    uow = _uow(
        source_execution,
        None,
        source_session,
        None,
        (delivered, queued, dispatching, failed, cancelled),
        transition,
        operations,
    )
    service = HandoffInputTransferService(lambda: uow)

    classification = service.classify_source_inputs(transition=transition, now=NOW)

    by_id = {item.source_input_id: item for item in classification.dispositions}
    assert by_id[delivered.id].action is HandoffInputAction.PRESERVE_DELIVERED
    assert by_id[delivered.id].accepted_sequence == 4
    assert by_id[queued.id].action is HandoffInputAction.TRANSFER_QUEUED
    assert by_id[dispatching.id].action is HandoffInputAction.BLOCK
    assert by_id[failed.id].action is HandoffInputAction.EXCLUDE
    assert by_id[cancelled.id].action is HandoffInputAction.EXCLUDE
    assert classification.transferable_input_ids == (queued.id,)
    assert classification.unknown_input_ids == (dispatching.id,)


def test_classify_queued_input_with_existing_operation_is_not_transferable() -> None:
    source_execution_id = uuid4()
    source_session_id = uuid4()
    transition = _transition(
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
    )
    queued = _input(source_session_id, source_execution_id)
    source_execution = SimpleNamespace(
        id=source_execution_id,
        workspace_id=WORKSPACE_ID,
        worker_item_id=transition.worker_item_id,
    )
    source_session = SimpleNamespace(
        id=source_session_id,
        workspace_id=WORKSPACE_ID,
        execution_id=source_execution_id,
        last_sequence=3,
    )
    operation = _operation(
        queued,
        status=GatewayOperationStatus.RUNNING,
        execution_id=source_execution_id,
        session_id=source_session_id,
    )
    uow = _uow(
        source_execution,
        None,
        source_session,
        None,
        (queued,),
        transition,
        (operation,),
    )

    classification = HandoffInputTransferService(lambda: uow).classify_source_inputs(
        transition=transition,
        now=NOW,
    )

    assert classification.unknown_input_ids == (queued.id,)
    assert classification.transferable_input_ids == ()


def test_classify_accepted_blocked_and_superseded_inputs_without_replay() -> None:
    source_execution_id = uuid4()
    source_session_id = uuid4()
    transition = _transition(
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
    )
    accepted = _input(
        source_session_id,
        source_execution_id,
        status=ExecutionInputStatus.ACCEPTED,
    )
    blocked = _input(
        source_session_id,
        source_execution_id,
        status=ExecutionInputStatus.BLOCKED,
    )
    superseded = _input(
        source_session_id,
        source_execution_id,
        status=ExecutionInputStatus.SUPERSEDED,
    )
    source_execution = SimpleNamespace(
        id=source_execution_id,
        workspace_id=WORKSPACE_ID,
        worker_item_id=transition.worker_item_id,
    )
    source_session = SimpleNamespace(
        id=source_session_id,
        workspace_id=WORKSPACE_ID,
        execution_id=source_execution_id,
        last_sequence=3,
    )
    uow = _uow(
        source_execution,
        None,
        source_session,
        None,
        (accepted, blocked, superseded),
        transition,
    )

    classification = HandoffInputTransferService(lambda: uow).classify_source_inputs(
        transition=transition,
        now=NOW,
    )

    by_id = {item.source_input_id: item for item in classification.dispositions}
    assert by_id[accepted.id].action is HandoffInputAction.BLOCK
    assert by_id[blocked.id].action is HandoffInputAction.BLOCK
    assert by_id[superseded.id].action is HandoffInputAction.EXCLUDE
    assert classification.transferable_input_ids == ()
    assert set(classification.unknown_input_ids) == {accepted.id, blocked.id}


def test_delivered_input_requires_exact_sequence_operation_evidence() -> None:
    source_execution_id = uuid4()
    source_session_id = uuid4()
    transition = _transition(
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
    )
    delivered = _input(
        source_session_id,
        source_execution_id,
        status=ExecutionInputStatus.DELIVERED,
    )
    source_execution = SimpleNamespace(
        id=source_execution_id,
        workspace_id=WORKSPACE_ID,
        worker_item_id=transition.worker_item_id,
    )
    source_session = SimpleNamespace(
        id=source_session_id,
        workspace_id=WORKSPACE_ID,
        execution_id=source_execution_id,
        last_sequence=3,
    )
    operation = _operation(
        delivered,
        status=GatewayOperationStatus.SUCCEEDED,
        execution_id=source_execution_id,
        session_id=source_session_id,
        result_ref="not-a-sequence",
    )
    uow = _uow(
        source_execution,
        None,
        source_session,
        None,
        (delivered,),
        transition,
        (operation,),
    )

    classification = HandoffInputTransferService(lambda: uow).classify_source_inputs(
        transition=transition,
        now=NOW,
    )

    assert classification.unknown_input_ids == (delivered.id,)
    assert classification.dispositions[0].action is HandoffInputAction.BLOCK


def test_transfer_safe_inputs_is_idempotent_and_preserves_lineage() -> None:
    source_execution_id = uuid4()
    source_session_id = uuid4()
    target_execution_id = uuid4()
    target_session_id = uuid4()
    transition = _transition(
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
        target_execution_id=target_execution_id,
        target_session_id=target_session_id,
    )
    queued = _input(source_session_id, source_execution_id)
    source_execution = SimpleNamespace(
        id=source_execution_id,
        workspace_id=WORKSPACE_ID,
        worker_item_id=transition.worker_item_id,
    )
    target_execution = SimpleNamespace(
        id=target_execution_id,
        workspace_id=WORKSPACE_ID,
        worker_item_id=transition.worker_item_id,
        status=SimpleNamespace(value="running"),
        handoff_transition_id=uuid4(),
    )
    source_session = SimpleNamespace(
        id=source_session_id,
        workspace_id=WORKSPACE_ID,
        execution_id=source_execution_id,
        last_sequence=3,
    )
    target_session = SimpleNamespace(
        id=target_session_id,
        workspace_id=WORKSPACE_ID,
        execution_id=target_execution_id,
        status=SimpleNamespace(value="active"),
    )
    bundle_id = uuid4()
    transition.handoff_context_bundle_id = bundle_id
    target_execution.worker_item_id = transition.worker_item_id
    target_execution.handoff_transition_id = transition.id
    bundle = SimpleNamespace(
        id=bundle_id,
        workspace_id=WORKSPACE_ID,
        worker_item_id=transition.worker_item_id,
        handoff_transition_id=transition.id,
        target_execution_id=target_execution_id,
        delivered_input_refs=(),
        transferable_input_refs=(queued.id,),
        excluded_input_refs=(),
    )
    uow = _uow(
        source_execution,
        target_execution,
        source_session,
        target_session,
        (queued,),
        transition,
        bundles={bundle_id: bundle},
    )
    service = HandoffInputTransferService(lambda: uow)
    classification = service.classify_source_inputs(transition=transition, now=NOW)

    first = service.transfer_safe_inputs(
        transition=transition,
        target_execution=target_execution,
        target_session_id=target_session_id,
        classification=classification,
        now=NOW,
    )
    second = service.transfer_safe_inputs(
        transition=transition,
        target_execution=target_execution,
        target_session_id=target_session_id,
        classification=classification,
        now=NOW,
    )

    assert len(first) == 1
    assert second == first
    target = first[0]
    assert target.status is ExecutionInputStatus.QUEUED
    assert target.execution_id == target_execution_id
    assert target.session_id == target_session_id
    assert target.source_input_id == queued.id
    assert target.handoff_transition_id == transition.id
    assert target.source_command_id == queued.source_command_id
    assert target.client_idempotency_key == f"handoff:{transition.id}:{queued.id}"
    assert uow.execution_inputs.items[queued.id].status is ExecutionInputStatus.SUPERSEDED
    assert len(uow.execution_inputs.added) == 1


def test_transfer_reclassifies_against_bundle_before_mutation() -> None:
    source_execution_id = uuid4()
    source_session_id = uuid4()
    target_execution_id = uuid4()
    target_session_id = uuid4()
    transition = _transition(
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
        target_execution_id=target_execution_id,
        target_session_id=target_session_id,
    )
    queued = _input(source_session_id, source_execution_id)
    source_execution = SimpleNamespace(
        id=source_execution_id,
        workspace_id=WORKSPACE_ID,
        worker_item_id=transition.worker_item_id,
    )
    target_execution = SimpleNamespace(
        id=target_execution_id,
        workspace_id=WORKSPACE_ID,
        worker_item_id=transition.worker_item_id,
        status=SimpleNamespace(value="running"),
        handoff_transition_id=uuid4(),
    )
    source_session = SimpleNamespace(
        id=source_session_id,
        workspace_id=WORKSPACE_ID,
        execution_id=source_execution_id,
        last_sequence=3,
    )
    target_session = SimpleNamespace(
        id=target_session_id,
        workspace_id=WORKSPACE_ID,
        execution_id=target_execution_id,
        status=SimpleNamespace(value="active"),
    )
    bundle_id = uuid4()
    transition.handoff_context_bundle_id = bundle_id
    target_execution.worker_item_id = transition.worker_item_id
    target_execution.handoff_transition_id = transition.id
    bundle = SimpleNamespace(
        id=bundle_id,
        workspace_id=WORKSPACE_ID,
        worker_item_id=transition.worker_item_id,
        handoff_transition_id=transition.id,
        target_execution_id=target_execution_id,
        delivered_input_refs=(),
        transferable_input_refs=(queued.id,),
        excluded_input_refs=(),
    )
    uow = _uow(
        source_execution,
        target_execution,
        source_session,
        target_session,
        (queued,),
        transition,
        bundles={bundle_id: bundle},
    )
    service = HandoffInputTransferService(lambda: uow)
    classification = service.classify_source_inputs(transition=transition, now=NOW)
    queued.cancel(now=NOW)

    with pytest.raises(HandoffInputTransferError) as error:
        service.transfer_safe_inputs(
            transition=transition,
            target_execution=target_execution,
            target_session_id=target_session_id,
            classification=classification,
            now=NOW,
        )

    assert error.value.code == "handoff_input_classification_stale"
    assert uow.execution_inputs.added == []
