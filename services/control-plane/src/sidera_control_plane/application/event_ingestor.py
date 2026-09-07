import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.result_source_fence import (
    discover_result_source_fence,
    lock_result_source_fence,
)
from sidera_control_plane.application.result_verification import (
    record_verification_observation,
)
from sidera_control_plane.domain.artifacts import (
    Artifact,
    ArtifactCandidate,
    EvidenceClaim,
)
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.gateway import AdapterEvent, ExecutionEvent
from sidera_control_plane.domain.lease import AssignmentLease

MAX_EVENT_PAYLOAD_BYTES = 16 * 1024
MAX_EVENT_STRING_BYTES = 4 * 1024
_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|token|secret|api[_-]?key|authorization|credential)"
)


class EventIngestionError(RuntimeError):
    """An Adapter event cannot be safely normalized into a Session stream."""


class EventSequenceGap(EventIngestionError):
    """The Adapter skipped one or more Session event sequences."""

    def __init__(self, expected_sequence: int, received_sequence: int) -> None:
        super().__init__(
            f"event sequence gap: expected {expected_sequence}, "
            f"received {received_sequence}"
        )
        self.expected_sequence = expected_sequence
        self.received_sequence = received_sequence


class EventSequenceConflict(EventIngestionError):
    """A sequence was already persisted with different event content."""


class ArtifactRegistrationError(RuntimeError):
    """An Artifact candidate cannot be safely associated with the Task."""


@dataclass(frozen=True, slots=True)
class EventIngestionResult:
    event: ExecutionEvent | None
    ingested: bool
    duplicate: bool


@dataclass(frozen=True, slots=True)
class EventIngestionReport:
    events: tuple[ExecutionEvent, ...]
    duplicate_sequences: tuple[int, ...]
    terminal_observations: tuple["TerminalEventObservation", ...] = ()


@dataclass(frozen=True, slots=True)
class TerminalEventObservation:
    """A redacted terminal fact that can be recorded by a result coordinator."""

    event_type: str
    sequence: int
    outcome: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ArtifactRegistrationReport:
    artifacts: tuple[Artifact, ...]
    evidence_claims: tuple[EvidenceClaim, ...]


class SessionEventIngestor:
    """Persist ordered, fenced Adapter events as standard ExecutionEvents."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def ingest(
        self,
        *,
        workspace_id: UUID,
        execution_id: UUID,
        session_id: UUID,
        lease_id: UUID,
        fencing_token: int,
        events: Iterable[AdapterEvent],
    ) -> EventIngestionReport:
        ingested: list[ExecutionEvent] = []
        duplicates: list[int] = []
        terminal_observations: list[TerminalEventObservation] = []
        for event in events:
            observation = _terminal_observation(event)
            if observation is not None:
                terminal_observations.append(observation)
            result = self.ingest_event(
                workspace_id=workspace_id,
                execution_id=execution_id,
                session_id=session_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                event=event,
            )
            if result.duplicate:
                duplicates.append(event.sequence)
            elif result.event is not None:
                ingested.append(result.event)
        return EventIngestionReport(
            tuple(ingested),
            tuple(duplicates),
            tuple(terminal_observations),
        )

    def ingest_event(
        self,
        *,
        workspace_id: UUID,
        execution_id: UUID,
        session_id: UUID,
        lease_id: UUID,
        fencing_token: int,
        event: AdapterEvent,
    ) -> EventIngestionResult:
        gap: EventSequenceGap | None = None
        with self._uow_factory() as uow:
            execution = uow.executions.get(execution_id, workspace_id, for_update=True)
            session = uow.sessions.get_active_for_execution(
                execution_id, for_update=True
            )
            lease = uow.leases.get(lease_id, workspace_id, for_update=True)
            if execution is None or session is None or lease is None:
                raise EventIngestionError("event runtime facts are unavailable")
            self._assert_fence(
                execution,
                session,
                lease,
                lease_id=lease_id,
                fencing_token=fencing_token,
            )

            existing = uow.execution_events.get(
                session.id, event.sequence, workspace_id
            )
            if existing is not None:
                if _same_event(existing, event):
                    return EventIngestionResult(
                        event=None,
                        ingested=False,
                        duplicate=True,
                    )
                raise EventSequenceConflict(
                    f"event sequence {event.sequence} has different content"
                )

            expected_sequence = session.last_sequence + 1
            if event.sequence != expected_sequence:
                if event.sequence > expected_sequence:
                    previous_version = session.version
                    error_event = _gap_event(
                        workspace_id=workspace_id,
                        execution_id=execution_id,
                        session_id=session.id,
                        sequence=expected_sequence,
                        event=event,
                        occurred_at=self._clock(),
                    )
                    session.record_activity(
                        sequence=expected_sequence, now=self._clock()
                    )
                    uow.sessions.save(session, previous_version)
                    uow.execution_events.add(error_event)
                    gap = EventSequenceGap(expected_sequence, event.sequence)
                else:
                    raise EventIngestionError(
                        f"event sequence {event.sequence} is behind "
                        f"the Session cursor {session.last_sequence}"
                    )
            else:
                normalized = ExecutionEvent(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    execution_id=execution_id,
                    session_id=session.id,
                    sequence=event.sequence,
                    event_type=event.event_type,
                    payload=_redact_payload(event.payload),
                    occurred_at=event.occurred_at,
                    raw_ref=_bounded_raw_ref(event.raw_ref),
                )
                previous_version = session.version
                session.record_activity(sequence=event.sequence, now=self._clock())
                uow.sessions.save(session, previous_version)
                uow.execution_events.add(normalized)
                record_verification_observation(
                    uow,
                    execution=execution,
                    event=normalized,
                )
                return EventIngestionResult(
                    event=normalized,
                    ingested=True,
                    duplicate=False,
                )
        if gap is not None:
            raise gap
        raise EventIngestionError("event ingestion ended without a result")

    def _assert_fence(
        self,
        execution: Execution,
        session: ExecutionSession,
        lease: AssignmentLease,
        *,
        lease_id: UUID,
        fencing_token: int,
    ) -> None:
        if (
            session.execution_id != execution.id
            or session.workspace_id != execution.workspace_id
        ):
            raise EventIngestionError("session does not belong to the execution")
        execution.assert_fence(lease_id=lease_id, fencing_token=fencing_token)
        lease.assert_owner(session.worker_id, fencing_token, self._clock())


class ArtifactRegistry:
    """Register immutable Artifact metadata and candidate Evidence claims."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def register(
        self,
        *,
        workspace_id: UUID,
        task_id: UUID,
        execution_id: UUID,
        session_id: UUID | None,
        candidates: Iterable[ArtifactCandidate],
        acceptance_criteria: Iterable[str],
        producer: str,
        environment_ref: str,
    ) -> ArtifactRegistrationReport:
        criteria = {item.strip() for item in acceptance_criteria if item.strip()}
        artifacts: list[Artifact] = []
        claims: list[EvidenceClaim] = []
        with self._uow_factory() as uow:
            execution_probe = uow.executions.get(execution_id, workspace_id)
            if execution_probe is None:
                raise ArtifactRegistrationError("artifact execution was not found")
            if execution_probe.task_id != task_id:
                raise ArtifactRegistrationError(
                    "artifact task does not match its execution"
                )
            source_fence = discover_result_source_fence(uow, execution_probe)
            lock_result_source_fence(uow, source_fence)
            execution = uow.executions.get(execution_id, workspace_id, for_update=True)
            if execution is None or execution.version != execution_probe.version:
                raise ArtifactRegistrationError(
                    "artifact execution changed during registration"
                )
            existing_artifacts = list(
                uow.artifacts.list_for_execution(execution_id, workspace_id)
            )
            existing_claims = list(
                uow.evidence_claims.list_for_task(task_id, workspace_id)
            )
            for candidate in candidates:
                artifact = _find_artifact(existing_artifacts, candidate)
                if artifact is None:
                    sanitized_candidate = ArtifactCandidate(
                        artifact_type=candidate.artifact_type,
                        storage_ref=candidate.storage_ref,
                        content_hash=candidate.content_hash,
                        size_bytes=candidate.size_bytes,
                        media_type=candidate.media_type,
                        provenance=_redact_payload(candidate.provenance),
                        content=None,
                    )
                    artifact = Artifact.from_candidate(
                        id=uuid4(),
                        workspace_id=workspace_id,
                        task_id=task_id,
                        execution_id=execution_id,
                        session_id=session_id,
                        candidate=sanitized_candidate,
                    )
                    uow.artifacts.add(artifact)
                    existing_artifacts.append(artifact)
                artifacts.append(artifact)

                for criterion in _criterion_refs(candidate.provenance):
                    if criterion not in criteria:
                        raise ArtifactRegistrationError(
                            f"artifact references unknown acceptance criterion: {criterion}"
                        )
                    claim = _find_claim(
                        existing_claims,
                        artifact_id=artifact.id,
                        criterion=criterion,
                    )
                    if claim is None:
                        conclusion = _provenance_text(
                            candidate.provenance,
                            "conclusion",
                            "Artifact was produced by the worker.",
                        )
                        claim = EvidenceClaim.create_candidate(
                            id=uuid4(),
                            workspace_id=workspace_id,
                            task_id=task_id,
                            execution_id=execution_id,
                            acceptance_criterion_ref=criterion,
                            conclusion=conclusion,
                            environment_ref=_provenance_text(
                                candidate.provenance,
                                "environment_ref",
                                environment_ref,
                            ),
                            artifact_ids=(artifact.id,),
                            producer=_provenance_text(
                                candidate.provenance, "producer", producer
                            ),
                            created_at=self._clock(),
                        )
                        uow.evidence_claims.add(claim)
                        existing_claims.append(claim)
                    claims.append(claim)
        return ArtifactRegistrationReport(tuple(artifacts), tuple(claims))


_TERMINAL_EVENT_OUTCOMES = {
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


def _terminal_observation(
    event: AdapterEvent,
) -> TerminalEventObservation | None:
    outcome = _TERMINAL_EVENT_OUTCOMES.get(event.event_type.strip().lower())
    if outcome is None:
        return None
    return TerminalEventObservation(
        event_type=event.event_type,
        sequence=event.sequence,
        outcome=outcome,
        payload=_redact_payload(event.payload),
    )


def _gap_event(
    *,
    workspace_id: UUID,
    execution_id: UUID,
    session_id: UUID,
    sequence: int,
    event: AdapterEvent,
    occurred_at: datetime,
) -> ExecutionEvent:
    return ExecutionEvent(
        id=uuid4(),
        workspace_id=workspace_id,
        execution_id=execution_id,
        session_id=session_id,
        sequence=sequence,
        event_type="event.ingestion.error",
        payload={
            "code": "event_sequence_gap",
            "expected_sequence": sequence,
            "received_sequence": event.sequence,
            "received_event_type": event.event_type,
        },
        occurred_at=occurred_at,
        raw_ref=_bounded_raw_ref(event.raw_ref),
    )


def _find_artifact(
    artifacts: Iterable[Artifact], candidate: ArtifactCandidate
) -> Artifact | None:
    return next(
        (
            artifact
            for artifact in artifacts
            if artifact.storage_ref == candidate.storage_ref
            and artifact.content_hash == candidate.content_hash
        ),
        None,
    )


def _find_claim(
    claims: Iterable[EvidenceClaim], *, artifact_id: UUID, criterion: str
) -> EvidenceClaim | None:
    return next(
        (
            claim
            for claim in claims
            if claim.acceptance_criterion_ref == criterion
            and artifact_id in claim.artifact_ids
        ),
        None,
    )


def _criterion_refs(provenance: Mapping[str, Any]) -> tuple[str, ...]:
    value = provenance.get("acceptance_criterion_refs")
    if value is None:
        value = provenance.get("acceptance_criterion_ref")
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(
            item.strip() for item in value if isinstance(item, str) and item.strip()
        )
    if value is not None:
        raise ArtifactRegistrationError(
            "acceptance_criterion_ref(s) must be text or a list of text"
        )
    return ()


def _provenance_text(provenance: Mapping[str, Any], name: str, default: str) -> str:
    value = provenance.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else default


def _same_event(existing: ExecutionEvent, incoming: AdapterEvent) -> bool:
    return (
        existing.event_type == incoming.event_type
        and dict(existing.payload) == _redact_payload(incoming.payload)
        and existing.raw_ref == _bounded_raw_ref(incoming.raw_ref)
    )


def _bounded_raw_ref(value: str | None) -> str | None:
    if value is None:
        return None
    clean = value.strip()
    return clean[:500] if clean else None


def _redact_payload(payload: Mapping[str, Any]) -> dict[str, object]:
    redacted = _redact_value(payload)
    if not isinstance(redacted, dict):
        return {"value": redacted}
    try:
        encoded = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = json.dumps({"value": str(redacted)}, separators=(",", ":"))
    encoded_bytes = encoded.encode("utf-8")
    if len(encoded_bytes) <= MAX_EVENT_PAYLOAD_BYTES:
        return redacted
    return {
        "truncated": True,
        "sha256": hashlib.sha256(encoded_bytes).hexdigest(),
        "size_bytes": len(encoded_bytes),
    }


def _redact_value(value: Any, *, key: str | None = None) -> object:
    if key is not None and _SECRET_KEY.search(key):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= MAX_EVENT_STRING_BYTES:
            return value
        return {
            "truncated": True,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "size_bytes": len(encoded),
        }
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)
