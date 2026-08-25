from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.planning import (
    PlanningBriefRevision,
    PlanningClarification,
    PlanningRequestStatus,
)
from sidera_control_plane.domain.planning_proposals import ProposalRevision
from sidera_control_plane.domain.plans import PlanValidationError, TaskPlanSnapshot
from sidera_control_plane.domain.task import TERMINAL_TASK_STATUSES
from sidera_control_plane.domain.work_context import (
    AcceptedDecision,
    ClarificationDecision,
    UnresolvedQuestion,
    WorkContext,
    WorkContextRevisionReason,
    WorkContextStatus,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    PlanningBriefRevisionModel,
    PlanningClarificationModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    PlanSnapshotModel,
    ProposalMaterializationModel,
    TaskModel,
    WorkContextModel,
)
from sidera_control_plane.persistence.planning_repository import (
    _to_brief,
    _to_clarification,
    _to_proposal,
)
from sidera_control_plane.persistence.work_context_repository import (
    WorkContextRepository,
)


@dataclass(frozen=True, slots=True)
class BackfillSummary:
    examined: int
    created: int
    incomplete: int
    skipped: int
    failed: int
    last_planning_request_id: UUID | None

    def to_mapping(self) -> dict[str, object]:
        return {
            "examined": self.examined,
            "created": self.created,
            "incomplete": self.incomplete,
            "skipped": self.skipped,
            "failed": self.failed,
            "last_planning_request_id": (
                str(self.last_planning_request_id)
                if self.last_planning_request_id is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class _BackfillCandidate:
    brief: PlanningBriefRevision | None
    proposal: ProposalRevision | None
    materialization: ProposalMaterializationModel | None
    plan: TaskPlanSnapshot | None
    task: TaskModel | None
    clarifications: tuple[PlanningClarification, ...]
    missing_fact_codes: tuple[str, ...]

    @property
    def can_persist(self) -> bool:
        return self.brief is not None


class WorkContextBackfillService:
    """Rebuild missing WorkContexts from durable planning and task records."""

    def run(
        self,
        database: Database,
        workspace_id: UUID,
        *,
        batch_size: int = 100,
        dry_run: bool = False,
        after_planning_request_id: UUID | None = None,
    ) -> BackfillSummary:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")

        examined = 0
        created = 0
        incomplete = 0
        skipped = 0
        failed = 0
        cursor = after_planning_request_id
        last_planning_request_id: UUID | None = None

        while True:
            with database.session() as session:
                statement = (
                    select(PlanningRequestModel)
                    .where(PlanningRequestModel.workspace_id == workspace_id)
                    .order_by(PlanningRequestModel.id.asc())
                    .limit(batch_size)
                )
                if cursor is not None:
                    statement = statement.where(PlanningRequestModel.id > cursor)
                requests = session.scalars(statement).all()
                if not requests:
                    break

                batch_last_id = requests[-1].id
                examined += len(requests)
                last_planning_request_id = batch_last_id
                try:
                    batch_created, batch_incomplete, batch_skipped = (
                        self._process_batch(
                            session,
                            requests,
                            dry_run=dry_run,
                        )
                    )
                    if not dry_run:
                        session.commit()
                except (
                    KeyError,
                    PlanValidationError,
                    SQLAlchemyError,
                    TypeError,
                    ValidationError,
                    ValueError,
                ):
                    session.rollback()
                    failed += len(requests)
                    cursor = batch_last_id
                    continue

                created += batch_created
                incomplete += batch_incomplete
                skipped += batch_skipped
                cursor = batch_last_id

        return BackfillSummary(
            examined=examined,
            created=created,
            incomplete=incomplete,
            skipped=skipped,
            failed=failed,
            last_planning_request_id=last_planning_request_id,
        )

    def _process_batch(
        self,
        session: Session,
        requests: Sequence[PlanningRequestModel],
        *,
        dry_run: bool,
    ) -> tuple[int, int, int]:
        created = 0
        incomplete = 0
        skipped = 0
        for request in requests:
            existing = session.scalar(
                select(WorkContextModel.id).where(
                    WorkContextModel.workspace_id == request.workspace_id,
                    WorkContextModel.planning_request_id == request.id,
                )
            )
            if existing is not None:
                skipped += 1
                continue

            candidate = self._candidate(session, request)
            if candidate.missing_fact_codes:
                incomplete += 1
            if not candidate.can_persist:
                continue
            if not dry_run:
                self._persist(session, request, candidate)
            created += 1
        return created, incomplete, skipped

    def _candidate(
        self, session: Session, request: PlanningRequestModel
    ) -> _BackfillCandidate:
        brief = self._load_brief(session, request)
        if brief is None:
            return _BackfillCandidate(
                brief=None,
                proposal=None,
                materialization=None,
                plan=None,
                task=None,
                clarifications=(),
                missing_fact_codes=("brief_missing",),
            )

        materialization = session.scalar(
            select(ProposalMaterializationModel).where(
                ProposalMaterializationModel.workspace_id == request.workspace_id,
                ProposalMaterializationModel.planning_request_id == request.id,
            )
        )
        proposal = self._load_current_proposal(session, request)
        if proposal is None and materialization is not None:
            proposal_row = session.scalar(
                select(PlanningProposalRevisionModel).where(
                    PlanningProposalRevisionModel.id
                    == materialization.proposal_revision_id,
                    PlanningProposalRevisionModel.workspace_id
                    == request.workspace_id,
                )
            )
            if proposal_row is not None:
                proposal = _to_proposal(proposal_row)

        missing: set[str] = set()
        request_status = PlanningRequestStatus(request.status)
        if proposal is None and (
            request.current_proposal_revision is not None
            or request_status
            in {
                PlanningRequestStatus.PROPOSAL_READY,
                PlanningRequestStatus.MATERIALIZED,
            }
        ):
            missing.add("proposal_missing")

        plan: TaskPlanSnapshot | None = None
        task: TaskModel | None = None
        if request_status is PlanningRequestStatus.MATERIALIZED:
            if materialization is None:
                missing.add("materialization_missing")
            else:
                plan_row = session.scalar(
                    select(PlanSnapshotModel).where(
                        PlanSnapshotModel.id == materialization.plan_snapshot_id,
                        PlanSnapshotModel.workspace_id == request.workspace_id,
                    )
                )
                if plan_row is None:
                    missing.add("plan_snapshot_missing")
                else:
                    plan = TaskPlanSnapshot.from_payload(dict(plan_row.payload))
                task = session.scalar(
                    select(TaskModel).where(
                        TaskModel.id == materialization.root_task_id,
                        TaskModel.workspace_id == request.workspace_id,
                    )
                )
                if task is None:
                    missing.add("root_task_missing")

        clarifications = tuple(
            _to_clarification(row)
            for row in session.scalars(
                select(PlanningClarificationModel)
                .where(
                    PlanningClarificationModel.workspace_id == request.workspace_id,
                    PlanningClarificationModel.planning_request_id == request.id,
                )
                .order_by(PlanningClarificationModel.sequence.asc())
            ).all()
        )
        return _BackfillCandidate(
            brief=brief,
            proposal=proposal,
            materialization=materialization,
            plan=plan,
            task=task,
            clarifications=clarifications,
            missing_fact_codes=tuple(sorted(missing)),
        )

    @staticmethod
    def _load_brief(
        session: Session, request: PlanningRequestModel
    ) -> PlanningBriefRevision | None:
        statement = select(PlanningBriefRevisionModel).where(
            PlanningBriefRevisionModel.workspace_id == request.workspace_id,
            PlanningBriefRevisionModel.planning_request_id == request.id,
        )
        if request.current_brief_revision is not None:
            statement = statement.where(
                PlanningBriefRevisionModel.revision == request.current_brief_revision
            )
            row = session.scalar(statement)
            if row is not None:
                return _to_brief(row)
            statement = select(PlanningBriefRevisionModel).where(
                PlanningBriefRevisionModel.workspace_id == request.workspace_id,
                PlanningBriefRevisionModel.planning_request_id == request.id,
            )
        row = session.scalars(
            statement.order_by(PlanningBriefRevisionModel.revision.desc())
        ).first()
        return _to_brief(row) if row is not None else None

    @staticmethod
    def _load_current_proposal(
        session: Session, request: PlanningRequestModel
    ) -> ProposalRevision | None:
        if request.current_proposal_revision is None:
            return None
        row = session.scalar(
            select(PlanningProposalRevisionModel).where(
                PlanningProposalRevisionModel.workspace_id == request.workspace_id,
                PlanningProposalRevisionModel.planning_request_id == request.id,
                PlanningProposalRevisionModel.revision
                == request.current_proposal_revision,
            )
        )
        return _to_proposal(row) if row is not None else None

    def _persist(
        self,
        session: Session,
        request: PlanningRequestModel,
        candidate: _BackfillCandidate,
    ) -> None:
        assert candidate.brief is not None
        work_context_id = uuid4()
        revision = WorkContextRevisionFactory().create(
            work_context_id=work_context_id,
            workspace_id=request.workspace_id,
            brief=candidate.brief,
            revision_number=1,
            reason=WorkContextRevisionReason.LEGACY_BACKFILL,
            actor="legacy_backfill",
            created_at=candidate.brief.created_at,
            accepted_decisions=self._accepted_decisions(candidate),
            clarification_decisions=self._clarification_decisions(candidate),
            unresolved_questions=self._unresolved_questions(candidate),
            missing_fact_codes=candidate.missing_fact_codes,
            proposal_revision_id=(
                candidate.proposal.id if candidate.proposal is not None else None
            ),
            plan_snapshot_id=(
                candidate.plan.id if candidate.plan is not None else None
            ),
        )
        root_task_id = (
            candidate.materialization.root_task_id
            if candidate.materialization is not None and candidate.task is not None
            else None
        )
        status = WorkContextStatus.ACTIVE
        if candidate.missing_fact_codes:
            status = WorkContextStatus.LEGACY_INCOMPLETE
        elif candidate.task is not None and candidate.task.status in {
            item.value for item in TERMINAL_TASK_STATUSES
        }:
            status = WorkContextStatus.ARCHIVED
        context = WorkContext.create_backfilled(
            work_context_id=work_context_id,
            workspace_id=request.workspace_id,
            planning_request_id=request.id,
            root_task_id=root_task_id,
            first_revision=revision,
            status=status,
            created_at=request.created_at,
        )
        WorkContextRepository(session).add(context, revision)

    @staticmethod
    def _accepted_decisions(
        candidate: _BackfillCandidate,
    ) -> tuple[AcceptedDecision, ...]:
        decisions: list[AcceptedDecision] = []
        if candidate.proposal is not None:
            decisions.extend(
                WorkContextRevisionFactory.accepted_proposal_decisions(
                    candidate.proposal
                )
            )
        ordinal = len(decisions)
        if candidate.task is not None:
            decisions.append(
                AcceptedDecision(
                    key=f"task:{candidate.task.id}:title",
                    statement=candidate.task.title,
                    source=f"task:{candidate.task.id}",
                    ordinal=ordinal,
                )
            )
            ordinal += 1
            decisions.extend(
                AcceptedDecision(
                    key=f"task:{candidate.task.id}:acceptance:{index:04d}",
                    statement=criterion,
                    source=f"task:{candidate.task.id}",
                    ordinal=ordinal + index,
                )
                for index, criterion in enumerate(candidate.task.acceptance_criteria)
            )
            ordinal += len(candidate.task.acceptance_criteria)
        if candidate.plan is not None:
            decisions.extend(
                WorkContextBackfillService._plan_decisions(candidate.plan, ordinal)
            )
        return tuple(decisions)

    @staticmethod
    def _plan_decisions(
        plan: TaskPlanSnapshot, ordinal_offset: int
    ) -> tuple[AcceptedDecision, ...]:
        decisions: list[AcceptedDecision] = []
        decisions.append(
            AcceptedDecision(
                key=f"plan:{plan.id}:goal",
                statement=plan.goal_summary,
                source=f"plan:{plan.id}",
                ordinal=ordinal_offset,
            )
        )
        ordinal = ordinal_offset + 1
        items = (*plan.subtasks, *plan.work_items)
        for item in items:
            for output_index, output in enumerate(item.expected_outputs):
                decisions.append(
                    AcceptedDecision(
                        key=f"plan:{plan.id}:output:{item.key}:{output_index:04d}",
                        statement=f"{item.key}: {output}",
                        source=f"plan:{plan.id}",
                        ordinal=ordinal,
                    )
                )
                ordinal += 1
        return tuple(decisions)

    @staticmethod
    def _clarification_decisions(
        candidate: _BackfillCandidate,
    ) -> tuple[ClarificationDecision, ...]:
        decisions: list[ClarificationDecision] = []
        for item in candidate.clarifications:
            if item.answer is None:
                continue
            decisions.append(
                ClarificationDecision(
                    key=f"clarification:{item.id}",
                    question=item.question,
                    answer=item.answer,
                    answered_by=item.answered_by or "legacy_backfill",
                    ordinal=len(decisions),
                )
            )
        return tuple(decisions)

    @staticmethod
    def _unresolved_questions(
        candidate: _BackfillCandidate,
    ) -> tuple[UnresolvedQuestion, ...]:
        return tuple(
            UnresolvedQuestion(
                key=f"clarification:{item.id}",
                question=item.question,
                blocking=item.blocking,
                ordinal=index,
            )
            for index, item in enumerate(
                item for item in candidate.clarifications if item.answer is None
            )
        )


def _uuid_argument(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill missing WorkContext rows from durable legacy records."
    )
    parser.add_argument("--workspace-id", required=True, type=_uuid_argument)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--after-planning-request-id", type=_uuid_argument)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-write", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.dry_run and not args.confirm_write:
        parser.error("--confirm-write is required unless --dry-run is set")
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")

    settings = Settings()  # pyright: ignore[reportCallIssue]
    database = Database(settings.database_url)
    try:
        summary = WorkContextBackfillService().run(
            database,
            args.workspace_id,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            after_planning_request_id=args.after_planning_request_id,
        )
    finally:
        database.dispose()
    print(json.dumps(summary.to_mapping(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
