from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID, uuid4

from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.planning import (
    PlanningBriefRevision,
    PlanningClarification,
)
from sidera_control_plane.domain.planning_proposals import ProposalRevision
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
    WorkReference,
)


class WorkContextRevisionFactory:
    """Build immutable context revisions from typed planning records."""

    def create(
        self,
        *,
        work_context_id: UUID,
        workspace_id: UUID,
        brief: PlanningBriefRevision,
        revision_number: int,
        reason: WorkContextRevisionReason,
        actor: str,
        created_at: datetime,
        base_snapshot: WorkContextSnapshotV1 | None = None,
        clarification_decisions: Sequence[ClarificationDecision] | None = None,
        accepted_decisions: Sequence[AcceptedDecision] | None = None,
        unresolved_questions: Sequence[UnresolvedQuestion] | None = None,
        knowledge_refs: Sequence[KnowledgeRevisionRef] | None = None,
        artifact_refs: Sequence[WorkReference] | None = None,
        evidence_refs: Sequence[WorkReference] | None = None,
        checkpoint_refs: Sequence[WorkReference] | None = None,
        proposal_revision_id: UUID | None = None,
        plan_snapshot_id: UUID | None = None,
        revision_id: UUID | None = None,
    ) -> WorkContextRevision:
        snapshot = self.snapshot_from_brief(
            brief,
            base_snapshot=base_snapshot,
            clarification_decisions=clarification_decisions,
            accepted_decisions=accepted_decisions,
            unresolved_questions=unresolved_questions,
            knowledge_refs=knowledge_refs,
            artifact_refs=artifact_refs,
            evidence_refs=evidence_refs,
            checkpoint_refs=checkpoint_refs,
        )
        return WorkContextRevision.create(
            revision_id=revision_id or uuid4(),
            work_context_id=work_context_id,
            workspace_id=workspace_id,
            revision_number=revision_number,
            brief_planning_request_id=brief.planning_request_id,
            brief_revision=brief.revision,
            proposal_revision_id=proposal_revision_id,
            plan_snapshot_id=plan_snapshot_id,
            reason=reason,
            snapshot=snapshot,
            created_by=actor,
            created_at=created_at,
        )

    def snapshot_from_brief(
        self,
        brief: PlanningBriefRevision,
        *,
        base_snapshot: WorkContextSnapshotV1 | None = None,
        clarification_decisions: Sequence[ClarificationDecision] | None = None,
        accepted_decisions: Sequence[AcceptedDecision] | None = None,
        unresolved_questions: Sequence[UnresolvedQuestion] | None = None,
        knowledge_refs: Sequence[KnowledgeRevisionRef] | None = None,
        artifact_refs: Sequence[WorkReference] | None = None,
        evidence_refs: Sequence[WorkReference] | None = None,
        checkpoint_refs: Sequence[WorkReference] | None = None,
    ) -> WorkContextSnapshotV1:
        return WorkContextSnapshotV1.create(
            objective=brief.objective,
            constraints=tuple(
                ContextTextItem(
                    key=_stable_text_key("constraint", text),
                    text=text,
                    ordinal=ordinal,
                )
                for ordinal, text in enumerate(brief.constraints)
            ),
            acceptance_criteria=tuple(
                ContextTextItem(
                    key=f"acceptance_criterion:{ordinal:04d}",
                    text=text,
                    ordinal=ordinal,
                )
                for ordinal, text in enumerate(brief.acceptance_criteria)
            ),
            requested_outputs=tuple(
                ContextTextItem(
                    key=f"requested_output:{ordinal:04d}",
                    text=text,
                    ordinal=ordinal,
                )
                for ordinal, text in enumerate(brief.requested_outputs)
            ),
            clarification_decisions=_carry(
                base_snapshot,
                "clarification_decisions",
                clarification_decisions,
            ),
            accepted_decisions=_carry(
                base_snapshot, "accepted_decisions", accepted_decisions
            ),
            unresolved_questions=_carry(
                base_snapshot, "unresolved_questions", unresolved_questions
            ),
            knowledge_refs=_carry(base_snapshot, "knowledge_refs", knowledge_refs),
            artifact_refs=_carry(base_snapshot, "artifact_refs", artifact_refs),
            evidence_refs=_carry(base_snapshot, "evidence_refs", evidence_refs),
            checkpoint_refs=_carry(base_snapshot, "checkpoint_refs", checkpoint_refs),
        )

    @staticmethod
    def clarification_decisions(
        clarifications: Sequence[PlanningClarification],
        answers: Mapping[UUID, str],
        *,
        actor: str,
        ordinal_offset: int = 0,
    ) -> tuple[ClarificationDecision, ...]:
        decisions: list[ClarificationDecision] = []
        for ordinal, clarification in enumerate(clarifications, start=ordinal_offset):
            answer = answers.get(clarification.id)
            if answer is None:
                raise ValidationError("clarification decision answer is required")
            decisions.append(
                ClarificationDecision(
                    key=f"clarification:{clarification.id}",
                    question=clarification.question,
                    answer=answer,
                    answered_by=actor,
                    ordinal=ordinal,
                )
            )
        return tuple(decisions)

    @staticmethod
    def accepted_proposal_decisions(
        proposal: ProposalRevision,
        *,
        ordinal_offset: int = 0,
    ) -> tuple[AcceptedDecision, ...]:
        """Project proposal decisions and outputs into typed context facts."""

        summary = proposal.payload.decision_summary
        source = f"proposal:{proposal.id}"
        statements: list[tuple[str, str]] = [
            ("proposal:understanding", summary.understanding),
        ]
        statements.extend(
            (f"proposal:assumption:{ordinal:04d}", assumption)
            for ordinal, assumption in enumerate(summary.assumptions)
        )
        statements.extend(
            (f"proposal:scope_include:{ordinal:04d}", scope)
            for ordinal, scope in enumerate(summary.scope_includes)
        )
        statements.extend(
            (f"proposal:scope_exclude:{ordinal:04d}", scope)
            for ordinal, scope in enumerate(summary.scope_excludes)
        )
        statements.extend(
            (
                f"proposal:output:{item.key}:{ordinal:04d}",
                f"{item.key}: {output}",
            )
            for item in proposal.payload.subtasks
            for ordinal, output in enumerate(item.expected_outputs)
        )
        return tuple(
            AcceptedDecision(
                key=key,
                statement=statement,
                source=source,
                ordinal=ordinal_offset + ordinal,
            )
            for ordinal, (key, statement) in enumerate(statements)
        )


def work_context_event(
    envelope: CommandEnvelope,
    *,
    context: WorkContext,
    revision: WorkContextRevision,
    event_type: str,
) -> EventData:
    if event_type not in {"WorkContextCreated", "WorkContextRevised"}:
        raise ValidationError("unsupported work context event type")
    payload: dict[str, object] = {
        "planning_request_id": str(context.planning_request_id),
        "revision_id": str(revision.revision_id),
        "revision_number": revision.revision_number,
        "brief_revision": revision.brief_revision,
        "reason": revision.reason.value,
        "content_hash": revision.content_hash,
    }
    if revision.proposal_revision_id is not None:
        payload["proposal_revision_id"] = str(revision.proposal_revision_id)
    if revision.plan_snapshot_id is not None:
        payload["plan_snapshot_id"] = str(revision.plan_snapshot_id)
    return EventData(
        id=uuid4(),
        workspace_id=envelope.workspace_id,
        aggregate_type="work_context",
        aggregate_id=context.id,
        aggregate_version=context.version,
        event_type=event_type,
        payload=payload,
        actor_type=envelope.actor_type,
        actor_id=envelope.actor_id,
        causation_id=envelope.id,
        correlation_id=envelope.correlation_id,
    )


def work_context_bound_event(
    envelope: CommandEnvelope,
    *,
    context: WorkContext,
    revision: WorkContextRevision,
    root_task_id: UUID,
) -> EventData:
    """Publish binding as a separate one-shot sub-aggregate event.

    The event store reserves one aggregate version per event. The revision and
    binding are committed together, but the binding notification therefore
    uses its own aggregate identity while retaining the WorkContext ID.
    """

    return EventData(
        id=uuid4(),
        workspace_id=envelope.workspace_id,
        aggregate_type="work_context_binding",
        aggregate_id=context.id,
        aggregate_version=1,
        event_type="WorkContextBoundToTask",
        payload={
            "work_context_id": str(context.id),
            "planning_request_id": str(context.planning_request_id),
            "root_task_id": str(root_task_id),
            "revision_id": str(revision.revision_id),
            "revision_number": revision.revision_number,
            "context_version": context.version,
        },
        actor_type=envelope.actor_type,
        actor_id=envelope.actor_id,
        causation_id=envelope.id,
        correlation_id=envelope.correlation_id,
    )


def _carry[T](
    base_snapshot: WorkContextSnapshotV1 | None,
    field_name: str,
    value: Sequence[T] | None,
) -> tuple[T, ...]:
    if value is not None:
        return tuple(value)
    if base_snapshot is None:
        return ()
    return tuple(getattr(base_snapshot, field_name))


def _stable_text_key(prefix: str, text: str) -> str:
    digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"
