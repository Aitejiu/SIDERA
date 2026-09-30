from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from sidera_control_plane.domain.gateway import (
    DeliveryOutcome,
    DeliveryResult,
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffCheckpointEvidence,
    HandoffInspectionOutcome,
    HandoffOperationInspection,
    SessionContextBinding,
)

from .state import DemoStateStore, OperationRecord


class DemoHandoffError(RuntimeError):
    """A demo handoff operation cannot be represented safely."""


def capture_checkpoint(
    state: DemoStateStore,
    session: Mapping[str, object],
    *,
    transition_id: UUID,
    source_input_cutoff_sequence: int,
    operation_id: UUID,
) -> HandoffCheckpointEvidence:
    if source_input_cutoff_sequence < 0:
        raise DemoHandoffError("handoff_cutoff_invalid")
    session_id = _session_id(session)
    key = f"handoff/checkpoint/{session_id}"
    request = {
        "session_id": str(session_id),
        "transition_id": str(transition_id),
        "cutoff_sequence": source_input_cutoff_sequence,
    }
    record = state.reserve(key, operation_id, request)
    if record.status == "applied":
        return _checkpoint_from_record(record)
    if record.status != "pending":
        raise DemoHandoffError("handoff_checkpoint_previous_failure")
    if _has_event(state, session_id, "session.stopped"):
        raise DemoHandoffError("session_stopped")
    accepted_sequence = _last_sequence(state, session_id)
    if accepted_sequence < source_input_cutoff_sequence:
        raise DemoHandoffError("handoff_checkpoint_sequence_stale")
    context = _context_identity(session)
    workspace_digest = _text(session, "workspace_digest")
    current_step = _current_step(state, session_id)
    evidence = HandoffCheckpointEvidence(
        operation_id=operation_id,
        transition_id=transition_id,
        source_session_id=session_id,
        accepted_sequence=accepted_sequence,
        workspace_digest=workspace_digest,
        context_package_id=context[0],
        context_package_hash=context[1],
        context_version=context[2],
        progress_summary_ref=(
            f"demo-handoff://progress/{session_id}/{accepted_sequence}"
        ),
        artifact_refs=(
            f"demo-handoff://artifact-state/{session_id}/{workspace_digest}",
        ),
        evidence_refs=(
            f"demo-handoff://step/{session_id}/{current_step}",
            f"demo-handoff://workspace/{session_id}/{workspace_digest}",
        ),
        quiescent=True,
        produced_at=datetime.now(UTC),
    )
    state.settle(
        key,
        operation_id,
        "applied",
        {"outcome": "succeeded", "evidence": evidence.to_mapping()},
    )
    return evidence


def inspect_operation(
    state: DemoStateStore,
    session: Mapping[str, object],
    *,
    operation_id: UUID,
    context_binding: SessionContextBinding | None,
) -> HandoffOperationInspection:
    session_id = _session_id(session)
    record = state.find_operation(operation_id)
    if record is None:
        return HandoffOperationInspection(
            operation_id=operation_id,
            operation_type=GatewayOperationType.HANDOFF_CHECKPOINT,
            outcome=HandoffInspectionOutcome.ABSENT,
            execution_id=_execution_id(session),
            session_id=session_id,
            accepted_sequence=None,
            context_binding=context_binding,
            observed_at=datetime.now(UTC),
            evidence_refs=(),
        )
    operation_type = GatewayOperationType.HANDOFF_CHECKPOINT
    raw_type = record.result.get("operation_type")
    if isinstance(raw_type, str):
        try:
            operation_type = GatewayOperationType(raw_type)
        except ValueError:
            pass
    outcome = {
        "pending": HandoffInspectionOutcome.IN_PROGRESS,
        "applied": HandoffInspectionOutcome.SUCCEEDED,
        "rejected": HandoffInspectionOutcome.FAILED,
        "unknown": HandoffInspectionOutcome.UNKNOWN,
    }[record.status]
    evidence = record.result.get("evidence")
    evidence_mapping = evidence if isinstance(evidence, Mapping) else record.result
    accepted_sequence = _optional_int(evidence_mapping.get("accepted_sequence"))
    evidence_refs = _refs(evidence_mapping.get("evidence_refs"))
    return HandoffOperationInspection(
        operation_id=operation_id,
        operation_type=operation_type,
        outcome=outcome,
        execution_id=_execution_id(session),
        session_id=session_id,
        accepted_sequence=accepted_sequence,
        context_binding=context_binding,
        observed_at=datetime.now(UTC),
        evidence_refs=evidence_refs,
    )


def enable_writer(
    state: DemoStateStore,
    session: Mapping[str, object],
    *,
    fencing_token: int,
    operation_id: UUID,
    context_binding: SessionContextBinding | None,
) -> DeliveryResult:
    session_id = _session_id(session)
    if not bool(session.get("handoff_held")):
        return _blocked(operation_id, session_id, "handoff_context_binding_missing")
    if context_binding is None:
        return _blocked(operation_id, session_id, "handoff_context_binding_missing")
    expected_token = _optional_int(session.get("fencing_token"))
    if expected_token is None or fencing_token != expected_token:
        return _blocked(operation_id, session_id, "handoff_fencing_mismatch")
    key = f"handoff/writer/{session_id}"
    record = state.reserve(
        key,
        operation_id,
        {
            "session_id": str(session_id),
            "fencing_token": fencing_token,
            "context_version": context_binding.context_version,
        },
    )
    if record.status == "applied":
        return _delivery_from_record(record, operation_id, session_id)
    if record.status != "pending":
        return _blocked(operation_id, session_id, "handoff_writer_previous_failure")
    if _has_event(state, session_id, "session.stopped"):
        result = _blocked(operation_id, session_id, "session_stopped")
        state.settle(
            key,
            operation_id,
            "rejected",
            {
                "outcome": result.outcome.value,
                "failure_code": result.failure_code,
                "execution_session_id": str(session_id),
            },
        )
        return result
    event = state.append_event(
        session_id,
        operation_id,
        "handoff.writer_enabled",
        {
            "context_version": context_binding.context_version,
            "fencing_token": fencing_token,
            "simulation": True,
        },
    )
    state.settle(
        key,
        operation_id,
        "applied",
        {
            "outcome": DeliveryOutcome.DELIVERED.value,
            "accepted_sequence": event.sequence,
            "execution_session_id": str(session_id),
            "operation_type": GatewayOperationType.HANDOFF_ENABLE_WRITER.value,
        },
    )
    return DeliveryResult(
        operation_id=operation_id,
        outcome=DeliveryOutcome.DELIVERED,
        accepted_sequence=event.sequence,
        failure_code=None,
        execution_session_id=session_id,
    )


def writer_enabled(state: DemoStateStore, session_id: UUID) -> bool:
    record = state.read(f"handoff/writer/{session_id}")
    return record is not None and record.get("status") == "applied"


def checkpoint_for(
    state: DemoStateStore, session_id: UUID
) -> HandoffCheckpointEvidence | None:
    raw = state.read(f"handoff/checkpoint/{session_id}")
    if raw is None:
        return None
    record = OperationRecord.from_dict(raw)
    if record.status != "applied":
        return None
    return _checkpoint_from_record(record)


def stopped_delivery(
    state: DemoStateStore,
    session: Mapping[str, object],
    *,
    operation_id: UUID,
) -> DeliveryResult | None:
    checkpoint = checkpoint_for(state, _session_id(session))
    if checkpoint is None:
        return None
    expected = gateway_operation_id(
        _execution_id(session), GatewayOperationType.STOP_SESSION
    )
    if operation_id != expected:
        raise DemoHandoffError("handoff_stop_identity_mismatch")
    if not _has_event(state, _session_id(session), "session.stopped"):
        state.append_event(
            _session_id(session),
            operation_id,
            "session.stopped",
            {"handoff": True, "simulation": True},
        )
    return DeliveryResult(
        operation_id=operation_id,
        outcome=DeliveryOutcome.DELIVERED,
        accepted_sequence=checkpoint.accepted_sequence,
        failure_code=None,
        execution_session_id=_session_id(session),
    )


def context_binding_from_session(
    session: Mapping[str, object],
) -> SessionContextBinding | None:
    raw = session.get("context_binding")
    if not isinstance(raw, Mapping):
        return None
    try:
        return SessionContextBinding(
            operation_id=UUID(str(raw["operation_id"])),
            execution_session_id=UUID(str(raw["execution_session_id"])),
            consumer_execution_id=UUID(str(raw["consumer_execution_id"])),
            package_id=UUID(str(raw["package_id"])),
            package_hash=str(raw["package_hash"]),
            context_version=int(str(raw["context_version"])),
            acknowledged_at=datetime.fromisoformat(str(raw["acknowledged_at"])),
            schema_version=int(str(raw.get("schema_version", 1))),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DemoHandoffError("handoff_context_binding_corrupt") from exc


def is_handoff_target(session: Mapping[str, object]) -> bool:
    return bool(session.get("handoff_held"))


def _checkpoint_from_record(record: OperationRecord) -> HandoffCheckpointEvidence:
    raw = record.result.get("evidence")
    if not isinstance(raw, Mapping):
        raise DemoHandoffError("handoff_checkpoint_record_corrupt")
    try:
        return HandoffCheckpointEvidence(
            operation_id=UUID(str(raw["operation_id"])),
            transition_id=UUID(str(raw["transition_id"])),
            source_session_id=UUID(str(raw["source_session_id"])),
            accepted_sequence=int(str(raw["accepted_sequence"])),
            workspace_digest=str(raw["workspace_digest"]),
            context_package_id=UUID(str(raw["context_package_id"])),
            context_package_hash=str(raw["context_package_hash"]),
            context_version=int(str(raw["context_version"])),
            progress_summary_ref=str(raw["progress_summary_ref"]),
            artifact_refs=_refs(raw.get("artifact_refs")),
            evidence_refs=_refs(raw.get("evidence_refs")),
            quiescent=bool(raw["quiescent"]),
            produced_at=datetime.fromisoformat(str(raw["produced_at"])),
            schema_version=int(str(raw.get("schema_version", 1))),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DemoHandoffError("handoff_checkpoint_record_corrupt") from exc


def _session_id(session: Mapping[str, object]) -> UUID:
    try:
        return UUID(str(session["execution_session_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise DemoHandoffError("handoff_session_identity_missing") from exc


def _execution_id(session: Mapping[str, object]) -> UUID:
    try:
        return UUID(str(session["execution_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise DemoHandoffError("handoff_execution_identity_missing") from exc


def _context_identity(session: Mapping[str, object]) -> tuple[UUID, str, int]:
    raw = session.get("context_identity")
    if not isinstance(raw, Mapping):
        raise DemoHandoffError("handoff_context_missing")
    try:
        return (
            UUID(str(raw["package_id"])),
            str(raw["package_hash"]),
            int(str(raw["context_version"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DemoHandoffError("handoff_context_corrupt") from exc


def _text(session: Mapping[str, object], key: str) -> str:
    value = session.get(key)
    if not isinstance(value, str) or not value:
        raise DemoHandoffError(f"handoff_{key}_missing")
    return value


def _last_sequence(state: DemoStateStore, session_id: UUID) -> int:
    return max((event.sequence for event in state.events_after(session_id, 0)), default=0)


def _current_step(state: DemoStateStore, session_id: UUID) -> str:
    events = state.events_after(session_id, 0)
    for event in reversed(events):
        step = event.payload.get("step_key")
        if isinstance(step, str) and step:
            return step
    return "session-started"


def _has_event(state: DemoStateStore, session_id: UUID, event_type: str) -> bool:
    return any(event.event_type == event_type for event in state.events_after(session_id, 0))


def _optional_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _refs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise DemoHandoffError("handoff_refs_corrupt")
    return tuple(str(item) for item in value)


def _blocked(operation_id: UUID, session_id: UUID, code: str) -> DeliveryResult:
    return DeliveryResult(
        operation_id=operation_id,
        outcome=DeliveryOutcome.BLOCKED,
        accepted_sequence=None,
        failure_code=code,
        execution_session_id=session_id,
    )


def _delivery_from_record(
    record: OperationRecord, operation_id: UUID, session_id: UUID
) -> DeliveryResult:
    return DeliveryResult(
        operation_id=operation_id,
        outcome=DeliveryOutcome(str(record.result.get("outcome", "unknown"))),
        accepted_sequence=_optional_int(record.result.get("accepted_sequence")),
        failure_code=(
            str(record.result["failure_code"])
            if record.result.get("failure_code") is not None
            else None
        ),
        execution_session_id=session_id,
    )
