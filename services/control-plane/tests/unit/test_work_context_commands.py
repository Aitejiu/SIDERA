from datetime import UTC, datetime
from uuid import uuid4

from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
    WorkContextSemanticPatch,
)
from sidera_control_plane.domain.planning import PlanningBriefRevision
from sidera_control_plane.domain.work_context import (
    AcceptedDecision,
    ClarificationDecision,
    UnresolvedQuestion,
    WorkContextRevisionReason,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _brief() -> PlanningBriefRevision:
    return PlanningBriefRevision.create(
        planning_request_id=uuid4(),
        revision=1,
        objective="Implement the work context lifecycle",
        constraints=("Keep the snapshot immutable", "Do not persist chat"),
        acceptance_criteria=("The request is queued",),
        context_refs=("conversation:ignored-by-context",),
        requested_outputs=("proposal",),
        user_feedback=None,
        source_correlation=uuid4(),
        created_by="brief-author",
        created_at=NOW,
    )


def test_revision_factory_builds_typed_snapshot_without_raw_source_ref() -> None:
    brief = _brief()
    context_id = uuid4()
    factory = WorkContextRevisionFactory()

    revision = factory.create(
        work_context_id=context_id,
        workspace_id=uuid4(),
        brief=brief,
        revision_number=1,
        reason=WorkContextRevisionReason.REQUEST_CREATED,
        actor="envelope-actor",
        created_at=NOW,
    )

    assert revision.brief_planning_request_id == brief.planning_request_id
    assert revision.snapshot.objective == brief.objective
    assert {item.text for item in revision.snapshot.constraints} == set(brief.constraints)
    assert revision.snapshot.acceptance_criteria[0].text == "The request is queued"
    assert revision.snapshot.requested_outputs[0].text == "proposal"
    assert revision.snapshot.clarification_decisions == ()
    assert revision.created_by == "envelope-actor"
    assert "source_ref" not in revision.snapshot.to_mapping()
    assert "conversation:ignored-by-context" not in revision.snapshot.canonical_bytes().decode()


def test_semantic_patch_replaces_declared_text_and_appends_typed_facts() -> None:
    brief = _brief()
    base = WorkContextRevisionFactory().create(
        work_context_id=uuid4(),
        workspace_id=uuid4(),
        brief=brief,
        revision_number=1,
        reason=WorkContextRevisionReason.REQUEST_CREATED,
        actor="operator",
        created_at=NOW,
    ).snapshot
    decision = {
        "key": "decision:scope",
        "statement": "Keep the scope inside the control plane",
        "source": "operator",
        "ordinal": 0,
    }
    question = {
        "key": "question:budget",
        "question": "What is the budget?",
        "blocking": False,
        "ordinal": 0,
    }

    patched = WorkContextRevisionFactory.snapshot_from_semantic_patch(
        base,
        WorkContextSemanticPatch(
            constraints=("Only typed context",),
            accepted_decisions=(AcceptedDecision.from_mapping(decision),),
            unresolved_questions=(UnresolvedQuestion.from_mapping(question),),
        ),
    )

    assert [item.text for item in patched.constraints] == ["Only typed context"]
    assert patched.accepted_decisions[-1].key == "decision:scope"
    assert patched.unresolved_questions[-1].key == "question:budget"
    assert patched.content_hash != base.content_hash


def test_revision_factory_appends_typed_clarification_decisions_to_a_new_snapshot() -> None:
    brief = _brief()
    decision = ClarificationDecision(
        key="clarification:adapter",
        question="Which adapter should run?",
        answer="opencode",
        answered_by="user:1",
        ordinal=0,
    )

    revision = WorkContextRevisionFactory().create(
        work_context_id=uuid4(),
        workspace_id=uuid4(),
        brief=PlanningBriefRevision.create(
            planning_request_id=brief.planning_request_id,
            revision=2,
            objective=brief.objective,
            constraints=brief.constraints,
            acceptance_criteria=brief.acceptance_criteria,
            context_refs=brief.context_refs,
            requested_outputs=brief.requested_outputs,
            user_feedback="clarification answers: adapter=opencode",
            source_correlation=uuid4(),
            created_by="operator",
            created_at=NOW,
        ),
        revision_number=2,
        reason=WorkContextRevisionReason.CLARIFICATION_ACCEPTED,
        actor="user:1",
        created_at=NOW,
        clarification_decisions=(decision,),
    )

    assert revision.snapshot.clarification_decisions == (decision,)
    assert revision.reason is WorkContextRevisionReason.CLARIFICATION_ACCEPTED
