from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.application.knowledge_candidate_submission import (
    KnowledgeCandidateSubmission,
    KnowledgeCandidateSubmissionStatus,
)
from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.persistence.models import KnowledgeCandidateSubmissionModel


class KnowledgeCandidateSubmissionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, submission: KnowledgeCandidateSubmission) -> None:
        self._session.add(
            KnowledgeCandidateSubmissionModel(
                id=submission.id,
                workspace_id=submission.workspace_id,
                producer_type=submission.producer_type,
                producer_id=submission.producer_id,
                planning_run_id=submission.planning_run_id,
                execution_id=submission.execution_id,
                worker_item_id=submission.worker_item_id,
                task_id=submission.task_id,
                subtask_task_id=submission.subtask_task_id,
                artifact_refs=list(submission.artifact_refs),
                evidence_refs=list(submission.evidence_refs),
                proposal_hash=submission.proposal_hash,
                content_ref=submission.content_ref,
                content_bytes=submission.content_bytes,
                knowledge_command_id=submission.knowledge_command_id,
                status=submission.status.value,
                attempt=submission.attempt,
                lease_owner=submission.lease_owner,
                lease_expires_at=submission.lease_expires_at,
                next_attempt_at=submission.next_attempt_at,
                terminal_candidate_id=submission.terminal_candidate_id,
                error_code=submission.error_code,
                version=submission.version,
                created_at=submission.created_at,
                updated_at=submission.updated_at,
            )
        )

    def get(
        self, submission_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> KnowledgeCandidateSubmission | None:
        statement = select(KnowledgeCandidateSubmissionModel).where(
            KnowledgeCandidateSubmissionModel.id == submission_id,
            KnowledgeCandidateSubmissionModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain(row) if row is not None else None

    def find_by_producer_hash(
        self,
        workspace_id: UUID,
        producer_type: str,
        producer_id: UUID,
        proposal_hash: str,
        *,
        for_update: bool = False,
    ) -> KnowledgeCandidateSubmission | None:
        statement = select(KnowledgeCandidateSubmissionModel).where(
            KnowledgeCandidateSubmissionModel.workspace_id == workspace_id,
            KnowledgeCandidateSubmissionModel.producer_type == producer_type,
            KnowledgeCandidateSubmissionModel.producer_id == producer_id,
            KnowledgeCandidateSubmissionModel.proposal_hash == proposal_hash,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain(row) if row is not None else None

    def list_for_producer(
        self, workspace_id: UUID, producer_type: str, producer_id: UUID
    ) -> tuple[KnowledgeCandidateSubmission, ...]:
        with self._session.no_autoflush:
            rows = self._session.scalars(
                select(KnowledgeCandidateSubmissionModel)
                .where(
                    KnowledgeCandidateSubmissionModel.workspace_id == workspace_id,
                    KnowledgeCandidateSubmissionModel.producer_type == producer_type,
                    KnowledgeCandidateSubmissionModel.producer_id == producer_id,
                )
                .order_by(
                    KnowledgeCandidateSubmissionModel.created_at,
                    KnowledgeCandidateSubmissionModel.id,
                )
            ).all()
        return tuple(_to_domain(row) for row in rows)

    def claim_next(
        self,
        *,
        owner: str,
        now: datetime,
        lease_ttl: timedelta,
        limit: int,
        workspace_id: UUID | None = None,
    ) -> tuple[KnowledgeCandidateSubmission, ...]:
        if not owner.strip():
            raise ValueError("candidate submission owner is required")
        if lease_ttl.total_seconds() <= 0 or limit < 1:
            raise ValueError("candidate submission claim limits must be positive")
        claimable = or_(
            KnowledgeCandidateSubmissionModel.status.in_(
                [
                    KnowledgeCandidateSubmissionStatus.QUEUED.value,
                    KnowledgeCandidateSubmissionStatus.RETRY_WAIT.value,
                ]
            ),
            and_(
                KnowledgeCandidateSubmissionModel.status
                == KnowledgeCandidateSubmissionStatus.SUBMITTING.value,
                KnowledgeCandidateSubmissionModel.lease_expires_at <= now,
            ),
        )
        due = or_(
            KnowledgeCandidateSubmissionModel.next_attempt_at.is_(None),
            KnowledgeCandidateSubmissionModel.next_attempt_at <= now,
        )
        statement = (
            select(KnowledgeCandidateSubmissionModel)
            .where(claimable, due)
            .order_by(
                KnowledgeCandidateSubmissionModel.created_at,
                KnowledgeCandidateSubmissionModel.id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if workspace_id is not None:
            statement = statement.where(
                KnowledgeCandidateSubmissionModel.workspace_id == workspace_id
            )
        rows = self._session.scalars(statement).all()
        result: list[KnowledgeCandidateSubmission] = []
        for row in rows:
            row.status = KnowledgeCandidateSubmissionStatus.SUBMITTING.value
            row.lease_owner = owner
            row.lease_expires_at = now + lease_ttl
            row.next_attempt_at = None
            row.attempt += 1
            row.version += 1
            row.updated_at = now
            result.append(_to_domain(row))
        self._session.flush()
        return tuple(result)

    def save(self, submission: KnowledgeCandidateSubmission, previous_version: int) -> None:
        statement = (
            update(KnowledgeCandidateSubmissionModel)
            .where(
                KnowledgeCandidateSubmissionModel.id == submission.id,
                KnowledgeCandidateSubmissionModel.workspace_id == submission.workspace_id,
                KnowledgeCandidateSubmissionModel.version == previous_version,
            )
            .values(
                knowledge_command_id=submission.knowledge_command_id,
                status=submission.status.value,
                attempt=submission.attempt,
                lease_owner=submission.lease_owner,
                lease_expires_at=submission.lease_expires_at,
                next_attempt_at=submission.next_attempt_at,
                terminal_candidate_id=submission.terminal_candidate_id,
                error_code=submission.error_code,
                version=submission.version,
                updated_at=submission.updated_at,
            )
        )
        result = self._session.execute(statement)
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            actual = self._session.scalar(
                select(KnowledgeCandidateSubmissionModel.version).where(
                    KnowledgeCandidateSubmissionModel.id == submission.id,
                    KnowledgeCandidateSubmissionModel.workspace_id == submission.workspace_id,
                )
            )
            raise VersionConflict(previous_version, actual or 0)

    def list_for_workspace(
        self,
        workspace_id: UUID,
        *,
        producer_types: Collection[str] = (),
    ) -> tuple[KnowledgeCandidateSubmission, ...]:
        statement = select(KnowledgeCandidateSubmissionModel).where(
            KnowledgeCandidateSubmissionModel.workspace_id == workspace_id
        )
        values = tuple(producer_types)
        if values:
            statement = statement.where(
                KnowledgeCandidateSubmissionModel.producer_type.in_(values)
            )
        with self._session.no_autoflush:
            rows = self._session.scalars(
                statement.order_by(
                    KnowledgeCandidateSubmissionModel.created_at,
                    KnowledgeCandidateSubmissionModel.id,
                )
            ).all()
        return tuple(_to_domain(row) for row in rows)


def _to_domain(row: KnowledgeCandidateSubmissionModel) -> KnowledgeCandidateSubmission:
    return KnowledgeCandidateSubmission(
        id=row.id,
        workspace_id=row.workspace_id,
        producer_type=row.producer_type,
        producer_id=row.producer_id,
        planning_run_id=row.planning_run_id,
        execution_id=row.execution_id,
        worker_item_id=row.worker_item_id,
        task_id=row.task_id,
        subtask_task_id=row.subtask_task_id,
        artifact_refs=tuple(str(item) for item in row.artifact_refs),
        evidence_refs=tuple(str(item) for item in row.evidence_refs),
        proposal_hash=row.proposal_hash,
        content_ref=row.content_ref,
        content_bytes=row.content_bytes,
        knowledge_command_id=row.knowledge_command_id,
        status=KnowledgeCandidateSubmissionStatus(row.status),
        attempt=row.attempt,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        next_attempt_at=row.next_attempt_at,
        terminal_candidate_id=row.terminal_candidate_id,
        error_code=row.error_code,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
