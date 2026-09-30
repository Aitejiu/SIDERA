from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from sidera_control_plane.application.result_queries import (
    ResultAcceptanceBasisSnapshot,
    ResultApplyFact,
    ResultArtifactFact,
    ResultCursor,
    ResultDetailSnapshot,
    ResultOperationFact,
    ResultPage,
    ResultPageMarker,
    ResultQueryStore,
    ResultRootSnapshot,
    ResultTaskFact,
)
from sidera_control_plane.domain.code_results import (
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.result_operations import (
    ResultOperationStatus,
    ReworkRecordV1,
    TaskResultHead,
)
from sidera_control_plane.domain.result_verification import (
    TaskAcceptanceDecisionV1,
    VerificationResultV1,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    CodeResultSnapshotModel,
    IntegrationCandidateModel,
    PlanSnapshotModel,
    ProjectionChangeModel,
    ResultApplyOperationModel,
    ResultOperationAttemptModel,
    ResultReworkRecordModel,
    ResultVerificationResultModel,
    RootTaskPlanHeadModel,
    TaskAcceptanceDecisionModel,
    TaskModel,
    TaskResultHeadModel,
)

_ACTIVE_OPERATION_STATUSES = (
    ResultOperationStatus.PENDING.value,
    ResultOperationStatus.RUNNING.value,
    ResultOperationStatus.BLOCKED.value,
    ResultOperationStatus.UNKNOWN.value,
)
_MAX_CURRENT_VERIFICATIONS = 256
_MAX_CURRENT_REWORK = 100
_MAX_APPLY_HISTORY = 100


class SqlAlchemyResultQueryStore(ResultQueryStore):
    """Read the result projection without exposing storage or repository paths."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def load_root(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        cursor: ResultCursor | None,
        limit: int,
    ) -> ResultRootSnapshot | None:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            task = session.scalar(
                select(TaskModel).where(
                    TaskModel.workspace_id == workspace_id,
                    TaskModel.id == root_task_id,
                )
            )
            if task is None:
                return None

            head = _head(
                session.scalar(
                    select(TaskResultHeadModel).where(
                        TaskResultHeadModel.workspace_id == workspace_id,
                        TaskResultHeadModel.root_task_id == root_task_id,
                    )
                )
            )
            plan = _current_plan(session, workspace_id, root_task_id)
            history = _candidate_history(
                session,
                workspace_id,
                root_task_id,
                cursor=cursor,
                limit=limit,
            )
            current_candidate = (
                _candidate_row(
                    session,
                    workspace_id,
                    root_task_id,
                    head.current_candidate_id,
                )
                if head is not None and head.current_candidate_id is not None
                else None
            )
            current_sources = (
                _source_results(session, workspace_id, current_candidate)
                if current_candidate is not None
                else ()
            )
            current_code_result = (
                _code_result(session, workspace_id, current_candidate)
                if current_candidate is not None
                else None
            )
            current_verifications = (
                _verification_results(
                    session,
                    workspace_id,
                    root_task_id,
                    current_candidate.id,
                    limit=_MAX_CURRENT_VERIFICATIONS,
                )
                if current_candidate is not None
                else ()
            )
            verification_operation_versions = _operation_versions(
                session,
                workspace_id,
                root_task_id,
                current_verifications,
            )
            current_rework = (
                _rework_records(
                    session,
                    workspace_id,
                    root_task_id,
                    current_candidate.id,
                    limit=_MAX_CURRENT_REWORK,
                )
                if current_candidate is not None
                else ()
            )
            replacement_result_ids = _replacement_result_ids(
                session,
                workspace_id,
                root_task_id,
                current_rework,
            )
            acceptance = (
                _acceptance_by_id(
                    session, workspace_id, head.current_acceptance_id
                )
                if head is not None and head.current_acceptance_id is not None
                else None
            )
            active_operation = _active_operation(
                session,
                workspace_id,
                root_task_id,
                head,
            )
            artifact_ids = _artifact_ids(
                (*current_sources, *(() if current_code_result is None else (current_code_result,))),
                current_verifications,
            )
            return ResultRootSnapshot(
                task=ResultTaskFact(
                    task_id=task.id,
                    workspace_id=task.workspace_id,
                    title=task.title,
                    status=task.status,
                    version=task.version,
                    updated_at=_as_utc(task.updated_at),
                ),
                head=head,
                plan=plan,
                current_candidate=current_candidate,
                current_sources=current_sources,
                current_code_result=current_code_result,
                current_verifications=current_verifications,
                verification_operation_versions=verification_operation_versions,
                current_rework=current_rework,
                replacement_result_ids=replacement_result_ids,
                acceptance=acceptance,
                active_operation=active_operation,
                candidate_history=history,
                artifacts=_artifacts(session, workspace_id, artifact_ids),
                last_change_cursor=_last_change_cursor(session, workspace_id),
                as_of=datetime.now(UTC),
            )

    def load_detail(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        candidate_id: UUID,
        *,
        verification_cursor: ResultCursor | None,
        rework_cursor: ResultCursor | None,
        limit: int,
    ) -> ResultDetailSnapshot | None:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            task = session.scalar(
                select(TaskModel).where(
                    TaskModel.workspace_id == workspace_id,
                    TaskModel.id == root_task_id,
                )
            )
            candidate = _candidate_row(
                session, workspace_id, root_task_id, candidate_id
            )
            if task is None or candidate is None:
                return None
            head = _head(
                session.scalar(
                    select(TaskResultHeadModel).where(
                        TaskResultHeadModel.workspace_id == workspace_id,
                        TaskResultHeadModel.root_task_id == root_task_id,
                    )
                )
            )
            plan = _plan_for_candidate(session, workspace_id, candidate)
            source_results = _source_results(session, workspace_id, candidate)
            code_result = _code_result(session, workspace_id, candidate)
            verifications = _verification_page(
                session,
                workspace_id,
                root_task_id,
                candidate_id,
                cursor=verification_cursor,
                limit=limit,
            )
            rework = _rework_page(
                session,
                workspace_id,
                root_task_id,
                candidate_id,
                cursor=rework_cursor,
                limit=limit,
            )
            acceptance = _acceptance_for_candidate(
                session, workspace_id, root_task_id, candidate_id
            )
            apply_operations = _apply_history(
                session, workspace_id, root_task_id, candidate_id
            )
            all_verifications = _verification_results(
                session,
                workspace_id,
                root_task_id,
                candidate_id,
                limit=_MAX_CURRENT_VERIFICATIONS,
            )
            artifact_ids = _artifact_ids(
                (*source_results, *(() if code_result is None else (code_result,))),
                all_verifications,
            )
            return ResultDetailSnapshot(
                task=ResultTaskFact(
                    task_id=task.id,
                    workspace_id=task.workspace_id,
                    title=task.title,
                    status=task.status,
                    version=task.version,
                    updated_at=_as_utc(task.updated_at),
                ),
                head=head,
                plan=plan,
                candidate=candidate,
                source_results=source_results,
                code_result=code_result,
                verifications=verifications,
                rework=rework,
                acceptance=acceptance,
                apply_operations=apply_operations,
                artifacts=_artifacts(session, workspace_id, artifact_ids),
                last_change_cursor=_last_change_cursor(session, workspace_id),
                as_of=datetime.now(UTC),
            )

    def list_verifications(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        candidate_id: UUID,
        *,
        cursor: ResultCursor | None,
        limit: int,
    ) -> ResultPage[VerificationResultV1] | None:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            if _candidate_row(session, workspace_id, root_task_id, candidate_id) is None:
                return None
            return _verification_page(
                session,
                workspace_id,
                root_task_id,
                candidate_id,
                cursor=cursor,
                limit=limit,
            )

    def list_rework(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        candidate_id: UUID,
        *,
        cursor: ResultCursor | None,
        limit: int,
    ) -> ResultPage[ReworkRecordV1] | None:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            if _candidate_row(session, workspace_id, root_task_id, candidate_id) is None:
                return None
            return _rework_page(
                session,
                workspace_id,
                root_task_id,
                candidate_id,
                cursor=cursor,
                limit=limit,
            )

    def load_acceptance_basis(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        candidate_id: UUID,
    ) -> ResultAcceptanceBasisSnapshot | None:
        with self._database.session() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            head = _head(
                session.scalar(
                    select(TaskResultHeadModel).where(
                        TaskResultHeadModel.workspace_id == workspace_id,
                        TaskResultHeadModel.root_task_id == root_task_id,
                    )
                )
            )
            if head is None or head.current_candidate_id != candidate_id:
                return None
            candidate = _candidate_row(
                session, workspace_id, root_task_id, candidate_id
            )
            if candidate is None:
                return None
            return ResultAcceptanceBasisSnapshot(
                candidate=candidate,
                plan=_plan_for_candidate(session, workspace_id, candidate),
                verifications=_verification_results(
                    session,
                    workspace_id,
                    root_task_id,
                    candidate_id,
                    limit=_MAX_CURRENT_VERIFICATIONS,
                ),
                result_version=head.result_version,
            )


def _candidate_history(
    session: Session,
    workspace_id: UUID,
    root_task_id: UUID,
    *,
    cursor: ResultCursor | None,
    limit: int,
) -> ResultPage[IntegrationCandidateV1]:
    statement = select(IntegrationCandidateModel).where(
        IntegrationCandidateModel.workspace_id == workspace_id,
        IntegrationCandidateModel.root_task_id == root_task_id,
    )
    if cursor is not None:
        statement = statement.where(
            or_(
                IntegrationCandidateModel.created_at < cursor.created_at,
                and_(
                    IntegrationCandidateModel.created_at == cursor.created_at,
                    IntegrationCandidateModel.id < cursor.item_id,
                ),
            )
        )
    rows = session.scalars(
        statement.order_by(
            IntegrationCandidateModel.created_at.desc(),
            IntegrationCandidateModel.id.desc(),
        ).limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    items = tuple(_candidate(row) for row in rows[:limit])
    marker = (
        ResultPageMarker(items[-1].created_at, items[-1].id)
        if has_more and items
        else None
    )
    return ResultPage(items=items, next_marker=marker)


def _verification_page(
    session: Session,
    workspace_id: UUID,
    root_task_id: UUID,
    candidate_id: UUID,
    *,
    cursor: ResultCursor | None,
    limit: int,
) -> ResultPage[VerificationResultV1]:
    statement = select(ResultVerificationResultModel).where(
        ResultVerificationResultModel.workspace_id == workspace_id,
        ResultVerificationResultModel.root_task_id == root_task_id,
        ResultVerificationResultModel.candidate_id == candidate_id,
    )
    if cursor is not None:
        statement = statement.where(
            or_(
                ResultVerificationResultModel.created_at > cursor.created_at,
                and_(
                    ResultVerificationResultModel.created_at == cursor.created_at,
                    ResultVerificationResultModel.id > cursor.item_id,
                ),
            )
        )
    rows = session.scalars(
        statement.order_by(
            ResultVerificationResultModel.created_at.asc(),
            ResultVerificationResultModel.id.asc(),
        ).limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    items = tuple(_verification(row) for row in rows[:limit])
    marker = (
        ResultPageMarker(rows[limit - 1].created_at, rows[limit - 1].id)
        if has_more and items
        else None
    )
    return ResultPage(items=items, next_marker=marker)


def _rework_page(
    session: Session,
    workspace_id: UUID,
    root_task_id: UUID,
    candidate_id: UUID,
    *,
    cursor: ResultCursor | None,
    limit: int,
) -> ResultPage[ReworkRecordV1]:
    statement = select(ResultReworkRecordModel).where(
        ResultReworkRecordModel.workspace_id == workspace_id,
        ResultReworkRecordModel.root_task_id == root_task_id,
        ResultReworkRecordModel.candidate_id == candidate_id,
    )
    if cursor is not None:
        statement = statement.where(
            or_(
                ResultReworkRecordModel.created_at > cursor.created_at,
                and_(
                    ResultReworkRecordModel.created_at == cursor.created_at,
                    ResultReworkRecordModel.id > cursor.item_id,
                ),
            )
        )
    rows = session.scalars(
        statement.order_by(
            ResultReworkRecordModel.created_at.asc(),
            ResultReworkRecordModel.id.asc(),
        ).limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    items = tuple(_rework(row) for row in rows[:limit])
    marker = (
        ResultPageMarker(items[-1].created_at, items[-1].id)
        if has_more and items
        else None
    )
    return ResultPage(items=items, next_marker=marker)


def _candidate_row(
    session: Session,
    workspace_id: UUID,
    root_task_id: UUID,
    candidate_id: UUID | None,
) -> IntegrationCandidateV1 | None:
    if candidate_id is None:
        return None
    row = session.scalar(
        select(IntegrationCandidateModel).where(
            IntegrationCandidateModel.workspace_id == workspace_id,
            IntegrationCandidateModel.root_task_id == root_task_id,
            IntegrationCandidateModel.id == candidate_id,
        )
    )
    return None if row is None else _candidate(row)


def _source_results(
    session: Session,
    workspace_id: UUID,
    candidate: IntegrationCandidateV1,
) -> tuple[CodeResultSnapshotV1, ...]:
    ids = (*candidate.source_result_ids,)
    rows = session.scalars(
        select(CodeResultSnapshotModel).where(
            CodeResultSnapshotModel.workspace_id == workspace_id,
            CodeResultSnapshotModel.root_task_id == candidate.root_task_id,
            CodeResultSnapshotModel.id.in_(ids),
        )
    ).all()
    by_id = {row.id: _snapshot(row) for row in rows}
    return tuple(by_id[item] for item in ids if item in by_id)


def _code_result(
    session: Session,
    workspace_id: UUID,
    candidate: IntegrationCandidateV1,
) -> CodeResultSnapshotV1 | None:
    row = session.scalar(
        select(CodeResultSnapshotModel).where(
            CodeResultSnapshotModel.workspace_id == workspace_id,
            CodeResultSnapshotModel.root_task_id == candidate.root_task_id,
            CodeResultSnapshotModel.id == candidate.code_result_id,
        )
    )
    return None if row is None else _snapshot(row)


def _verification_results(
    session: Session,
    workspace_id: UUID,
    root_task_id: UUID,
    candidate_id: UUID,
    *,
    limit: int,
) -> tuple[VerificationResultV1, ...]:
    rows = session.scalars(
        select(ResultVerificationResultModel)
        .where(
            ResultVerificationResultModel.workspace_id == workspace_id,
            ResultVerificationResultModel.root_task_id == root_task_id,
            ResultVerificationResultModel.candidate_id == candidate_id,
        )
        .order_by(
            ResultVerificationResultModel.created_at.asc(),
            ResultVerificationResultModel.id.asc(),
        )
        .limit(limit)
    ).all()
    return tuple(_verification(row) for row in rows)


def _operation_versions(
    session: Session,
    workspace_id: UUID,
    root_task_id: UUID,
    verifications: Sequence[VerificationResultV1],
) -> tuple[tuple[UUID, int], ...]:
    operation_ids = tuple(dict.fromkeys(item.operation_id for item in verifications))
    if not operation_ids:
        return ()
    rows = session.execute(
        select(ResultOperationAttemptModel.operation_id, ResultOperationAttemptModel.version)
        .where(
            ResultOperationAttemptModel.workspace_id == workspace_id,
            ResultOperationAttemptModel.root_task_id == root_task_id,
            ResultOperationAttemptModel.operation_id.in_(operation_ids),
        )
    ).all()
    return tuple((operation_id, version) for operation_id, version in rows)


def _rework_records(
    session: Session,
    workspace_id: UUID,
    root_task_id: UUID,
    candidate_id: UUID,
    *,
    limit: int,
) -> tuple[ReworkRecordV1, ...]:
    rows = session.scalars(
        select(ResultReworkRecordModel)
        .where(
            ResultReworkRecordModel.workspace_id == workspace_id,
            ResultReworkRecordModel.root_task_id == root_task_id,
            ResultReworkRecordModel.candidate_id == candidate_id,
        )
        .order_by(
            ResultReworkRecordModel.created_at.asc(),
            ResultReworkRecordModel.id.asc(),
        )
        .limit(limit)
    ).all()
    return tuple(_rework(row) for row in rows)


def _replacement_result_ids(
    session: Session,
    workspace_id: UUID,
    root_task_id: UUID,
    rework: Sequence[ReworkRecordV1],
) -> tuple[UUID, ...]:
    request_ids = tuple(
        dict.fromkeys(request_id for record in rework for request_id in record.request_ids)
    )
    if not request_ids:
        return ()
    rows = session.scalars(
        select(CodeResultSnapshotModel.id)
        .where(
            CodeResultSnapshotModel.workspace_id == workspace_id,
            CodeResultSnapshotModel.root_task_id == root_task_id,
            CodeResultSnapshotModel.request_id.in_(request_ids),
        )
        .order_by(CodeResultSnapshotModel.created_at.asc(), CodeResultSnapshotModel.id.asc())
    ).all()
    return tuple(rows)


def _acceptance_by_id(
    session: Session, workspace_id: UUID, acceptance_id: UUID | None
) -> TaskAcceptanceDecisionV1 | None:
    if acceptance_id is None:
        return None
    row = session.scalar(
        select(TaskAcceptanceDecisionModel).where(
            TaskAcceptanceDecisionModel.workspace_id == workspace_id,
            TaskAcceptanceDecisionModel.id == acceptance_id,
        )
    )
    return None if row is None else _acceptance(row)


def _acceptance_for_candidate(
    session: Session,
    workspace_id: UUID,
    root_task_id: UUID,
    candidate_id: UUID,
) -> TaskAcceptanceDecisionV1 | None:
    row = session.scalar(
        select(TaskAcceptanceDecisionModel)
        .where(
            TaskAcceptanceDecisionModel.workspace_id == workspace_id,
            TaskAcceptanceDecisionModel.root_task_id == root_task_id,
            TaskAcceptanceDecisionModel.candidate_id == candidate_id,
        )
        .order_by(
            TaskAcceptanceDecisionModel.created_at.desc(),
            TaskAcceptanceDecisionModel.id.desc(),
        )
    )
    return None if row is None else _acceptance(row)


def _apply_history(
    session: Session,
    workspace_id: UUID,
    root_task_id: UUID,
    candidate_id: UUID,
) -> tuple[ResultApplyFact, ...]:
    rows = session.scalars(
        select(ResultApplyOperationModel)
        .where(
            ResultApplyOperationModel.workspace_id == workspace_id,
            ResultApplyOperationModel.root_task_id == root_task_id,
            ResultApplyOperationModel.candidate_id == candidate_id,
        )
        .order_by(
            ResultApplyOperationModel.created_at.asc(),
            ResultApplyOperationModel.id.asc(),
        )
        .limit(_MAX_APPLY_HISTORY)
    ).all()
    return tuple(_apply(row) for row in rows)


def _active_operation(
    session: Session,
    workspace_id: UUID,
    root_task_id: UUID,
    head: TaskResultHead | None,
) -> ResultOperationFact | None:
    if head is not None and head.active_operation_id is not None:
        apply = session.scalar(
            select(ResultApplyOperationModel).where(
                ResultApplyOperationModel.workspace_id == workspace_id,
                ResultApplyOperationModel.root_task_id == root_task_id,
                ResultApplyOperationModel.id == head.active_operation_id,
            )
        )
        if apply is not None:
            return ResultOperationFact(
                operation_id=apply.id,
                operation_kind="apply",
                status=apply.status,
                attempt=apply.attempt,
                failure_code=apply.failure_code,
                receipt_ref=apply.receipt_ref,
                version=apply.version,
                candidate_id=apply.candidate_id,
            )
    attempt = session.scalar(
        select(ResultOperationAttemptModel)
        .where(
            ResultOperationAttemptModel.workspace_id == workspace_id,
            ResultOperationAttemptModel.root_task_id == root_task_id,
            ResultOperationAttemptModel.status.in_(_ACTIVE_OPERATION_STATUSES),
        )
        .order_by(
            ResultOperationAttemptModel.updated_at.desc(),
            ResultOperationAttemptModel.operation_id.desc(),
        )
    )
    if attempt is None:
        return None
    candidate_id = _payload_uuid(attempt.payload, "candidate_id")
    return ResultOperationFact(
        operation_id=attempt.operation_id,
        operation_kind=attempt.operation_kind,
        status=attempt.status,
        attempt=attempt.attempt,
        failure_code=attempt.failure_code,
        receipt_ref=attempt.receipt_ref,
        version=attempt.version,
        candidate_id=candidate_id,
    )


def _current_plan(
    session: Session, workspace_id: UUID, root_task_id: UUID
) -> TaskPlanSnapshot | None:
    row = session.scalar(
        select(PlanSnapshotModel)
        .join(
            RootTaskPlanHeadModel,
            and_(
                RootTaskPlanHeadModel.workspace_id == PlanSnapshotModel.workspace_id,
                RootTaskPlanHeadModel.root_task_id == PlanSnapshotModel.root_task_id,
                RootTaskPlanHeadModel.active_snapshot_id == PlanSnapshotModel.id,
                RootTaskPlanHeadModel.active_generation == PlanSnapshotModel.generation,
            ),
        )
        .where(
            PlanSnapshotModel.workspace_id == workspace_id,
            PlanSnapshotModel.root_task_id == root_task_id,
        )
    )
    return None if row is None else _plan(row)


def _plan_for_candidate(
    session: Session, workspace_id: UUID, candidate: IntegrationCandidateV1
) -> TaskPlanSnapshot | None:
    row = session.scalar(
        select(PlanSnapshotModel).where(
            PlanSnapshotModel.workspace_id == workspace_id,
            PlanSnapshotModel.root_task_id == candidate.root_task_id,
            PlanSnapshotModel.id == candidate.plan_snapshot_id,
        )
    )
    return None if row is None else _plan(row)


def _head(row: TaskResultHeadModel | None) -> TaskResultHead | None:
    if row is None:
        return None
    return TaskResultHead(
        workspace_id=row.workspace_id,
        root_task_id=row.root_task_id,
        current_candidate_id=row.current_candidate_id,
        current_acceptance_id=row.current_acceptance_id,
        result_version=row.result_version,
        active_operation_id=row.active_operation_id,
        auto_rounds_used=row.auto_rounds_used,
        auto_rounds_limit=row.auto_rounds_limit,
        auto_seconds_limit=row.auto_seconds_limit,
        auto_started_at=row.auto_started_at,
        auto_deadline=row.auto_deadline,
    )


def _plan(row: PlanSnapshotModel) -> TaskPlanSnapshot:
    snapshot = TaskPlanSnapshot.from_payload(dict(row.payload))
    if (
        snapshot.id != row.id
        or snapshot.workspace_id != row.workspace_id
        or snapshot.root_task_id != row.root_task_id
        or snapshot.generation != row.generation
    ):
        raise ValueError("task plan snapshot payload does not match identity columns")
    return snapshot


def _snapshot(row: CodeResultSnapshotModel) -> CodeResultSnapshotV1:
    return CodeResultSnapshotV1.from_mapping(dict(row.payload))


def _candidate(row: IntegrationCandidateModel) -> IntegrationCandidateV1:
    return IntegrationCandidateV1.from_mapping(dict(row.payload))


def _verification(row: ResultVerificationResultModel) -> VerificationResultV1:
    return VerificationResultV1.from_mapping(dict(row.payload))


def _acceptance(row: TaskAcceptanceDecisionModel) -> TaskAcceptanceDecisionV1:
    return TaskAcceptanceDecisionV1.from_mapping(
        {
            "id": str(row.id),
            "workspace_id": str(row.workspace_id),
            "root_task_id": str(row.root_task_id),
            "candidate_id": str(row.candidate_id),
            "candidate_hash": row.candidate_hash,
            "plan_content_hash": row.plan_content_hash,
            "acceptance_basis_hash": row.acceptance_basis_hash,
            "result_version": row.result_version,
            "criteria": row.criteria_json,
            "actor_type": row.actor_type,
            "actor_id": row.actor_id,
            "command_id": str(row.command_id),
            "created_at": row.created_at.isoformat(),
        }
    )


def _rework(row: ResultReworkRecordModel) -> ReworkRecordV1:
    return ReworkRecordV1.from_mapping(
        {
            "id": str(row.id),
            "workspace_id": str(row.workspace_id),
            "root_task_id": str(row.root_task_id),
            "candidate_id": str(row.candidate_id),
            "source_issue_keys": row.source_issue_keys_json,
            "target_worker_item_ids": row.target_worker_item_ids_json,
            "impact_worker_item_ids": row.impact_worker_item_ids_json,
            "reason": row.reason,
            "mode": row.mode,
            "round_number": row.round_number,
            "source_command_id": str(row.source_command_id),
            "request_ids": row.request_ids_json,
            "status": row.status,
            "created_at": row.created_at.isoformat(),
        }
    )


def _apply(row: ResultApplyOperationModel) -> ResultApplyFact:
    return ResultApplyFact(
        operation_id=row.id,
        candidate_id=row.candidate_id,
        status=row.status,
        attempt=row.attempt,
        target_ref=row.target_ref,
        accepted_commit=row.accepted_commit,
        receipt_ref=row.receipt_ref,
        failure_code=row.failure_code,
        version=row.version,
        created_at=_as_utc(row.created_at),
    )


def _payload_uuid(payload: Mapping[str, object], key: str) -> UUID | None:
    value = payload.get(key)
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None

def _artifact_ids(
    sources: tuple[CodeResultSnapshotV1, ...],
    verifications: tuple[VerificationResultV1, ...],
) -> tuple[UUID, ...]:
    result: list[UUID] = []
    for item in (*sources, *verifications):
        for artifact_id in item.artifact_ids:
            if artifact_id not in result:
                result.append(artifact_id)
    return tuple(result)


def _artifacts(
    session: Session, workspace_id: UUID, artifact_ids: tuple[UUID, ...]
) -> tuple[ResultArtifactFact, ...]:
    if not artifact_ids:
        return ()
    rows = session.scalars(
        select(ArtifactModel).where(
            ArtifactModel.workspace_id == workspace_id,
            ArtifactModel.id.in_(artifact_ids),
        )
    ).all()
    by_id = {
        row.id: ResultArtifactFact(
            artifact_id=row.id,
            artifact_type=row.artifact_type,
            content_hash=row.content_hash,
            size_bytes=row.size_bytes,
            media_type=row.media_type,
            content_available=bool(row.storage_ref.strip()),
        )
        for row in rows
    }
    return tuple(by_id[item] for item in artifact_ids if item in by_id)


def _last_change_cursor(session: Session, workspace_id: UUID) -> int:
    value = session.scalar(
        select(func.max(ProjectionChangeModel.cursor)).where(
            ProjectionChangeModel.workspace_id == workspace_id
        )
    )
    return int(value or 0)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


__all__ = ["SqlAlchemyResultQueryStore"]
