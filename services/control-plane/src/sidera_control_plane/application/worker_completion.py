from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import UUID

from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.gateway import ExecutionEvent
from sidera_control_plane.domain.modules import (
    ModuleInstance,
    WorkerAdapterCapabilityProfile,
)


class CompletionPolicyError(DomainError):
    code = "completion_policy_invalid"


class CompletionProtocolError(DomainError):
    code = "completion_protocol_invalid"


class ExecutionCompletionPolicy(StrEnum):
    EXPLICIT = "explicit"
    INITIAL_TURN = "initial_turn"


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    outcome: Literal["succeeded", "failed"]
    event_type: str
    sequence: int
    input_operation_id: UUID | None
    policy_version: int
    failure_code: str | None


_TERMINAL_OUTCOMES: dict[
    str, Literal["succeeded", "failed", "canceled"]
] = {
    "execution.succeeded": "succeeded",
    "execution.completed": "succeeded",
    "execution.failed": "failed",
    "execution.canceled": "canceled",
    "execution.cancelled": "canceled",
    "session.succeeded": "succeeded",
    "session.completed": "succeeded",
    "session.failed": "failed",
    "session.canceled": "canceled",
    "session.cancelled": "canceled",
}
_FAILURE_CODES = frozenset(
    {
        "process_exited",
        "worker_session_failed",
        "protocol_failed",
        "provider_failed",
    }
)


def resolve_completion_policy(
    instance: ModuleInstance,
) -> ExecutionCompletionPolicy:
    namespaced_key = f"{instance.config_namespace}.completion_policy"
    raw_value = instance.config.get(namespaced_key, instance.config.get("completion_policy"))
    if raw_value is None:
        return ExecutionCompletionPolicy.EXPLICIT
    if isinstance(raw_value, ExecutionCompletionPolicy):
        return raw_value
    if not isinstance(raw_value, str):
        raise CompletionPolicyError(
            "completion_policy must be explicit or initial_turn"
        )
    try:
        return ExecutionCompletionPolicy(raw_value.strip().lower())
    except ValueError as exc:
        raise CompletionPolicyError(
            "completion_policy must be explicit or initial_turn"
        ) from exc


def trusted_terminal_outcome(
    event_type: str,
) -> Literal["succeeded", "failed", "canceled"] | None:
    return _TERMINAL_OUTCOMES.get(event_type.strip().lower())


def decide_completion(
    *,
    policy: ExecutionCompletionPolicy,
    capability_snapshot: WorkerAdapterCapabilityProfile,
    events: Sequence[ExecutionEvent],
    initial_operation_id: UUID,
    initial_operation_succeeded: bool,
) -> CompletionDecision | None:
    """Replay trusted terminal facts and return the earliest valid decision."""

    if not isinstance(policy, ExecutionCompletionPolicy):
        raise CompletionPolicyError("completion policy is not recognized")
    ordered_events = sorted(events, key=lambda event: event.sequence)
    for event in ordered_events:
        terminal_outcome = trusted_terminal_outcome(event.event_type)
        if terminal_outcome is not None:
            if terminal_outcome == "canceled":
                continue
            return CompletionDecision(
                outcome=terminal_outcome,
                event_type=event.event_type,
                sequence=event.sequence,
                input_operation_id=None,
                policy_version=1,
                failure_code=(
                    _failure_code(event.payload)
                    if terminal_outcome == "failed"
                    else None
                ),
            )

        if (
            policy is not ExecutionCompletionPolicy.INITIAL_TURN
            or event.event_type.strip().lower() != "turn.completed"
            or not capability_snapshot.supports_turn_completion_version(1)
        ):
            continue

        operation_id = _parse_turn_completion(event)
        if not initial_operation_succeeded or operation_id != initial_operation_id:
            continue
        return CompletionDecision(
            outcome="succeeded",
            event_type=event.event_type,
            sequence=event.sequence,
            input_operation_id=operation_id,
            policy_version=1,
            failure_code=None,
        )
    return None


def _parse_turn_completion(event: ExecutionEvent) -> UUID:
    payload = event.payload
    schema_version = payload.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise CompletionProtocolError("turn.completed schema version is unsupported")

    operation_value = payload.get("operation_id")
    if not isinstance(operation_value, str):
        raise CompletionProtocolError("turn.completed operation id is invalid")
    try:
        operation_id = UUID(operation_value)
    except (AttributeError, ValueError) as exc:
        raise CompletionProtocolError("turn.completed operation id is invalid") from exc

    if payload.get("stop_reason") != "end_turn":
        raise CompletionProtocolError("turn.completed stop reason is invalid")
    return operation_id


def _failure_code(payload: object) -> str:
    if isinstance(payload, Mapping):
        candidate = payload.get("failure_code")
        if isinstance(candidate, str) and candidate in _FAILURE_CODES:
            return candidate
    return "worker_session_failed"
