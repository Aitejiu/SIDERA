from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.context_package_queries import (
    ContextPackageAcknowledgementView,
    ContextPackageAssemblyView,
    ContextPackageMaterializationAuditView,
    ContextPackageView,
    KnowledgeSubmissionPageView,
    KnowledgeSubmissionView,
    decode_submission_cursor,
    encode_submission_cursor,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextMaterializationAudit,
    ContextPackageManifestV1,
    ContextPurpose,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.work_context import WorkContextRevision
from sidera_control_plane.persistence.context_package_repository import (
    ContextPackageRepository,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ContextAssemblyRequestModel,
    ContextPackageAcknowledgementModel,
    ContextPackageMaterializationAuditModel,
    ContextPackageModel,
    KnowledgeCandidateSubmissionModel,
    WorkContextRevisionModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from sidera_control_plane.persistence.work_context_repository import (
    WorkContextRepository,
)


class SqlAlchemyContextPackageQueryStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get_package(
        self, package_id: UUID, workspace_id: UUID
    ) -> ContextPackageView | None:
        with self._database.session() as session:
            row = session.scalar(
                select(ContextPackageModel).where(
                    ContextPackageModel.id == package_id,
                    ContextPackageModel.workspace_id == workspace_id,
                )
            )
            return _package_view(session, row) if row is not None else None

    def get_package_for_consumer(
        self,
        purpose: ContextPurpose,
        consumer_id: UUID,
        workspace_id: UUID,
    ) -> ContextPackageView | None:
        with self._database.session() as session:
            row = session.scalar(
                select(ContextPackageModel).where(
                    ContextPackageModel.workspace_id == workspace_id,
                    ContextPackageModel.purpose == ContextPurpose(purpose).value,
                    ContextPackageModel.consumer_id == consumer_id,
                )
            )
            return _package_view(session, row) if row is not None else None

    def list_submissions(
        self,
        workspace_id: UUID,
        *,
        planning_run_id: UUID | None = None,
        execution_id: UUID | None = None,
        worker_item_id: UUID | None = None,
        task_id: UUID | None = None,
        limit: int,
        cursor: str | None,
    ) -> KnowledgeSubmissionPageView:
        with self._database.session() as session:
            statement = select(KnowledgeCandidateSubmissionModel).where(
                KnowledgeCandidateSubmissionModel.workspace_id == workspace_id
            )
            filters = [
                planning_run_id is not None,
                execution_id is not None,
                worker_item_id is not None,
                task_id is not None,
            ]
            if sum(filters) != 1:
                raise ValueError("exactly one submission producer scope is required")
            if planning_run_id is not None:
                statement = statement.where(
                    KnowledgeCandidateSubmissionModel.planning_run_id == planning_run_id
                )
            elif execution_id is not None:
                statement = statement.where(
                    KnowledgeCandidateSubmissionModel.execution_id == execution_id
                )
            elif worker_item_id is not None:
                statement = statement.where(
                    KnowledgeCandidateSubmissionModel.worker_item_id == worker_item_id
                )
            else:
                assert task_id is not None
                statement = statement.where(
                    or_(
                        KnowledgeCandidateSubmissionModel.task_id == task_id,
                        KnowledgeCandidateSubmissionModel.subtask_task_id == task_id,
                    )
                )
            if cursor is not None:
                created_at, submission_id = decode_submission_cursor(cursor)
                statement = statement.where(
                    or_(
                        KnowledgeCandidateSubmissionModel.created_at > created_at,
                        and_(
                            KnowledgeCandidateSubmissionModel.created_at == created_at,
                            KnowledgeCandidateSubmissionModel.id > submission_id,
                        ),
                    )
                )
            rows = session.scalars(
                statement.order_by(
                    KnowledgeCandidateSubmissionModel.created_at.asc(),
                    KnowledgeCandidateSubmissionModel.id.asc(),
                ).limit(limit + 1)
            ).all()
            page = rows[:limit]
            next_cursor = (
                encode_submission_cursor(page[-1].created_at, page[-1].id)
                if len(rows) > limit
                else None
            )
            return KnowledgeSubmissionPageView(
                items=tuple(_submission_view(row) for row in page),
                next_cursor=next_cursor,
            )

    def materialization_input(
        self, package_id: UUID, workspace_id: UUID
    ) -> tuple[ContextPackageManifestV1, WorkContextRevision] | None:
        with self._database.session() as session:
            package = ContextPackageRepository(session).get(package_id, workspace_id)
            if package is None:
                return None
            revision = WorkContextRepository(session).get_revision(
                package.work_context_revision_id, workspace_id
            )
            if revision is None:
                raise ValidationError("context package work context revision not found")
            return package, revision

    def next_materialization_attempt(self, package_id: UUID, workspace_id: UUID) -> int:
        with self._database.session() as session:
            latest = session.scalar(
                select(func.max(ContextPackageMaterializationAuditModel.attempt)).where(
                    ContextPackageMaterializationAuditModel.package_id == package_id,
                    ContextPackageMaterializationAuditModel.workspace_id
                    == workspace_id,
                )
            )
            return int(latest or 0) + 1

    def append_materialization_audit(
        self,
        audit: ContextMaterializationAudit,
        *,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> None:
        with SqlAlchemyUnitOfWork(self._database) as uow:
            _ensure_materialization_command(
                uow,
                workspace_id=audit.workspace_id,
                package_id=audit.package_id,
                audit_attempt=audit.attempt,
                actor_type=actor_type,
                actor_id=actor_id,
                causation_id=causation_id,
                correlation_id=correlation_id,
            )
            uow.context_packages.append_materialization_audit(
                audit,
                actor_type=actor_type,
                actor_id=actor_id,
                causation_id=causation_id,
                correlation_id=correlation_id,
            )

    def mark_corrupt(
        self,
        package_id: UUID,
        workspace_id: UUID,
        *,
        reason: str,
        now: datetime,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> None:
        with SqlAlchemyUnitOfWork(self._database) as uow:
            _ensure_materialization_command(
                uow,
                workspace_id=workspace_id,
                package_id=package_id,
                audit_attempt=None,
                actor_type=actor_type,
                actor_id=actor_id,
                causation_id=causation_id,
                correlation_id=correlation_id,
            )
            uow.context_packages.mark_corrupt(
                package_id,
                workspace_id,
                reason=reason,
                now=now,
                actor_type=actor_type,
                actor_id=actor_id,
                causation_id=causation_id,
                correlation_id=correlation_id,
            )


def _package_view(session: Any, row: ContextPackageModel) -> ContextPackageView:
    manifest = ContextPackageRepository(session).get(row.id, row.workspace_id)
    if manifest is None:
        raise ValidationError("context package disappeared during query")
    assembly = session.scalar(
        select(ContextAssemblyRequestModel).where(
            ContextAssemblyRequestModel.id == row.assembly_request_id,
            ContextAssemblyRequestModel.workspace_id == row.workspace_id,
        )
    )
    if assembly is None:
        raise ValidationError("context package assembly request not found")
    revision = session.scalar(
        select(WorkContextRevisionModel).where(
            WorkContextRevisionModel.id == row.work_context_revision_id,
            WorkContextRevisionModel.workspace_id == row.workspace_id,
        )
    )
    if revision is None:
        raise ValidationError("context package work context revision not found")
    acknowledgements = session.scalars(
        select(ContextPackageAcknowledgementModel)
        .where(
            ContextPackageAcknowledgementModel.package_id == row.id,
            ContextPackageAcknowledgementModel.workspace_id == row.workspace_id,
        )
        .order_by(
            ContextPackageAcknowledgementModel.acknowledged_at.asc(),
            ContextPackageAcknowledgementModel.consumer_id.asc(),
        )
    ).all()
    audits = session.scalars(
        select(ContextPackageMaterializationAuditModel)
        .where(
            ContextPackageMaterializationAuditModel.package_id == row.id,
            ContextPackageMaterializationAuditModel.workspace_id == row.workspace_id,
        )
        .order_by(
            ContextPackageMaterializationAuditModel.occurred_at.asc(),
            ContextPackageMaterializationAuditModel.id.asc(),
        )
    ).all()
    return ContextPackageView(
        manifest=manifest,
        assembly=ContextPackageAssemblyView(
            request_id=assembly.id,
            status=assembly.status,
            attempt=assembly.attempt,
            next_attempt_at=assembly.next_attempt_at,
            error_code=assembly.error_code,
            request_fingerprint=assembly.request_fingerprint,
            version=assembly.version,
            created_at=assembly.created_at,
            updated_at=assembly.updated_at,
        ),
        work_context_revision_number=revision.revision_number,
        work_context_content_hash=revision.content_hash,
        acknowledgements=tuple(_ack_view(item) for item in acknowledgements),
        materialization_audits=tuple(_audit_view(item) for item in audits),
    )


def _ensure_materialization_command(
    uow: SqlAlchemyUnitOfWork,
    *,
    workspace_id: UUID,
    package_id: UUID,
    audit_attempt: int | None,
    actor_type: str,
    actor_id: str,
    causation_id: UUID,
    correlation_id: UUID,
) -> None:
    if uow.commands.find_by_id(workspace_id, causation_id) is not None:
        return
    envelope = CommandEnvelope(
        id=causation_id,
        workspace_id=workspace_id,
        command_type="MaterializeContextPackageOwnerAudit",
        target_type="context_package",
        actor_type=actor_type,
        actor_id=actor_id,
        target_id=package_id,
        payload={"attempt": audit_attempt},
        expected_version=1,
        idempotency_key=(
            f"context-materialize:{package_id}:{audit_attempt}:{causation_id}"
        ),
        correlation_id=correlation_id,
    )
    uow.commands.record_applied(
        envelope,
        hash_request(envelope),
        CommandResult(
            command_id=causation_id,
            aggregate_id=package_id,
            aggregate_version=1,
            event_ids=(),
            data={"package_id": str(package_id)},
        ),
    )


def _ack_view(
    row: ContextPackageAcknowledgementModel,
) -> ContextPackageAcknowledgementView:
    return ContextPackageAcknowledgementView(
        package_id=row.package_id,
        consumer_type=row.consumer_type,
        consumer_id=row.consumer_id,
        context_version=row.context_version,
        package_hash=row.acknowledged_package_hash,
        adapter_module_id=row.adapter_module_instance_id,
        adapter_config_version=row.adapter_config_version,
        acknowledged_at=row.acknowledged_at,
    )


def _audit_view(
    row: ContextPackageMaterializationAuditModel,
) -> ContextPackageMaterializationAuditView:
    return ContextPackageMaterializationAuditView(
        audit_id=row.id,
        package_id=row.package_id,
        consumer_type=row.consumer_type,
        consumer_id=row.consumer_id,
        context_version=row.context_version,
        attempt=row.attempt,
        outcome=row.outcome,
        section_count=row.section_count,
        byte_count=row.byte_count,
        input_token_count=row.input_token_count,
        failure_code=row.failure_code,
        occurred_at=row.occurred_at,
    )


def _submission_view(row: KnowledgeCandidateSubmissionModel) -> KnowledgeSubmissionView:
    return KnowledgeSubmissionView(
        submission_id=row.id,
        workspace_id=row.workspace_id,
        producer_type=row.producer_type,
        producer_id=row.producer_id,
        planning_run_id=row.planning_run_id,
        execution_id=row.execution_id,
        worker_item_id=row.worker_item_id,
        task_id=row.task_id,
        subtask_task_id=row.subtask_task_id,
        proposal_hash=row.proposal_hash,
        status=row.status,
        attempt=row.attempt,
        knowledge_command_id=row.knowledge_command_id,
        terminal_candidate_id=row.terminal_candidate_id,
        candidate_deep_link=(
            f"knowledge://candidates/{row.terminal_candidate_id}"
            if row.terminal_candidate_id is not None
            else None
        ),
        error_code=row.error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
