from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from sidera_knowledge.domain.candidates import (
    CandidateStateError,
    KnowledgeCandidate,
    KnowledgeCandidateReview,
    ensure_candidate_hash,
)
from sidera_knowledge.domain.pages import KnowledgeSectionSnapshot

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PAGE_ID = UUID("00000000-0000-0000-0000-000000000002")


def _candidate(*, target_page_id: UUID | None = None) -> KnowledgeCandidate:
    return KnowledgeCandidate(
        candidate_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        operation="revise_page" if target_page_id else "create_page",
        target_page_id=target_page_id,
        expected_revision_id=uuid4() if target_page_id else None,
        expected_revision_hash="b" * 64 if target_page_id else None,
        proposed_title="Project guide",
        proposed_slug="project-guide",
        proposed_summary="A candidate",
        proposed_tags=("sidera",),
        proposed_sections=(
            KnowledgeSectionSnapshot(
                section_key="overview",
                heading="Overview",
                summary="Summary",
                body_markdown="Candidate body",
                ordinal=0,
            ),
        ),
        producer_type="worker",
        producer_id="worker-1",
        task_id=uuid4(),
    )


def test_candidate_hash_covers_snapshot_provenance_and_is_stable() -> None:
    candidate = _candidate(target_page_id=PAGE_ID)
    assert len(candidate.candidate_hash) == 64
    assert candidate.proposed_snapshot["sections"][0]["body_markdown"] == (
        "Candidate body\n"
    )
    assert candidate.candidate_hash == candidate.candidate_hash

    with pytest.raises(CandidateStateError):
        ensure_candidate_hash(candidate, "0" * 64)


def test_revise_candidate_requires_target_revision_and_hash() -> None:
    with pytest.raises(CandidateStateError, match="target_page_id"):
        replace(_candidate(target_page_id=PAGE_ID), target_page_id=None)


def test_review_is_only_allowed_for_pending_candidates_and_terminal_states_cannot_repeat() -> None:
    candidate = _candidate()
    assert candidate.is_reviewable
    accepted = candidate.transition("accepted")
    assert accepted.status == "accepted"
    assert not accepted.is_reviewable

    with pytest.raises(CandidateStateError, match="terminal"):
        accepted.transition("rejected")

    review = KnowledgeCandidateReview(
        review_id=uuid4(),
        candidate_id=candidate.candidate_id,
        reviewer_actor="user:aitejiu",
        decision="accept",
        reason="Looks correct",
        observed_candidate_hash=candidate.candidate_hash,
        created_revision_id=uuid4(),
    )
    assert review.created_revision_id is not None


def test_non_accept_review_cannot_create_revision_and_changes_use_new_candidate() -> None:
    candidate = _candidate()
    with pytest.raises(CandidateStateError, match="created_revision_id"):
        KnowledgeCandidateReview(
            review_id=uuid4(),
            candidate_id=candidate.candidate_id,
            reviewer_actor="user:aitejiu",
            decision="request_changes",
            reason="Clarify the source",
            observed_candidate_hash=candidate.candidate_hash,
            created_revision_id=uuid4(),
        )

    replacement = _candidate()
    replacement = replace(
        replacement,
        supersedes_candidate_id=candidate.candidate_id,
    )
    assert replacement.supersedes_candidate_id == candidate.candidate_id
