from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.code_result_capture import (
    capture_operation_for_execution,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSession,
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.gateway import (
    GatewayOperationStatus,
    GatewayOperationType,
    NativeInputIdentity,
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffContextBundle,
    HandoffInputAction,
    HandoffInputClassification,
    HandoffInputDisposition,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffTransition,
    HandoffTransitionStatus,
)
from sidera_control_plane.domain.inputs import (
    ExecutionInput,
    ExecutionInputStatus,
    input_operation_id,
)

_HANDOFF_INPUT_NAMESPACE = NAMESPACE_URL
_TRANSFERRED_REASON = "handoff_transferred"


class HandoffInputTransferError(DomainError):
    """A handoff input cannot be classified or transferred safely."""

    code = "handoff_input_invalid"

    def __init__(
        self,
        message: str,
        *,
        code: str = "handoff_input_invalid",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.code = code


class _InputDecision(StrEnum):
    PRESERVE = "preserve"
    TRANSFER = "transfer"
    EXCLUDE = "exclude"
    BLOCK = "block"


class HandoffInputTransferService:
    """Classify source inputs and derive safe target queue identities."""

    def __init__(
        self, uow_factory: Callable[[], ControlPlaneUnitOfWork]
    ) -> None:
        self._uow_factory = uow_factory

    def classify_source_inputs(
        self,
        *,
        transition: HandoffTransition,
        now: datetime,
    ) -> HandoffInputClassification:
        """Classify source inputs from a locked, bounded source observation."""

        _aware(now, "handoff input classification timestamp")
        with self._uow_factory() as uow:
            current = self._load_transition(uow, transition)
            return self._classify_in_uow(uow, current, now=now)

    def transfer_safe_inputs(
        self,
        *,
        transition: HandoffTransition,
        target_execution: Execution,
        target_session_id: UUID,
        classification: HandoffInputClassification,
        now: datetime,
    ) -> tuple[ExecutionInput, ...]:
        """Derive one queued target input for every proven-safe source input."""

        _aware(now, "handoff input transfer timestamp")
        if not isinstance(classification, HandoffInputClassification):
            raise HandoffInputTransferError(
                "handoff input classification is invalid",
                code="handoff_input_classification_invalid",
            )
        if not isinstance(target_session_id, UUID):
            raise HandoffInputTransferError(
                "handoff target Session identity is invalid",
                code="handoff_input_target_invalid",
            )

        with self._uow_factory() as uow:
            current = self._load_transition(uow, transition)
            self._validate_transfer_transition(current, target_execution, target_session_id)
            stored_target = uow.executions.get(
                target_execution.id,
                current.workspace_id,
                for_update=True,
            )
            if stored_target is None:
                raise HandoffInputTransferError(
                    "handoff target Execution was not found",
                    code="handoff_input_target_execution_missing",
                )
            self._validate_target_execution(current, stored_target)
            target_session = uow.sessions.get_any(
                target_session_id,
                current.workspace_id,
                for_update=True,
            )
            if target_session is None:
                raise HandoffInputTransferError(
                    "handoff target Session was not found",
                    code="handoff_input_target_session_missing",
                )
            self._validate_target_session(
                current,
                target_session,
                target_session_id=target_session_id,
            )
            bundle = self._load_bundle(uow, current)

            fresh = self._classify_in_uow(uow, current, now=now)
            if _classification_categories(classification) != _classification_categories(
                fresh
            ):
                raise HandoffInputTransferError(
                    "handoff input classification is stale",
                    code="handoff_input_classification_stale",
                )
            if _bundle_categories(bundle) != _classification_bundle_categories(fresh):
                raise HandoffInputTransferError(
                    "handoff input classification does not match the immutable Bundle",
                    code="handoff_input_classification_stale",
                )
            if fresh.unknown_input_ids:
                raise HandoffInputTransferError(
                    "handoff input classification contains unknown inputs",
                    code="handoff_input_unknown",
                )

            transferred: list[ExecutionInput] = []
            for source_input_id in fresh.transferable_input_ids:
                source = uow.execution_inputs.get(
                    source_input_id,
                    current.workspace_id,
                    for_update=True,
                )
                if source is None:
                    raise HandoffInputTransferError(
                        "handoff source input disappeared",
                        code="handoff_input_source_missing",
                    )
                target = self._transfer_one(
                    uow,
                    current,
                    source,
                    target_execution=stored_target,
                    target_session_id=target_session_id,
                    now=now,
                )
                transferred.append(target)
            return tuple(transferred)

    @staticmethod
    def _load_transition(
        uow: ControlPlaneUnitOfWork, transition: HandoffTransition
    ) -> HandoffTransition:
        transition_id = transition.id
        workspace_id = transition.workspace_id
        if not isinstance(transition_id, UUID) or not isinstance(workspace_id, UUID):
            raise HandoffInputTransferError(
                "handoff transition identity is invalid",
                code="handoff_input_transition_invalid",
            )
        current = uow.handoff_transitions.get(
            transition_id,
            workspace_id,
            for_update=True,
        )
        if current is None:
            raise HandoffInputTransferError(
                "handoff transition was not found",
                code="handoff_input_transition_missing",
            )
        if current.id != transition_id or current.workspace_id != workspace_id:
            raise HandoffInputTransferError(
                "handoff transition identity changed",
                code="handoff_input_transition_stale",
            )
        supplied_version = getattr(transition, "version", None)
        if supplied_version is not None and supplied_version != current.version:
            raise HandoffInputTransferError(
                "handoff transition version is stale",
                code="handoff_input_transition_stale",
            )
        return current

    def _classify_in_uow(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: HandoffTransition,
        *,
        now: datetime,
    ) -> HandoffInputClassification:
        source_execution_id = _uuid_attr(transition, "source_execution_id")
        source_session_id = _uuid_attr(transition, "source_session_id")
        worker_item_id = _uuid_attr(transition, "worker_item_id")
        workspace_id = _uuid_attr(transition, "workspace_id")
        cutoff_sequence = _non_negative_attr(
            transition, "source_input_cutoff_sequence"
        )
        self._validate_running_transition(transition)
        source_execution = uow.executions.get(
            source_execution_id,
            workspace_id,
            for_update=True,
        )
        if source_execution is None:
            raise HandoffInputTransferError(
                "handoff source Execution was not found",
                code="handoff_input_source_execution_missing",
            )
        if (
            getattr(source_execution, "workspace_id", None) != workspace_id
            or getattr(source_execution, "id", None) != source_execution_id
            or getattr(source_execution, "worker_item_id", None) != worker_item_id
        ):
            raise HandoffInputTransferError(
                "handoff source Execution facts changed",
                code="handoff_input_source_execution_stale",
            )
        source_session = uow.sessions.get_any(
            source_session_id,
            workspace_id,
            for_update=True,
        )
        if source_session is None:
            raise HandoffInputTransferError(
                "handoff source Session was not found",
                code="handoff_input_source_session_missing",
            )
        last_sequence = getattr(source_session, "last_sequence", None)
        if (
            getattr(source_session, "workspace_id", None) != workspace_id
            or getattr(source_session, "id", None) != source_session_id
            or getattr(source_session, "execution_id", None) != source_execution_id
            or not isinstance(last_sequence, int)
            or isinstance(last_sequence, bool)
            or last_sequence < cutoff_sequence
        ):
            raise HandoffInputTransferError(
                "handoff source Session sequence is stale",
                code="handoff_input_source_sequence_stale",
            )

        source_inputs = uow.execution_inputs.list_for_session(
            source_session_id,
            workspace_id,
            before=_input_cutoff_time(transition, now),
            for_update=True,
        )
        derived_inputs = uow.execution_inputs.list_for_handoff_transition(
            transition.id,
            workspace_id,
            for_update=True,
        )
        derived_by_source: dict[UUID, ExecutionInput] = {}
        for item in derived_inputs:
            source_input_id = item.source_input_id
            if source_input_id is None:
                raise HandoffInputTransferError(
                    "handoff derived input is missing source lineage",
                    code="handoff_input_target_conflict",
                )
            if source_input_id in derived_by_source:
                raise HandoffInputTransferError(
                    "handoff source input has multiple derived inputs",
                    code="handoff_input_target_conflict",
                )
            derived_by_source[source_input_id] = item

        dispositions: list[HandoffInputDisposition] = []
        transferable: list[UUID] = []
        excluded: list[UUID] = []
        unknown: list[UUID] = []
        for item in source_inputs:
            if (
                item.workspace_id != workspace_id
                or item.execution_id != source_execution_id
                or item.session_id != source_session_id
            ):
                disposition = _disposition(
                    item,
                    action=HandoffInputAction.BLOCK,
                    reason_code="source_input_identity_changed",
                )
                unknown.append(item.id)
                dispositions.append(disposition)
                continue
            decision = self._decide_input(
                uow,
                transition,
                item,
                derived_by_source.get(item.id),
                last_sequence=last_sequence,
            )
            disposition = _disposition(
                item,
                action=_action_for_decision(decision),
                reason_code=_reason_for_decision(decision, item),
                accepted_sequence=_accepted_sequence_for_decision(uow, decision, item),
                operation_id=_operation_id_for_decision(decision, item),
            )
            dispositions.append(disposition)
            if disposition.action is HandoffInputAction.TRANSFER_QUEUED:
                transferable.append(item.id)
            elif disposition.action is HandoffInputAction.EXCLUDE:
                excluded.append(item.id)
            elif disposition.action is HandoffInputAction.BLOCK:
                unknown.append(item.id)

        return HandoffInputClassification(
            source_session_id=source_session_id,
            cutoff_sequence=cutoff_sequence,
            dispositions=tuple(dispositions),
            transferable_input_ids=tuple(transferable),
            excluded_input_ids=tuple(excluded),
            unknown_input_ids=tuple(unknown),
        )

    @staticmethod
    def _decide_input(
        uow: ControlPlaneUnitOfWork,
        transition: HandoffTransition,
        item: ExecutionInput,
        derived: ExecutionInput | None,
        *,
        last_sequence: int,
    ) -> _InputDecision:
        status = item.status
        if not isinstance(status, ExecutionInputStatus):
            try:
                status = ExecutionInputStatus(status)
            except (TypeError, ValueError):
                return _InputDecision.BLOCK

        if (
            status is ExecutionInputStatus.SUPERSEDED
            and item.superseded_reason == _TRANSFERRED_REASON
        ):
            return (
                _InputDecision.TRANSFER
                if _derived_input_matches(derived, transition, item)
                else _InputDecision.BLOCK
            )
        if status is ExecutionInputStatus.DELIVERED:
            operation = _operation_for(uow, item)
            accepted_sequence = _accepted_sequence(operation)
            return (
                _InputDecision.PRESERVE
                if _delivered_operation_matches(
                    operation,
                    item,
                    accepted_sequence,
                    last_sequence=last_sequence,
                )
                and _native_delivery_matches(uow, operation, item, transition)
                else _InputDecision.BLOCK
            )
        if status is ExecutionInputStatus.QUEUED:
            operation = _operation_for(uow, item)
            if operation is None and item.operation_id is None:
                return _InputDecision.TRANSFER
            if (
                operation is not None
                and _operation_matches_input(operation, item)
                and _operation_status(operation) is GatewayOperationStatus.FAILED
            ):
                return _InputDecision.EXCLUDE
            return _InputDecision.BLOCK
        if status is ExecutionInputStatus.FAILED:
            return _InputDecision.EXCLUDE
        if status is ExecutionInputStatus.CANCELLED:
            return _InputDecision.EXCLUDE
        if status is ExecutionInputStatus.SUPERSEDED:
            return _InputDecision.EXCLUDE
        return _InputDecision.BLOCK

    @staticmethod
    def _validate_transfer_transition(
        transition: HandoffTransition,
        target_execution: Execution,
        target_session_id: UUID,
    ) -> None:
        if _status_value(getattr(transition, "status", None)) != HandoffTransitionStatus.RUNNING.value:
            raise HandoffInputTransferError(
                "handoff transition is not running",
                code="handoff_input_transition_blocked",
            )
        if _status_value(getattr(transition, "phase", None)) != HandoffPhase.TRANSFER_SAFE_INPUTS.value:
            raise HandoffInputTransferError(
                "handoff transition is not transferring safe inputs",
                code="handoff_input_phase_mismatch",
            )
        target_execution_id = getattr(transition, "target_execution_id", None)
        if (
            not isinstance(target_execution_id, UUID)
            or getattr(target_execution, "id", None) != target_execution_id
            or getattr(transition, "target_session_id", None) != target_session_id
        ):
            raise HandoffInputTransferError(
                "handoff target identity does not match the transition",
                code="handoff_input_target_mismatch",
            )

    @staticmethod
    def _validate_target_execution(
        transition: HandoffTransition, target: Execution
    ) -> None:
        if (
            target.workspace_id != transition.workspace_id
            or target.worker_item_id != transition.worker_item_id
            or getattr(target, "handoff_transition_id", None)
            != transition.id
            or _status_value(getattr(target, "status", None))
            != ExecutionStatus.RUNNING.value
        ):
            raise HandoffInputTransferError(
                "handoff target Execution facts are not active",
                code="handoff_input_target_execution_stale",
            )

    @staticmethod
    def _validate_target_session(
        transition: HandoffTransition,
        session: ExecutionSession,
        *,
        target_session_id: UUID,
    ) -> None:
        if (
            session.id != target_session_id
            or session.workspace_id != transition.workspace_id
            or session.execution_id != transition.target_execution_id
            or _status_value(getattr(session, "status", None))
            != ExecutionSessionStatus.ACTIVE.value
        ):
            raise HandoffInputTransferError(
                "handoff target Session facts are not active",
                code="handoff_input_target_session_stale",
            )

    @staticmethod
    def _load_bundle(
        uow: ControlPlaneUnitOfWork, transition: HandoffTransition
    ) -> HandoffContextBundle:
        bundle_id = getattr(transition, "handoff_context_bundle_id", None)
        if not isinstance(bundle_id, UUID):
            raise HandoffInputTransferError(
                "handoff context Bundle is missing",
                code="handoff_input_bundle_missing",
            )
        bundle = uow.handoff_context_bundles.get(
            bundle_id,
            transition.workspace_id,
            for_update=True,
        )
        if bundle is None:
            raise HandoffInputTransferError(
                "handoff context Bundle was not found",
                code="handoff_input_bundle_missing",
            )
        if (
            bundle.handoff_transition_id != transition.id
            or bundle.target_execution_id != transition.target_execution_id
            or bundle.worker_item_id != transition.worker_item_id
        ):
            raise HandoffInputTransferError(
                "handoff context Bundle identity changed",
                code="handoff_input_bundle_stale",
            )
        return bundle

    @staticmethod
    def _transfer_one(
        uow: ControlPlaneUnitOfWork,
        transition: HandoffTransition,
        source: ExecutionInput,
        *,
        target_execution: Execution,
        target_session_id: UUID,
        now: datetime,
    ) -> ExecutionInput:
        terminal_operation = uow.results.get_operation(
            capture_operation_for_execution(
                uow,
                workspace_id=transition.workspace_id,
                execution_id=target_execution.id,
            ),
            transition.workspace_id,
            for_update=True,
        )
        terminal_payload = getattr(terminal_operation, "payload", None)
        if (
            isinstance(terminal_payload, Mapping)
            and terminal_payload.get("terminal_observed") is True
        ):
            raise HandoffInputTransferError(
                "handoff target execution is finalizing",
                code="handoff_input_target_finalizing",
            )
        key = f"handoff:{transition.id}:{source.id}"
        existing = uow.execution_inputs.find_by_client_idempotency_key(
            target_session_id,
            transition.workspace_id,
            key,
            for_update=True,
        )
        if existing is not None:
            if not _derived_input_matches(existing, transition, source):
                raise HandoffInputTransferError(
                    "handoff target input lineage conflicts",
                    code="handoff_input_target_conflict",
                )
            if existing.status is not ExecutionInputStatus.QUEUED:
                raise HandoffInputTransferError(
                    "handoff target input is not queued",
                    code="handoff_input_target_conflict",
                )
            if source.status is ExecutionInputStatus.QUEUED:
                previous_version = source.version
                source.supersede(now=now, reason=_TRANSFERRED_REASON)
                uow.execution_inputs.save(source, previous_version)
            elif not (
                source.status is ExecutionInputStatus.SUPERSEDED
                and source.superseded_reason == _TRANSFERRED_REASON
            ):
                raise HandoffInputTransferError(
                    "handoff source input cannot be reconciled",
                    code="handoff_input_source_conflict",
                )
            return existing

        if (
            source.status is not ExecutionInputStatus.QUEUED
            or source.operation_id is not None
            or _operation_for(uow, source) is not None
        ):
            raise HandoffInputTransferError(
                "handoff source input is no longer safe",
                code="handoff_input_classification_stale",
            )
        get_pending = getattr(uow.execution_inputs, "get_pending_for_session", None)
        if callable(get_pending):
            pending = get_pending(
                target_session_id,
                transition.workspace_id,
                for_update=True,
            )
            if pending is not None:
                raise HandoffInputTransferError(
                    "handoff target Session already has a pending input",
                    code="handoff_input_target_pending_conflict",
                )
        target = ExecutionInput.accept(
            id=_derived_input_id(transition.id, source.id),
            workspace_id=transition.workspace_id,
            execution_id=target_execution.id,
            session_id=target_session_id,
            client_idempotency_key=key,
            text=source.text,
            attachment_refs=source.attachment_refs,
            now=now,
            source_command_id=source.source_command_id,
            source_input_id=source.id,
            handoff_transition_id=transition.id,
        )
        target.queue()
        uow.execution_inputs.add(target)
        uow.flush()
        previous_version = source.version
        source.supersede(now=now, reason=_TRANSFERRED_REASON)
        uow.execution_inputs.save(source, previous_version)
        return target

    @staticmethod
    def _validate_running_transition(transition: HandoffTransition) -> None:
        if _status_value(getattr(transition, "status", None)) != HandoffTransitionStatus.RUNNING.value:
            raise HandoffInputTransferError(
                "handoff transition is not running",
                code="handoff_input_transition_blocked",
            )


def _disposition(
    item: ExecutionInput,
    *,
    action: HandoffInputAction,
    reason_code: str,
    accepted_sequence: int | None = None,
    operation_id: UUID | None = None,
) -> HandoffInputDisposition:
    return HandoffInputDisposition(
        source_input_id=item.id,
        source_status=item.status.value,
        action=action,
        reason_code=reason_code,
        accepted_sequence=accepted_sequence,
        operation_id=operation_id,
    )


def _action_for_decision(decision: _InputDecision) -> HandoffInputAction:
    return {
        _InputDecision.PRESERVE: HandoffInputAction.PRESERVE_DELIVERED,
        _InputDecision.TRANSFER: HandoffInputAction.TRANSFER_QUEUED,
        _InputDecision.EXCLUDE: HandoffInputAction.EXCLUDE,
        _InputDecision.BLOCK: HandoffInputAction.BLOCK,
    }[decision]


def _reason_for_decision(decision: _InputDecision, item: ExecutionInput) -> str:
    if decision is _InputDecision.PRESERVE:
        return "delivered_exact_sequence"
    if decision is _InputDecision.TRANSFER:
        return (
            _TRANSFERRED_REASON
            if item.status is ExecutionInputStatus.SUPERSEDED
            else "queued_never_claimed"
        )
    if decision is _InputDecision.EXCLUDE:
        if item.status is ExecutionInputStatus.FAILED:
            return "input_failed"
        if item.status is ExecutionInputStatus.CANCELLED:
            return "input_cancelled"
        if item.status is ExecutionInputStatus.SUPERSEDED:
            return "input_superseded"
        return "delivery_failed"
    if item.status is ExecutionInputStatus.ACCEPTED:
        return "input_not_queued"
    if item.status is ExecutionInputStatus.DISPATCHING:
        return "input_in_flight"
    if item.status is ExecutionInputStatus.BLOCKED:
        return "input_unknown"
    if item.status is ExecutionInputStatus.DELIVERED:
        return "delivered_operation_unproven"
    return "input_state_unknown"


def _accepted_sequence_for_decision(
    uow: ControlPlaneUnitOfWork,
    decision: _InputDecision,
    item: ExecutionInput,
) -> int | None:
    if decision is not _InputDecision.PRESERVE:
        return None
    return _accepted_sequence(_operation_for(uow, item))


def _operation_id_for_decision(
    decision: _InputDecision, item: ExecutionInput
) -> UUID | None:
    return input_operation_id(item.id) if decision is _InputDecision.PRESERVE else None


def _operation_for(uow: ControlPlaneUnitOfWork, item: ExecutionInput) -> object | None:
    return uow.gateway_operations.get_by_operation_id(
        input_operation_id(item.id),
        item.workspace_id,
        for_update=True,
    )


def _delivered_operation_matches(
    operation: object | None,
    item: ExecutionInput,
    accepted_sequence: int | None,
    *,
    last_sequence: int,
) -> bool:
    return (
        operation is not None
        and _operation_matches_input(operation, item)
        and _operation_status(operation) is GatewayOperationStatus.SUCCEEDED
        and accepted_sequence is not None
        and accepted_sequence <= last_sequence
    )


def _operation_matches_input(operation: object, item: ExecutionInput) -> bool:
    return (
        getattr(operation, "operation_id", None) == input_operation_id(item.id)
        and _enum_value(
            getattr(operation, "operation_type", None), GatewayOperationType
        )
        is GatewayOperationType.SEND_INPUT
        and getattr(operation, "workspace_id", None) == item.workspace_id
        and getattr(operation, "execution_id", None) == item.execution_id
        and getattr(operation, "session_id", None) == item.session_id
    )


def _operation_status(operation: object) -> GatewayOperationStatus | None:
    value = getattr(operation, "status", None)
    if isinstance(value, GatewayOperationStatus):
        return value
    try:
        return GatewayOperationStatus(value)
    except (TypeError, ValueError):
        return None


def _accepted_sequence(operation: object | None) -> int | None:
    if operation is None or _operation_status(operation) is not GatewayOperationStatus.SUCCEEDED:
        return None
    result_ref = getattr(operation, "result_ref", None)
    if isinstance(result_ref, str) and result_ref.startswith(
        ("native-input:", "native-input-delivery:")
    ):
        try:
            return NativeInputIdentity.delivery_sequence_from_result_ref(result_ref)
        except DomainError:
            return None
    if not isinstance(result_ref, str) or not result_ref.startswith("sequence:"):
        return None
    suffix = result_ref.removeprefix("sequence:")
    if not suffix or any(char not in "0123456789" for char in suffix):
        return None
    try:
        sequence = int(suffix)
    except ValueError:
        return None
    return sequence if sequence >= 0 else None


def _native_delivery_matches(uow, operation, item, transition):
    repository = getattr(uow, "execution_authorities", None)
    link = (
        None
        if repository is None
        else repository.get_link(item.workspace_id, item.execution_id)
    )
    result_ref = getattr(operation, "result_ref", None)
    if repository is None or link is None:
        return not isinstance(result_ref, str) or not result_ref.startswith(
            ("native-input:", "native-input-delivery:")
        )
    if link.run_id is None or item.operation_id != input_operation_id(item.id):
        return False
    try:
        identity = NativeInputIdentity.from_result_ref(result_ref)
        sequence = NativeInputIdentity.delivery_sequence_from_result_ref(result_ref)
    except DomainError:
        return False
    authority = repository.get(link.authority_id, workspace_id=item.workspace_id)
    session = uow.sessions.get_any(item.session_id, item.workspace_id)
    return (
        identity
        == NativeInputIdentity(
            item.workspace_id,
            item.id,
            item.execution_id,
            item.session_id,
            link.run_id,
            item.operation_id,
            link.authority_id,
            link.binding_version,
        )
        and authority is not None
        and authority.execution_id == item.execution_id
        and authority.profile_ref == link.profile_ref
        and authority.binding_version == link.binding_version
        and session is not None
        and session.execution_id == item.execution_id
        and session.adapter_session_id == f"agent-run:{link.run_id}"
        and session.id == uuid5(NAMESPACE_URL, f"sidera.native.session:{link.run_id}")
        and sequence is not None
    )


def _derived_input_matches(
    derived: ExecutionInput | None,
    transition: HandoffTransition,
    source: ExecutionInput,
) -> bool:
    return (
        derived is not None
        and derived.id == _derived_input_id(transition.id, source.id)
        and derived.workspace_id == source.workspace_id
        and derived.execution_id == transition.target_execution_id
        and derived.session_id == transition.target_session_id
        and derived.source_input_id == source.id
        and derived.handoff_transition_id == transition.id
        and derived.client_idempotency_key
        == f"handoff:{transition.id}:{source.id}"
        and derived.source_command_id == source.source_command_id
        and derived.text == source.text
        and derived.attachment_refs == source.attachment_refs
        and _status_value(derived.status) == ExecutionInputStatus.QUEUED.value
    )


def _derived_input_id(transition_id: UUID, source_input_id: UUID) -> UUID:
    return uuid5(
        _HANDOFF_INPUT_NAMESPACE,
        f"sidera.handoff-input:{transition_id}:{source_input_id}",
    )


def _classification_categories(
    classification: HandoffInputClassification,
) -> tuple[tuple[UUID, ...], tuple[UUID, ...], tuple[UUID, ...], tuple[UUID, ...]]:
    delivered = tuple(
        item.source_input_id
        for item in classification.dispositions
        if item.action is HandoffInputAction.PRESERVE_DELIVERED
    )
    return (
        delivered,
        classification.transferable_input_ids,
        classification.excluded_input_ids,
        classification.unknown_input_ids,
    )


def _classification_bundle_categories(
    classification: HandoffInputClassification,
) -> tuple[tuple[UUID, ...], tuple[UUID, ...], tuple[UUID, ...]]:
    categories = _classification_categories(classification)
    return categories[:3]


def _bundle_categories(
    bundle: HandoffContextBundle,
) -> tuple[tuple[UUID, ...], tuple[UUID, ...], tuple[UUID, ...]]:
    return (
        tuple(bundle.delivered_input_refs),
        tuple(bundle.transferable_input_refs),
        tuple(bundle.excluded_input_refs),
    )


def _enum_value[EnumValue: StrEnum](
    value: object, enum_type: type[EnumValue]
) -> EnumValue | None:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return None


def _status_value(value: object) -> str | None:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, str):
        return value
    return getattr(value, "value", None)


def _uuid_attr(source: object, name: str) -> UUID:
    value = getattr(source, name, None)
    if not isinstance(value, UUID):
        raise HandoffInputTransferError(
            f"handoff {name} is invalid",
            code="handoff_input_transition_invalid",
        )
    return value


def _non_negative_attr(source: object, name: str) -> int:
    value = getattr(source, name, None)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HandoffInputTransferError(
            f"handoff {name} is invalid",
            code="handoff_input_transition_invalid",
        )
    return value


def _aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HandoffInputTransferError(
            f"{label} must be timezone-aware",
            code="handoff_input_timestamp_invalid",
        )


def _input_cutoff_time(transition: HandoffTransition, now: datetime) -> datetime:
    confirmed_at = getattr(transition, "confirmed_at", None)
    if (
        not isinstance(confirmed_at, datetime)
        or confirmed_at.tzinfo is None
        or confirmed_at.utcoffset() is None
    ):
        return now
    return min(confirmed_at, now)
