from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import (
    KnowledgeRefInvalid,
    ValidationError,
    WorkContextArchived,
    WorkContextBaseMismatch,
    WorkContextNotArchivable,
    WorkContextNotFound,
    WorkContextVersionConflict,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.planning import (
    RUN_TERMINAL_STATUSES,
    PlanningBriefRevision,
    PlanningClarification,
    PlanningRequestStatus,
)
from sidera_control_plane.domain.planning_proposals import ProposalRevision
from sidera_control_plane.domain.task import TERMINAL_TASK_STATUSES
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


@dataclass(frozen=True, slots=True)
class WorkContextSemanticPatch:
    """Typed changes allowed by an explicit context revision command."""

    objective: str | None = None
    constraints: tuple[str, ...] | None = None
    acceptance_criteria: tuple[str, ...] | None = None
    requested_outputs: tuple[str, ...] | None = None
    accepted_decisions: tuple[AcceptedDecision, ...] = ()
    unresolved_questions: tuple[UnresolvedQuestion, ...] = ()
    artifact_refs: tuple[WorkReference, ...] = ()
    evidence_refs: tuple[WorkReference, ...] = ()
    checkpoint_refs: tuple[WorkReference, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "constraints",
            "acceptance_criteria",
            "requested_outputs",
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            normalized = tuple(value)
            if any(not isinstance(item, str) or not item.strip() for item in normalized):
                raise ValidationError(f"semantic patch {field_name} must contain text")
            object.__setattr__(self, field_name, normalized)
        if self.objective is not None:
            if not isinstance(self.objective, str) or not self.objective.strip():
                raise ValidationError("semantic patch objective must contain text")
            object.__setattr__(self, "objective", self.objective.strip())
        for field_name, expected in (
            ("accepted_decisions", AcceptedDecision),
            ("unresolved_questions", UnresolvedQuestion),
            ("artifact_refs", WorkReference),
            ("evidence_refs", WorkReference),
            ("checkpoint_refs", WorkReference),
        ):
            value = tuple(getattr(self, field_name))
            if any(not isinstance(item, expected) for item in value):
                raise ValidationError(f"semantic patch {field_name} is not typed")
            object.__setattr__(self, field_name, value)

    @property
    def has_changes(self) -> bool:
        return any(
            value is not None and value != ()
            for value in (
                self.objective,
                self.constraints,
                self.acceptance_criteria,
                self.requested_outputs,
                self.accepted_decisions,
                self.unresolved_questions,
                self.artifact_refs,
                self.evidence_refs,
                self.checkpoint_refs,
            )
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
        missing_fact_codes: Sequence[str] | None = None,
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
            missing_fact_codes=missing_fact_codes,
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
        missing_fact_codes: Sequence[str] | None = None,
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
            missing_fact_codes=_carry(
                base_snapshot, "missing_fact_codes", missing_fact_codes
            ),
        )

    @staticmethod
    def snapshot_from_semantic_patch(
        base_snapshot: WorkContextSnapshotV1,
        patch: WorkContextSemanticPatch,
    ) -> WorkContextSnapshotV1:
        if not patch.has_changes:
            raise ValidationError("semantic patch must contain at least one change")
        return WorkContextSnapshotV1.create(
            objective=patch.objective
            if patch.objective is not None
            else base_snapshot.objective,
            constraints=(
                _context_text_items("constraint", patch.constraints)
                if patch.constraints is not None
                else base_snapshot.constraints
            ),
            acceptance_criteria=(
                _context_text_items("acceptance_criterion", patch.acceptance_criteria)
                if patch.acceptance_criteria is not None
                else base_snapshot.acceptance_criteria
            ),
            requested_outputs=(
                _context_text_items("requested_output", patch.requested_outputs)
                if patch.requested_outputs is not None
                else base_snapshot.requested_outputs
            ),
            clarification_decisions=base_snapshot.clarification_decisions,
            accepted_decisions=(
                *base_snapshot.accepted_decisions,
                *patch.accepted_decisions,
            ),
            unresolved_questions=(
                *base_snapshot.unresolved_questions,
                *patch.unresolved_questions,
            ),
            knowledge_refs=base_snapshot.knowledge_refs,
            artifact_refs=(*base_snapshot.artifact_refs, *patch.artifact_refs),
            evidence_refs=(*base_snapshot.evidence_refs, *patch.evidence_refs),
            checkpoint_refs=(*base_snapshot.checkpoint_refs, *patch.checkpoint_refs),
            missing_fact_codes=base_snapshot.missing_fact_codes,
        )

    @staticmethod
    def revision_from_snapshot(
        *,
        work_context_id: UUID,
        workspace_id: UUID,
        planning_request_id: UUID,
        brief_revision: int,
        revision_number: int,
        reason: WorkContextRevisionReason,
        snapshot: WorkContextSnapshotV1,
        actor: str,
        created_at: datetime,
        proposal_revision_id: UUID | None = None,
        plan_snapshot_id: UUID | None = None,
        revision_id: UUID | None = None,
    ) -> WorkContextRevision:
        return WorkContextRevision.create(
            revision_id=revision_id or uuid4(),
            work_context_id=work_context_id,
            workspace_id=workspace_id,
            revision_number=revision_number,
            brief_planning_request_id=planning_request_id,
            brief_revision=brief_revision,
            proposal_revision_id=proposal_revision_id,
            plan_snapshot_id=plan_snapshot_id,
            reason=reason,
            snapshot=snapshot,
            created_by=actor,
            created_at=created_at,
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
    selection_reason: str | None = None,
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
    if selection_reason is not None:
        payload["selection_reason"] = selection_reason
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


def work_context_archived_event(
    envelope: CommandEnvelope,
    *,
    context: WorkContext,
    revision: WorkContextRevision,
) -> EventData:
    return EventData(
        id=uuid4(),
        workspace_id=envelope.workspace_id,
        aggregate_type="work_context",
        aggregate_id=context.id,
        aggregate_version=context.version,
        event_type="WorkContextArchived",
        payload={
            "planning_request_id": str(context.planning_request_id),
            "revision_id": str(revision.revision_id),
            "revision_number": revision.revision_number,
            "content_hash": revision.content_hash,
            "status": context.status.value,
        },
        actor_type=envelope.actor_type,
        actor_id=envelope.actor_id,
        causation_id=envelope.id,
        correlation_id=envelope.correlation_id,
    )


class WorkContextCommandHandler:
    command_types = frozenset(
        {"ReviseWorkContext", "AttachKnowledgeRevision", "ArchiveWorkContext"}
    )

    def __init__(
        self,
        *,
        local_actor_type: str,
        local_actor_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._local_actor_type = local_actor_type
        self._local_actor_id = local_actor_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._factory = WorkContextRevisionFactory()

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.command_type == "ReviseWorkContext":
            return self._revise(uow, envelope)
        if envelope.command_type == "AttachKnowledgeRevision":
            return self._attach_knowledge(uow, envelope)
        if envelope.command_type == "ArchiveWorkContext":
            return self._archive(uow, envelope)
        raise ValidationError(f"unsupported work context command: {envelope.command_type}")

    def _revise(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        context, current_revision = self._load_context(uow, envelope)
        self._assert_active(context)
        self._assert_base(envelope, current_revision, prefix="base")
        request = uow.planning_requests.get(
            context.planning_request_id, envelope.workspace_id
        )
        if request is None:
            raise WorkContextNotFound("planning request for work context not found")
        brief = _find_brief(request.briefs, current_revision.brief_revision)
        patch = _semantic_patch(envelope.payload.get("patch"))
        snapshot = self._factory.snapshot_from_semantic_patch(
            current_revision.snapshot, patch
        )
        previous_version = context.version
        now = self._clock()
        revision = self._factory.revision_from_snapshot(
            work_context_id=context.id,
            workspace_id=envelope.workspace_id,
            planning_request_id=context.planning_request_id,
            brief_revision=brief.revision,
            revision_number=context.current_revision_number + 1,
            reason=_revision_reason(envelope.payload.get("reason")),
            snapshot=snapshot,
            actor=envelope.actor_id,
            created_at=now,
        )
        context.append_revision(revision, expected_version=previous_version, now=now)
        uow.work_contexts.append_revision(
            context, revision, expected_version=previous_version
        )
        event = work_context_event(
            envelope,
            context=context,
            revision=revision,
            event_type="WorkContextRevised",
        )
        uow.events.append_with_outbox(event)
        return _revision_result(envelope, context, revision, event)

    def _attach_knowledge(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        context, current_revision = self._load_context(uow, envelope)
        self._assert_active(context)
        request = uow.planning_requests.get(
            context.planning_request_id, envelope.workspace_id
        )
        if request is None:
            raise WorkContextNotFound("planning request for work context not found")
        brief = _find_brief(request.briefs, current_revision.brief_revision)
        knowledge_ref = _knowledge_ref(envelope.payload.get("knowledge_ref"))
        if knowledge_ref in current_revision.snapshot.knowledge_refs:
            raise KnowledgeRefInvalid("knowledge revision is already attached")
        selection_reason = _required_text(
            envelope.payload.get("selection_reason"), "selection_reason"
        )
        selection = AcceptedDecision(
            key=(
                f"knowledge:selection:{knowledge_ref.revision_id}:"
                f"{knowledge_ref.section_key}"
            ),
            statement=selection_reason,
            source=f"knowledge:{knowledge_ref.revision_id}",
            ordinal=len(current_revision.snapshot.accepted_decisions),
        )
        snapshot = self._factory.snapshot_from_semantic_patch(
            current_revision.snapshot,
            WorkContextSemanticPatch(
                accepted_decisions=(selection,),
            ),
        )
        snapshot = WorkContextSnapshotV1.create(
            objective=snapshot.objective,
            constraints=snapshot.constraints,
            acceptance_criteria=snapshot.acceptance_criteria,
            requested_outputs=snapshot.requested_outputs,
            clarification_decisions=snapshot.clarification_decisions,
            accepted_decisions=snapshot.accepted_decisions,
            unresolved_questions=snapshot.unresolved_questions,
            knowledge_refs=(*snapshot.knowledge_refs, knowledge_ref),
            artifact_refs=snapshot.artifact_refs,
            evidence_refs=snapshot.evidence_refs,
            checkpoint_refs=snapshot.checkpoint_refs,
        )
        previous_version = context.version
        now = self._clock()
        revision = self._factory.revision_from_snapshot(
            work_context_id=context.id,
            workspace_id=envelope.workspace_id,
            planning_request_id=context.planning_request_id,
            brief_revision=brief.revision,
            revision_number=context.current_revision_number + 1,
            reason=WorkContextRevisionReason.KNOWLEDGE_ATTACHED,
            snapshot=snapshot,
            actor=envelope.actor_id,
            created_at=now,
        )
        context.append_revision(revision, expected_version=previous_version, now=now)
        uow.work_contexts.append_revision(
            context, revision, expected_version=previous_version
        )
        event = work_context_event(
            envelope,
            context=context,
            revision=revision,
            event_type="WorkContextRevised",
            selection_reason=selection_reason,
        )
        uow.events.append_with_outbox(event)
        return _revision_result(envelope, context, revision, event)

    def _archive(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        context, current_revision = self._load_context(uow, envelope)
        if context.status.value == "archived":
            raise WorkContextArchived("work context is already archived")
        self._assert_base(envelope, current_revision, prefix="current")
        if not (
            envelope.actor_type == "system"
            or (
                envelope.actor_type == self._local_actor_type
                and envelope.actor_id == self._local_actor_id
            )
        ):
            raise WorkContextNotArchivable(
                "only the configured local owner or system can archive a work context"
            )
        request = uow.planning_requests.get(
            context.planning_request_id, envelope.workspace_id, for_update=True
        )
        if request is None:
            raise WorkContextNotFound("planning request for work context not found")
        if request.status is not PlanningRequestStatus.CANCELLED:
            self._assert_terminal_for_archive(uow, context, request)
        previous_version = context.version
        now = self._clock()
        context.archive(expected_version=previous_version, now=now)
        uow.work_contexts.archive(context, expected_version=previous_version)
        event = work_context_archived_event(
            envelope,
            context=context,
            revision=current_revision,
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=context.id,
            aggregate_version=context.version,
            event_ids=(event.id,),
            data={
                "work_context_id": str(context.id),
                "status": context.status.value,
                "work_context_revision": context.current_revision_number,
                "work_context_content_hash": current_revision.content_hash,
            },
        )

    def _load_context(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> tuple[WorkContext, WorkContextRevision]:
        if envelope.target_id is None or envelope.expected_version is None:
            raise ValidationError("work context command requires target_id and expected_version")
        context = uow.work_contexts.get(
            envelope.target_id, envelope.workspace_id, for_update=True
        )
        if context is None:
            raise WorkContextNotFound("work context not found")
        if context.version != envelope.expected_version:
            raise WorkContextVersionConflict(
                envelope.expected_version, context.version
            )
        revision = uow.work_contexts.get_revision(
            context.current_revision_id, envelope.workspace_id
        )
        if revision is None:
            raise WorkContextNotFound("work context current revision not found")
        return context, revision

    @staticmethod
    def _assert_active(context: WorkContext) -> None:
        if context.status.value == "archived":
            raise WorkContextArchived("work context is archived")

    @staticmethod
    def _assert_base(
        envelope: CommandEnvelope,
        current_revision: WorkContextRevision,
        *,
        prefix: str,
    ) -> None:
        revision_key = f"{prefix}_revision"
        hash_key = f"{prefix}_content_hash"
        expected_revision = _positive_int(envelope.payload.get(revision_key), revision_key)
        expected_hash = _required_text(envelope.payload.get(hash_key), hash_key)
        if (
            expected_revision != current_revision.revision_number
            or expected_hash != current_revision.content_hash
        ):
            raise WorkContextBaseMismatch(
                "work context base revision or hash does not match",
                details={
                    "expected_revision": expected_revision,
                    "expected_content_hash": expected_hash,
                    "current_revision": current_revision.revision_number,
                    "current_content_hash": current_revision.content_hash,
                },
            )

    @staticmethod
    def _assert_terminal_for_archive(
        uow: ControlPlaneUnitOfWork,
        context: WorkContext,
        request: Any,
    ) -> None:
        if context.root_task_id is None:
            raise WorkContextNotArchivable("work context has no terminal root task")
        root_task = uow.tasks.get(
            context.root_task_id, context.workspace_id, for_update=False
        )
        if root_task is None or root_task.status not in TERMINAL_TASK_STATUSES:
            raise WorkContextNotArchivable("root task is not terminal")
        if any(run.status not in RUN_TERMINAL_STATUSES for run in request.runs):
            raise WorkContextNotArchivable("planning run is still active")
        snapshot = uow.plans.get_current_for_root(
            context.root_task_id, context.workspace_id
        )
        task_ids = [context.root_task_id]
        if snapshot is not None:
            task_ids.extend(
                item.task_id
                for item in snapshot.subtasks
                if item.task_id is not None
            )
        if any(uow.executions.has_live_for_task(task_id) for task_id in task_ids):
            raise WorkContextNotArchivable("execution is still active")


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


def _context_text_items(
    prefix: str, values: Sequence[str] | None
) -> tuple[ContextTextItem, ...]:
    if values is None:
        return ()
    return tuple(
        ContextTextItem(
            key=(
                _stable_text_key(prefix, text)
                if prefix == "constraint"
                else f"{prefix}:{ordinal:04d}"
            ),
            text=text,
            ordinal=ordinal,
        )
        for ordinal, text in enumerate(values)
    )


def _revision_result(
    envelope: CommandEnvelope,
    context: WorkContext,
    revision: WorkContextRevision,
    event: EventData,
) -> CommandResult:
    return CommandResult(
        command_id=envelope.id,
        aggregate_id=context.id,
        aggregate_version=context.version,
        event_ids=(event.id,),
        data={
            "work_context_id": str(context.id),
            "work_context_revision": revision.revision_number,
            "work_context_content_hash": revision.content_hash,
        },
    )


def _find_brief(
    briefs: Sequence[PlanningBriefRevision], revision_number: int
) -> PlanningBriefRevision:
    for brief in briefs:
        if brief.revision == revision_number:
            return brief
    raise WorkContextNotFound("work context brief revision not found")


def _revision_reason(value: object) -> WorkContextRevisionReason:
    try:
        reason = WorkContextRevisionReason(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError("work context revision reason is invalid") from exc
    if reason not in {
        WorkContextRevisionReason.REPLANNED,
        WorkContextRevisionReason.USER_INTERVENTION,
    }:
        raise ValidationError("work context revision reason is not user-selectable")
    return reason


def _semantic_patch(value: object) -> WorkContextSemanticPatch:
    raw = _mapping(value, "patch")
    patch = WorkContextSemanticPatch(
        objective=_optional_text(raw.get("objective"), "objective"),
        constraints=_optional_text_tuple(raw.get("constraints"), "constraints"),
        acceptance_criteria=_optional_text_tuple(
            raw.get("acceptance_criteria"), "acceptance_criteria"
        ),
        requested_outputs=_optional_text_tuple(
            raw.get("requested_outputs"), "requested_outputs"
        ),
        accepted_decisions=tuple(
            AcceptedDecision.from_mapping(_mapping(item, "accepted decision"))
            for item in _sequence(raw.get("accepted_decisions", ()), "accepted_decisions")
        ),
        unresolved_questions=tuple(
            UnresolvedQuestion.from_mapping(_mapping(item, "unresolved question"))
            for item in _sequence(
                raw.get("unresolved_questions", ()), "unresolved_questions"
            )
        ),
        artifact_refs=tuple(
            WorkReference.from_mapping(_mapping(item, "artifact ref"))
            for item in _sequence(raw.get("artifact_refs", ()), "artifact_refs")
        ),
        evidence_refs=tuple(
            WorkReference.from_mapping(_mapping(item, "evidence ref"))
            for item in _sequence(raw.get("evidence_refs", ()), "evidence_refs")
        ),
        checkpoint_refs=tuple(
            WorkReference.from_mapping(_mapping(item, "checkpoint ref"))
            for item in _sequence(raw.get("checkpoint_refs", ()), "checkpoint_refs")
        ),
    )
    if not patch.has_changes:
        raise ValidationError("semantic patch must contain at least one change")
    return patch


def _knowledge_ref(value: object) -> KnowledgeRevisionRef:
    try:
        return KnowledgeRevisionRef.from_mapping(_mapping(value, "knowledge_ref"))
    except (TypeError, ValueError, ValidationError) as exc:
        details = exc.details if isinstance(exc, ValidationError) else {}
        raise KnowledgeRefInvalid("knowledge_ref is invalid", details=details) from exc


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field_name} must be an object")
    return value


def _sequence(value: object, field_name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValidationError(f"{field_name} must be an array")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _optional_text_tuple(value: object, field_name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(
        _required_text(item, f"{field_name} item")
        for item in _sequence(value, field_name)
    )


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} is required")
    return value.strip()


def _positive_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{field_name} must be positive")
    return value


def _stable_text_key(prefix: str, text: str) -> str:
    digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"
