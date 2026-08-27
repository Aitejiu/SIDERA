from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.knowledge_candidate_submission import (
    CandidateProposalSpoolError,
    FileCandidateProposalSpool,
    KnowledgeCandidateProposalV1,
    KnowledgeCandidateSectionV1,
    KnowledgeCandidateSourceSuggestionV1,
    KnowledgeCandidateSubmission,
    KnowledgeCandidateSubmissionStatus,
)
from sidera_control_plane.knowledge_candidate_worker import _candidate_payload


def _proposal(*, body: str = "The durable fact.") -> KnowledgeCandidateProposalV1:
    return KnowledgeCandidateProposalV1(
        operation="create_page",
        target_page_id=None,
        expected_revision_id=None,
        expected_revision_hash=None,
        title="Durable fact",
        slug="durable-fact",
        summary="A fact proposed by a worker.",
        tags=("sidera", "runtime"),
        sections=(
            KnowledgeCandidateSectionV1(
                section_key="summary",
                heading="Summary",
                summary="A safe summary.",
                body_markdown=body,
                ordinal=0,
            ),
        ),
        source_suggestions=(),
        artifact_refs=("artifact:diff",),
        evidence_refs=("evidence:acceptance",),
    )


def test_candidate_proposal_hash_is_canonical_and_scope_neutral() -> None:
    proposal = _proposal()

    encoded = proposal.canonical_bytes()
    assert proposal.proposal_hash == hashlib.sha256(encoded).hexdigest()
    assert proposal.to_mapping()["schema_version"] == 1
    assert "workspace_id" not in proposal.to_mapping()
    assert "task_id" not in proposal.to_mapping()
    assert "actor_id" not in proposal.to_mapping()
    assert "review_decision" not in proposal.to_mapping()
    assert KnowledgeCandidateProposalV1.from_mapping(
        json.loads(encoded.decode("utf-8"))
    ) == proposal


def test_revision_proposal_requires_exact_target_revision() -> None:
    target_page_id = uuid4()
    revision_id = uuid4()
    proposal = KnowledgeCandidateProposalV1(
        operation="revise_page",
        target_page_id=target_page_id,
        expected_revision_id=revision_id,
        expected_revision_hash="a" * 64,
        title="Revised fact",
        slug="revised-fact",
        summary="revision",
        tags=(),
        sections=_proposal().sections,
        source_suggestions=(),
        artifact_refs=(),
        evidence_refs=(),
    )
    assert proposal.to_mapping()["target_page_id"] == str(target_page_id)
    assert proposal.to_mapping()["expected_revision_id"] == str(revision_id)

    with pytest.raises(ValueError, match="target_page_id"):
        KnowledgeCandidateProposalV1(
            operation="create_page",
            target_page_id=target_page_id,
            expected_revision_id=None,
            expected_revision_hash=None,
            title="invalid",
            slug="invalid",
            summary="invalid",
            tags=(),
            sections=_proposal().sections,
            source_suggestions=(),
            artifact_refs=(),
            evidence_refs=(),
        )


def test_file_spool_is_atomic_private_and_hash_addressed(tmp_path) -> None:
    spool = FileCandidateProposalSpool(tmp_path / "candidate-spool", quota_bytes=1024 * 1024)
    proposal = _proposal()

    stored = spool.put(proposal)

    assert stored.content_bytes == len(proposal.canonical_bytes())
    assert stored.proposal_hash == proposal.proposal_hash
    assert stored.content_ref.endswith(f"-{proposal.proposal_hash}.json")
    path = spool.path_for(stored.content_ref)
    assert path is not None
    assert path.exists()
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert spool.read(stored.content_ref, expected_hash=proposal.proposal_hash) == proposal

    spool.delete(stored.content_ref)
    with pytest.raises(CandidateProposalSpoolError, match="not found"):
        spool.read(stored.content_ref, expected_hash=proposal.proposal_hash)


def test_file_spool_rejects_path_traversal_hash_mismatch_and_quota(tmp_path) -> None:
    proposal = _proposal(body="x" * 64)
    spool = FileCandidateProposalSpool(tmp_path / "candidate-spool", quota_bytes=10)

    with pytest.raises(CandidateProposalSpoolError, match="quota"):
        spool.put(proposal)

    spool = FileCandidateProposalSpool(tmp_path / "candidate-spool-2", quota_bytes=1024 * 1024)
    stored = spool.put(proposal)
    with pytest.raises(CandidateProposalSpoolError, match="path"):
        spool.read("../outside.json", expected_hash=proposal.proposal_hash)
    with pytest.raises(CandidateProposalSpoolError, match="hash"):
        spool.read(stored.content_ref, expected_hash="b" * 64)

    with pytest.raises(CandidateProposalSpoolError, match="path"):
        spool.delete("../../outside.json")


def test_file_spool_quarantines_permanent_failures_and_gcs_only_old_files(tmp_path) -> None:
    spool = FileCandidateProposalSpool(tmp_path / "candidate-spool", quota_bytes=1024 * 1024)
    stored = spool.put(_proposal())
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

    spool.quarantine(stored.content_ref, now=now)

    assert not spool.path_for(stored.content_ref).exists()
    quarantined = spool.root / "quarantine" / stored.content_ref
    assert quarantined.exists()
    assert os.stat(quarantined).st_mode & 0o777 == 0o600
    assert spool.gc_quarantine(now=now + timedelta(hours=23), retention=timedelta(hours=24)) == 0
    assert spool.gc_quarantine(now=now + timedelta(hours=25), retention=timedelta(hours=24)) == 1
    assert not quarantined.exists()


def test_proposal_ids_are_uuid_free_from_serialized_scope() -> None:
    mapping = _proposal().to_mapping()
    assert all(
        not isinstance(value, UUID)
        for value in mapping.values()
        if not isinstance(value, (list, dict))
    )


def test_worker_task_source_uses_submission_scope_not_proposal_metadata() -> None:
    task_id = uuid4()
    subtask_task_id = uuid4()
    producer_id = uuid4()
    proposal = KnowledgeCandidateProposalV1(
        operation="create_page",
        target_page_id=None,
        expected_revision_id=None,
        expected_revision_hash=None,
        title="Worker fact",
        slug="worker-fact",
        summary="A worker fact.",
        tags=(),
        sections=_proposal().sections,
        source_suggestions=(
            KnowledgeCandidateSourceSuggestionV1(
                source_type="task",
                label="worker task",
                source_id=uuid4(),
            ),
        ),
        artifact_refs=(),
        evidence_refs=(),
    )
    submission = KnowledgeCandidateSubmission.create(
        submission_id=uuid4(),
        workspace_id=uuid4(),
        producer_type="worker",
        producer_id=producer_id,
        proposal=proposal,
        content_ref="worker.json",
        content_bytes=len(proposal.canonical_bytes()),
        status=KnowledgeCandidateSubmissionStatus.QUEUED,
        execution_id=uuid4(),
        worker_item_id=uuid4(),
        task_id=task_id,
        subtask_task_id=subtask_task_id,
        now=datetime.now(UTC),
    )

    payload = _candidate_payload(proposal, submission, uuid4())
    sources = cast(list[dict[str, object]], payload["sources"])
    source = sources[0]
    assert source["source_type"] == "task"
    assert source["metadata"] == {}
    provenance = cast(dict[str, object], payload["provenance"])
    assert provenance["task_id"] == str(task_id)
    assert provenance["subtask_id"] == str(subtask_task_id)
