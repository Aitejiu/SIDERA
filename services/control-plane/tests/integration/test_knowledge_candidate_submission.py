from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import select

from sidera_control_plane.adapters.knowledge_http import KnowledgeAdapterError
from sidera_control_plane.application.knowledge_candidate_submission import (
    FileCandidateProposalSpool,
    KnowledgeCandidateProposalV1,
    KnowledgeCandidateSectionV1,
    KnowledgeCandidateSubmissionService,
    KnowledgeCandidateSubmissionStatus,
)
from sidera_control_plane.application.ports import KnowledgeCommandStatus
from sidera_control_plane.knowledge_candidate_worker import (
    KnowledgeCandidateSubmissionWorker,
)
from sidera_control_plane.persistence.models import (
    EventModel,
    KnowledgeCandidateSubmissionModel,
    OutboxMessageModel,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
MARKER = "candidate-body-marker-4e4a3f5c"


def _proposal(*, artifact_ref: str | None = None) -> KnowledgeCandidateProposalV1:
    return KnowledgeCandidateProposalV1(
        operation="create_page",
        target_page_id=None,
        expected_revision_id=None,
        expected_revision_hash=None,
        title="Candidate page",
        slug="candidate-page",
        summary="Candidate summary",
        tags=("candidate",),
        sections=(
            KnowledgeCandidateSectionV1(
                section_key="body",
                heading="Body",
                summary="Body summary",
                body_markdown=MARKER,
                ordinal=0,
            ),
        ),
        source_suggestions=(),
        artifact_refs=(artifact_ref,) if artifact_ref is not None else (),
        evidence_refs=(),
    )


class FakeKnowledgeCatalog:
    def __init__(
        self,
        *,
        temporary_failure: bool = False,
        command_missing: bool = False,
    ) -> None:
        self.temporary_failure = temporary_failure
        self.command_missing = command_missing
        self.submitted: list[tuple[UUID, Mapping[str, object]]] = []
        self.status_calls = 0

    def search(self, *args, **kwargs):
        raise AssertionError("candidate worker must not search Knowledge")

    def get_revision_manifest(self, *args, **kwargs):
        raise AssertionError("candidate worker must not read Knowledge pages")

    def materialize(self, *args, **kwargs):
        raise AssertionError("candidate worker must not materialize Knowledge")

    def get_command_status(
        self,
        workspace_id: UUID,
        command_id: UUID,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> KnowledgeCommandStatus:
        self.status_calls += 1
        if self.temporary_failure:
            raise KnowledgeAdapterError(
                "temporary_unavailable",
                "temporary",
                temporary=True,
            )
        if self.command_missing:
            raise KnowledgeAdapterError(
                "reference_not_found",
                "command is not known yet",
                temporary=False,
                status_code=404,
            )
        return KnowledgeCommandStatus(
            command_id=command_id,
            workspace_id=workspace_id,
            status="pending",
            result=None,
            error_code=None,
        )

    def submit_candidate(
        self,
        workspace_id: UUID,
        command_id: UUID,
        idempotency_key: str,
        candidate_payload: Mapping[str, object],
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> KnowledgeCommandStatus:
        self.submitted.append((command_id, candidate_payload))
        candidate_id = UUID(str(candidate_payload["candidate_id"]))
        return KnowledgeCommandStatus(
            command_id=command_id,
            workspace_id=workspace_id,
            status="applied",
            result={"candidate_id": str(candidate_id), "status": "pending"},
            error_code=None,
        )


def _queue(
    uow_factory,
    workspace_id: UUID,
    spool: FileCandidateProposalSpool,
    proposal: KnowledgeCandidateProposalV1,
    *,
    producer_id: UUID | None = None,
) -> UUID:
    service = KnowledgeCandidateSubmissionService(spool)
    submission_id = uuid4()
    with uow_factory() as uow:
        submission = service.queue_in_uow(
            uow,
            workspace_id=workspace_id,
            producer_type="worker",
            producer_id=producer_id or uuid4(),
            proposal=proposal,
            status=KnowledgeCandidateSubmissionStatus.QUEUED,
            now=NOW,
            correlation_id=submission_id,
            execution_id=None,
        )
        return submission.id


@pytest.mark.integration
def test_candidate_body_stays_in_spool_and_worker_submits_idempotently(
    workspace_id, uow_factory, database, tmp_path: Path
) -> None:
    spool = FileCandidateProposalSpool(tmp_path / "candidate-spool", quota_bytes=1_000_000)
    proposal = _proposal(artifact_ref=str(uuid4()))
    submission_id = _queue(uow_factory, workspace_id, spool, proposal)

    with database.session() as session:
        event_payloads = session.scalars(select(EventModel.payload)).all()
        outbox_payloads = session.scalars(select(OutboxMessageModel.payload)).all()
        row = session.get(KnowledgeCandidateSubmissionModel, submission_id)
        assert row is not None
        assert MARKER not in str(row)
        assert MARKER not in str(event_payloads)
        assert MARKER not in str(outbox_payloads)
        assert row.proposal_hash == proposal.proposal_hash

    catalog = FakeKnowledgeCatalog()
    worker = KnowledgeCandidateSubmissionWorker(
        uow_factory=uow_factory,
        catalog=catalog,
        spool=spool,
        owner_id="candidate-worker-test",
        clock=lambda: NOW,
    )
    result = worker.run_once()

    assert result.claimed == 1
    assert result.submitted == 1
    assert catalog.status_calls == 1
    assert len(catalog.submitted) == 1
    payload = catalog.submitted[0][1]
    with database.session() as session:
        stored = session.get(KnowledgeCandidateSubmissionModel, submission_id)
        assert stored is not None
        expected_candidate_id = uuid5(
            NAMESPACE_URL,
            f"sidera.knowledge-candidate:{stored.producer_type}:{stored.producer_id}:{stored.proposal_hash}",
        )
    assert payload["candidate_id"] == str(expected_candidate_id)
    assert MARKER in str(payload)
    assert spool.usage_bytes() == 0

    with database.session() as session:
        row = session.get(KnowledgeCandidateSubmissionModel, submission_id)
        assert row is not None
        assert row.status == "submitted"
        assert row.knowledge_command_id == catalog.submitted[0][0]
        assert MARKER not in str(session.scalars(select(EventModel.payload)).all())


@pytest.mark.integration
def test_temporary_knowledge_failure_enters_retry_wait_and_keeps_spool(
    workspace_id, uow_factory, database, tmp_path: Path
) -> None:
    spool = FileCandidateProposalSpool(tmp_path / "candidate-spool", quota_bytes=1_000_000)
    submission_id = _queue(uow_factory, workspace_id, spool, _proposal())
    worker = KnowledgeCandidateSubmissionWorker(
        uow_factory=uow_factory,
        catalog=FakeKnowledgeCatalog(temporary_failure=True),
        spool=spool,
        owner_id="candidate-worker-test",
        clock=lambda: NOW,
    )

    result = worker.run_once()

    assert result.retried == 1
    assert spool.usage_bytes() > 0
    with database.session() as session:
        row = session.get(KnowledgeCandidateSubmissionModel, submission_id)
        assert row is not None
        assert row.status == "retry_wait"
        assert row.error_code == "temporary_unavailable"
        assert row.knowledge_command_id is not None


@pytest.mark.integration
def test_existing_dispatch_command_is_reused_after_local_crash(
    workspace_id, uow_factory, tmp_path: Path
) -> None:
    spool = FileCandidateProposalSpool(tmp_path / "candidate-spool", quota_bytes=1_000_000)
    submission_id = _queue(uow_factory, workspace_id, spool, _proposal())
    command_id = uuid5(
        NAMESPACE_URL,
        f"sidera.knowledge-candidate-command:{submission_id}",
    )
    with uow_factory() as uow:
        submission = uow.knowledge_candidate_submissions.get(
            submission_id, workspace_id, for_update=True
        )
        assert submission is not None
        KnowledgeCandidateSubmissionWorker._ensure_dispatch_command(
            uow, submission, command_id
        )

    worker = KnowledgeCandidateSubmissionWorker(
        uow_factory=uow_factory,
        catalog=FakeKnowledgeCatalog(),
        spool=spool,
        owner_id="candidate-worker-test",
        clock=lambda: NOW,
    )

    result = worker.run_once()

    assert result.submitted == 1
    assert result.failed == 0


@pytest.mark.integration
def test_missing_remote_command_is_submitted_after_status_probe(
    workspace_id, uow_factory, tmp_path: Path
) -> None:
    spool = FileCandidateProposalSpool(tmp_path / "candidate-spool", quota_bytes=1_000_000)
    _queue(uow_factory, workspace_id, spool, _proposal())
    catalog = FakeKnowledgeCatalog(command_missing=True)
    worker = KnowledgeCandidateSubmissionWorker(
        uow_factory=uow_factory,
        catalog=catalog,
        spool=spool,
        owner_id="candidate-worker-test",
        clock=lambda: NOW,
    )

    result = worker.run_once()

    assert result.submitted == 1
    assert result.rejected == 0
    assert len(catalog.submitted) == 1
