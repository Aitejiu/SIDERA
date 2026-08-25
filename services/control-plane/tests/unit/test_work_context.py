from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.errors import (
    InvalidTransition,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.work_context import (
    AcceptedDecision,
    ClarificationDecision,
    ContextTextItem,
    KnowledgeRevisionRef,
    UnresolvedQuestion,
    WorkContext,
    WorkContextRevision,
    WorkContextRevisionReason,
    WorkContextSnapshotV1,
    WorkContextStatus,
    WorkReference,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
KNOWLEDGE_PAGE_ID = UUID("00000000-0000-0000-0000-000000000001")
KNOWLEDGE_REVISION_ID = UUID("00000000-0000-0000-0000-000000000002")
KNOWLEDGE_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000003")
ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000004")
EVIDENCE_ID = UUID("00000000-0000-0000-0000-000000000005")
CHECKPOINT_ID = UUID("00000000-0000-0000-0000-000000000006")


def _snapshot(*, reverse_constraints: bool = False) -> WorkContextSnapshotV1:
    constraints = (
        ContextTextItem(key="security", text="Keep secrets out of logs", ordinal=1),
        ContextTextItem(key="runtime", text="Use the configured runtime", ordinal=0),
    )
    if reverse_constraints:
        constraints = tuple(reversed(constraints))
    return WorkContextSnapshotV1.create(
        objective="Ship the context contract",
        constraints=constraints,
        acceptance_criteria=(
            ContextTextItem(key="tests", text="All gates pass", ordinal=0),
        ),
        requested_outputs=(
            ContextTextItem(key="api", text="Versioned API", ordinal=0),
        ),
        clarification_decisions=(
            ClarificationDecision(
                key="scope",
                question="What is in scope?",
                answer="The control plane",
                answered_by="local_owner",
                ordinal=0,
            ),
        ),
        accepted_decisions=(
            AcceptedDecision(
                key="delivery",
                statement="Release only after the full gate",
                source="local_owner",
                ordinal=0,
            ),
        ),
        unresolved_questions=(
            UnresolvedQuestion(
                key="rollout",
                question="When is rollout scheduled?",
                blocking=False,
                ordinal=0,
            ),
        ),
        knowledge_refs=(
            KnowledgeRevisionRef(
                page_id=KNOWLEDGE_PAGE_ID,
                revision_id=KNOWLEDGE_REVISION_ID,
                revision_hash="a" * 64,
                section_key="overview",
                section_hash="b" * 64,
                source_ids=(KNOWLEDGE_SOURCE_ID,),
                ordinal=0,
            ),
        ),
        artifact_refs=(WorkReference(ref_id=ARTIFACT_ID, ref_type="artifact", ordinal=0),),
        evidence_refs=(WorkReference(ref_id=EVIDENCE_ID, ref_type="evidence", ordinal=0),),
        checkpoint_refs=(WorkReference(ref_id=CHECKPOINT_ID, ref_type="checkpoint", ordinal=0),),
    )


def _revision(
    *,
    context_id,
    planning_request_id,
    workspace_id=None,
    revision_number=1,
    reason=WorkContextRevisionReason.REQUEST_CREATED,
    snapshot=None,
    brief_request_id=None,
) -> WorkContextRevision:
    return WorkContextRevision.create(
        revision_id=uuid4(),
        work_context_id=context_id,
        workspace_id=workspace_id or uuid4(),
        revision_number=revision_number,
        brief_planning_request_id=brief_request_id or planning_request_id,
        brief_revision=1,
        proposal_revision_id=None,
        plan_snapshot_id=None,
        reason=reason,
        snapshot=snapshot or _snapshot(),
        created_by="local_owner",
        created_at=NOW,
    )


def test_work_context_create_requires_revision_one_and_matching_brief_request() -> None:
    context_id = uuid4()
    planning_request_id = uuid4()
    revision = _revision(
        context_id=context_id,
        planning_request_id=planning_request_id,
    )
    context = WorkContext.create(
        work_context_id=context_id,
        workspace_id=revision.workspace_id,
        planning_request_id=planning_request_id,
        first_revision=revision,
        created_at=NOW,
    )

    assert context.status is WorkContextStatus.ACTIVE
    assert context.current_revision_id == revision.revision_id
    assert context.current_revision_number == 1
    assert context.version == 1
    assert revision.content_hash == revision.snapshot.content_hash

    with pytest.raises(ValidationError, match="brief planning request"):
        WorkContext.create(
            work_context_id=context_id,
            workspace_id=revision.workspace_id,
            planning_request_id=planning_request_id,
            first_revision=_revision(
                context_id=context_id,
                planning_request_id=planning_request_id,
                workspace_id=revision.workspace_id,
                brief_request_id=uuid4(),
            ),
            created_at=NOW,
        )


def test_revision_hash_is_canonical_and_old_revision_is_immutable() -> None:
    first = _snapshot()
    reordered = _snapshot(reverse_constraints=True)
    assert first.canonical_bytes() == reordered.canonical_bytes()
    assert first.content_hash == reordered.content_hash

    with pytest.raises(FrozenInstanceError):
        first.objective = "mutated"  # type: ignore[misc]


def test_current_pointer_only_moves_forward_and_archived_context_cannot_revise() -> None:
    context_id = uuid4()
    planning_request_id = uuid4()
    first = _revision(context_id=context_id, planning_request_id=planning_request_id)
    context = WorkContext.create(
        work_context_id=context_id,
        workspace_id=first.workspace_id,
        planning_request_id=planning_request_id,
        first_revision=first,
        created_at=NOW,
    )
    second = WorkContextRevision.create(
        revision_id=uuid4(),
        work_context_id=context_id,
        workspace_id=first.workspace_id,
        revision_number=2,
        brief_planning_request_id=planning_request_id,
        brief_revision=2,
        proposal_revision_id=None,
        plan_snapshot_id=None,
        reason=WorkContextRevisionReason.CLARIFICATION_ACCEPTED,
        snapshot=_snapshot(),
        created_by="local_owner",
        created_at=NOW,
    )

    context.append_revision(second, expected_version=1, now=NOW)
    assert context.version == 2
    assert context.current_revision_number == 2

    with pytest.raises(VersionConflict):
        context.append_revision(second, expected_version=1, now=NOW)
    context.archive(expected_version=2, now=NOW)
    with pytest.raises(InvalidTransition):
        context.append_revision(
            WorkContextRevision.create(
                revision_id=uuid4(),
                work_context_id=context_id,
                workspace_id=first.workspace_id,
                revision_number=3,
                brief_planning_request_id=planning_request_id,
                brief_revision=3,
                proposal_revision_id=None,
                plan_snapshot_id=None,
                reason=WorkContextRevisionReason.USER_INTERVENTION,
                snapshot=_snapshot(),
                created_by="local_owner",
                created_at=NOW,
            ),
            expected_version=3,
            now=NOW,
        )


def test_legacy_incomplete_is_readable_but_requires_explicit_completion() -> None:
    context_id = uuid4()
    planning_request_id = uuid4()
    first = _revision(
        context_id=context_id,
        planning_request_id=planning_request_id,
        reason=WorkContextRevisionReason.LEGACY_BACKFILL,
    )
    context = WorkContext.create_legacy(
        work_context_id=context_id,
        workspace_id=first.workspace_id,
        planning_request_id=planning_request_id,
        first_revision=first,
        created_at=NOW,
    )
    assert context.status is WorkContextStatus.LEGACY_INCOMPLETE
    assert context.can_create_consumer is False
    with pytest.raises(InvalidTransition):
        context.append_revision(first, expected_version=1, now=NOW)

    context.complete_legacy(
        WorkContextRevision.create(
            revision_id=uuid4(),
            work_context_id=context_id,
            workspace_id=first.workspace_id,
            revision_number=2,
            brief_planning_request_id=planning_request_id,
            brief_revision=1,
            proposal_revision_id=None,
            plan_snapshot_id=None,
            reason=WorkContextRevisionReason.LEGACY_BACKFILL,
            snapshot=_snapshot(),
            created_by="backfill",
            created_at=NOW,
        ),
        expected_version=1,
        now=NOW,
    )
    assert context.status is WorkContextStatus.ACTIVE
    assert context.can_create_consumer is True


def test_snapshot_rejects_raw_conversation_trace_and_body_fields() -> None:
    mapping = _snapshot().to_mapping()
    for forbidden in ("raw_conversation", "worker_trace", "body_markdown"):
        invalid = {**mapping, forbidden: "must not persist"}
        with pytest.raises(ValidationError):
            WorkContextSnapshotV1.from_mapping(invalid)
