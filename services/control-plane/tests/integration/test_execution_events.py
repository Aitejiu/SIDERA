from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from sidera_control_plane.application.event_ingestor import (
    ArtifactRegistry,
    EventSequenceGap,
    SessionEventIngestor,
)
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.domain.errors import StaleFencingToken
from sidera_control_plane.domain.gateway import AdapterEvent
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    AssignmentLeaseModel,
    EvidenceClaimModel,
    ExecutionEventModel,
    ExecutionModel,
    ExecutionSessionModel,
    TaskModel,
    WorkerModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _capability_payload() -> dict[str, str]:
    return {
        "input": CapabilityInput.QUEUED.value,
        "idempotency": IdempotencyCapability.NONE.value,
        "resume": ResumeCapability.SAME_PROCESS.value,
        "stop": StopCapability.EXACT_EXECUTION.value,
        "sandbox": SandboxCapability.FILESYSTEM.value,
        "stability": StabilityLevel.EXPERIMENTAL.value,
    }


def _seed_session(database: Database, workspace_id: UUID) -> dict[str, UUID]:
    task_id = uuid4()
    worker_id = uuid4()
    execution_id = uuid4()
    lease_id = uuid4()
    session_id = uuid4()
    with database.session() as session:
        session.add(
            TaskModel(
                id=task_id,
                workspace_id=workspace_id,
                title="Event ingestion",
                description="Persist ordered events",
                acceptance_criteria=["events are ordered"],
                status="running",
                priority=50,
                requested_by="integration",
                version=1,
            )
        )
        session.add(
            WorkerModel(
                id=worker_id,
                workspace_id=workspace_id,
                adapter_type="fake",
                display_name="Event Worker",
                status="busy",
                desired_state="active",
                capabilities=["python"],
                capacity=1,
                current_load=1,
                cost_per_hour_microunits=1,
                startup_latency_ms=1,
                last_heartbeat_at=NOW,
                version=1,
            )
        )
        session.commit()
    with database.session() as session:
        execution = ExecutionModel(
            id=execution_id,
            workspace_id=workspace_id,
            task_id=task_id,
            lineage_id=execution_id,
            attempt=1,
            max_attempts=2,
            purpose="Persist events",
            status="running",
            capability_requirements=["python"],
            pinned_worker_id=None,
            assigned_worker_id=worker_id,
            active_lease_id=None,
            last_fencing_token=1,
            not_before=NOW,
            failure_code=None,
            started_at=NOW,
            finished_at=None,
            version=1,
        )
        session.add(execution)
        session.flush([execution])
        session.add(
            AssignmentLeaseModel(
                id=lease_id,
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_id,
                status="active",
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                renewed_at=None,
                ended_at=None,
                fencing_token=1,
                assignment_reason={"source": "integration"},
                version=1,
            )
        )
        session.add(
            ExecutionSessionModel(
                id=session_id,
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_id,
                adapter_session_id="event-session",
                status="active",
                last_sequence=0,
                last_activity_at=NOW,
                input_mode="queued",
                adapter_module_id=None,
                adapter_config_version=None,
                workspace_module_id=None,
                workspace_config_version=None,
                capability_snapshot=_capability_payload(),
                process_ref="fake://event-session",
                workspace_ref="fake-workspace://event-session",
                version=1,
            )
        )
        session.flush()
        session.execute(
            update(ExecutionModel)
            .where(
                ExecutionModel.id == execution_id,
                ExecutionModel.workspace_id == workspace_id,
            )
            .values(active_lease_id=lease_id)
        )
        session.commit()
    return {
        "task_id": task_id,
        "worker_id": worker_id,
        "execution_id": execution_id,
        "lease_id": lease_id,
        "session_id": session_id,
    }


def _event(
    sequence: int,
    *,
    event_type: str = "progress.updated",
    payload: dict[str, object] | None = None,
) -> AdapterEvent:
    return AdapterEvent(
        event_type=event_type,
        sequence=sequence,
        payload=payload or {"progress": sequence},
        occurred_at=NOW,
        raw_ref=f"artifact://raw/event/{sequence}",
    )


def _ingestor(database: Database) -> SessionEventIngestor:
    return SessionEventIngestor(
        lambda: SqlAlchemyUnitOfWork(database),
        clock=lambda: NOW,
    )


def test_event_ingestor_deduplicates_and_rejects_stale_fences(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_session(database, workspace_id)
    ingestor = _ingestor(database)
    kwargs = {
        "workspace_id": workspace_id,
        "execution_id": ids["execution_id"],
        "session_id": ids["session_id"],
        "lease_id": ids["lease_id"],
        "fencing_token": 1,
    }

    first = ingestor.ingest_event(event=_event(1), **kwargs)
    duplicate = ingestor.ingest_event(event=_event(1), **kwargs)

    assert first.ingested is True
    assert first.event is not None
    assert duplicate.duplicate is True
    with pytest.raises(StaleFencingToken):
        ingestor.ingest_event(event=_event(2), **{**kwargs, "fencing_token": 2})
    with database.session() as session:
        assert session.query(ExecutionEventModel).count() == 1
        stored_session = session.get(ExecutionSessionModel, ids["session_id"])
        assert stored_session is not None
        assert stored_session.last_sequence == 1


def test_event_ingestor_records_sequence_gap_before_raising(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_session(database, workspace_id)
    ingestor = _ingestor(database)
    kwargs = {
        "workspace_id": workspace_id,
        "execution_id": ids["execution_id"],
        "session_id": ids["session_id"],
        "lease_id": ids["lease_id"],
        "fencing_token": 1,
    }

    with pytest.raises(EventSequenceGap) as error:
        ingestor.ingest_event(event=_event(3), **kwargs)

    assert error.value.expected_sequence == 1
    assert error.value.received_sequence == 3
    with database.session() as session:
        stored_event = session.query(ExecutionEventModel).one()
        assert stored_event.sequence == 1
        assert stored_event.event_type == "event.ingestion.error"
        assert stored_event.payload["received_sequence"] == 3
        stored_session = session.get(ExecutionSessionModel, ids["session_id"])
        assert stored_session is not None
        assert stored_session.last_sequence == 1


def test_turn_completion_is_stored_but_not_reported_as_terminal(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_session(database, workspace_id)
    ingestor = _ingestor(database)
    operation_id = uuid4()
    kwargs = {
        "workspace_id": workspace_id,
        "execution_id": ids["execution_id"],
        "session_id": ids["session_id"],
        "lease_id": ids["lease_id"],
        "fencing_token": 1,
    }

    report = ingestor.ingest(
        events=(
            _event(
                1,
                event_type="turn.completed",
                payload={
                    "schema_version": 1,
                    "operation_id": str(operation_id),
                    "stop_reason": "end_turn",
                },
            ),
        ),
        **kwargs,
    )

    assert [event.event_type for event in report.events] == ["turn.completed"]
    assert report.terminal_observations == ()
    with database.session() as session:
        stored = session.query(ExecutionEventModel).one()
        assert stored.event_type == "turn.completed"
        assert stored.payload["operation_id"] == str(operation_id)


def test_artifact_registry_is_idempotent_and_only_creates_candidate_evidence(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_session(database, workspace_id)
    registry = ArtifactRegistry(
        lambda: SqlAlchemyUnitOfWork(database),
        clock=lambda: NOW,
    )
    candidate = ArtifactCandidate(
        artifact_type=ArtifactType.REPORT,
        storage_ref="artifact://report/1",
        content_hash="a" * 64,
        size_bytes=10,
        media_type="text/plain",
        provenance={
            "producer": "fake",
            "acceptance_criterion_ref": "events are ordered",
            "conclusion": "The event stream is ordered.",
            "environment_ref": "env://integration",
            "api_key": "must-not-persist",
        },
    )
    kwargs = {
        "workspace_id": workspace_id,
        "task_id": ids["task_id"],
        "execution_id": ids["execution_id"],
        "session_id": ids["session_id"],
        "candidates": (candidate,),
        "acceptance_criteria": ("events are ordered",),
        "producer": "adapter",
        "environment_ref": "env://default",
    }

    first = registry.register(**kwargs)
    second = registry.register(**kwargs)

    assert len(first.artifacts) == 1
    assert len(first.evidence_claims) == 1
    assert second.artifacts == first.artifacts
    assert second.evidence_claims == first.evidence_claims
    with database.session() as session:
        artifact = session.query(ArtifactModel).one()
        claim = session.query(EvidenceClaimModel).one()
        assert artifact.provenance["api_key"] == "[redacted]"
        assert claim.status == "candidate"
