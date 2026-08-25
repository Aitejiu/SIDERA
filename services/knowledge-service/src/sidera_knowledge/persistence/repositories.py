from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sidera_knowledge.domain.candidates import (
    KnowledgeCandidate,
    KnowledgeCandidateReview,
)
from sidera_knowledge.domain.commands import (
    CommandEnvelope,
    EventEnvelope,
    topic_for_event,
)
from sidera_knowledge.domain.pages import (
    KnowledgePage,
    KnowledgeRelationSnapshot,
    KnowledgeRevision,
    KnowledgeSectionSnapshot,
    KnowledgeSource,
)
from sidera_knowledge.persistence.models import (
    CommandModel,
    EventModel,
    KnowledgeCandidateModel,
    KnowledgeCandidateReviewModel,
    KnowledgeCandidateSourceModel,
    KnowledgePageModel,
    KnowledgeRelationSnapshotModel,
    KnowledgeRevisionModel,
    KnowledgeRevisionSourceModel,
    KnowledgeSectionSnapshotModel,
    KnowledgeSourceModel,
    OutboxMessageModel,
)


class PersistenceConflict(RuntimeError):
    """A caller-visible optimistic or database integrity conflict."""


class PersistenceNotFound(RuntimeError):
    """A required aggregate does not exist in the requested workspace."""


def _require_created_at(value: datetime | None) -> datetime:
    if value is None:
        raise RuntimeError("database row has no created_at")
    return value


def _row_to_page(row: KnowledgePageModel) -> KnowledgePage:
    return KnowledgePage(
        page_id=row.id,
        workspace_id=row.workspace_id,
        canonical_title=row.canonical_title,
        slug=row.slug,
        status=cast(Any, row.status),
        current_revision_id=row.current_revision_id,
        version=row.version,
    )


def _row_to_section(row: KnowledgeSectionSnapshotModel) -> KnowledgeSectionSnapshot:
    return KnowledgeSectionSnapshot(
        section_key=row.section_key,
        heading=row.heading,
        summary=row.summary,
        body_markdown=row.body_markdown,
        ordinal=row.ordinal,
    )


def _row_to_relation(row: KnowledgeRelationSnapshotModel) -> KnowledgeRelationSnapshot:
    return KnowledgeRelationSnapshot(
        relation_key=row.relation_key,
        relation_kind=cast(Any, row.relation_kind),
        target_page_id=row.target_page_id,
        target_revision_id=row.target_revision_id,
    )


def _row_to_revision(
    session: Session, row: KnowledgeRevisionModel
) -> KnowledgeRevision:
    sections = session.scalars(
        select(KnowledgeSectionSnapshotModel)
        .where(
            KnowledgeSectionSnapshotModel.revision_id == row.id,
            KnowledgeSectionSnapshotModel.workspace_id == row.workspace_id,
        )
        .order_by(KnowledgeSectionSnapshotModel.ordinal)
    ).all()
    relations = session.scalars(
        select(KnowledgeRelationSnapshotModel)
        .where(
            KnowledgeRelationSnapshotModel.revision_id == row.id,
            KnowledgeRelationSnapshotModel.workspace_id == row.workspace_id,
        )
        .order_by(KnowledgeRelationSnapshotModel.relation_key)
    ).all()
    source_links = session.scalars(
        select(KnowledgeRevisionSourceModel)
        .where(
            KnowledgeRevisionSourceModel.revision_id == row.id,
            KnowledgeRevisionSourceModel.workspace_id == row.workspace_id,
        )
        .order_by(KnowledgeRevisionSourceModel.ordinal)
    ).all()
    tags = tuple(cast(Sequence[str], row.tags))
    return KnowledgeRevision(
        revision_id=row.id,
        page_id=row.page_id,
        workspace_id=row.workspace_id,
        revision_number=row.revision_number,
        title=row.title,
        summary=row.summary,
        tags=tags,
        sections=tuple(_row_to_section(section) for section in sections),
        relations=tuple(_row_to_relation(relation) for relation in relations),
        source_refs=tuple(link.source_id for link in source_links),
        change_reason=row.change_reason,
        created_by=row.created_by,
        created_at=_require_created_at(row.created_at),
        accepted_candidate_id=row.accepted_candidate_id,
    )


def _search_document(revision: KnowledgeRevision) -> str:
    parts = [revision.title, revision.summary, *revision.tags]
    parts.extend(section.body_markdown for section in revision.sections)
    return "\n".join(parts)


class SqlAlchemyKnowledgePageRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_page(self, page: KnowledgePage) -> None:
        self._session.add(
            KnowledgePageModel(
                id=page.page_id,
                workspace_id=page.workspace_id,
                canonical_title=page.canonical_title,
                slug=page.slug,
                status=page.status,
                current_revision_id=page.current_revision_id,
                version=page.version,
            )
        )

    def get_page(self, page_id: UUID, workspace_id: UUID) -> KnowledgePage | None:
        row = self._session.scalar(
            select(KnowledgePageModel).where(
                KnowledgePageModel.id == page_id,
                KnowledgePageModel.workspace_id == workspace_id,
            )
        )
        return _row_to_page(row) if row is not None else None

    def get_page_for_update(
        self, page_id: UUID, workspace_id: UUID
    ) -> KnowledgePage | None:
        row = self._session.scalar(
            select(KnowledgePageModel)
            .where(
                KnowledgePageModel.id == page_id,
                KnowledgePageModel.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        return _row_to_page(row) if row is not None else None

    def get_revision(
        self, revision_id: UUID, workspace_id: UUID
    ) -> KnowledgeRevision | None:
        row = self._session.scalar(
            select(KnowledgeRevisionModel).where(
                KnowledgeRevisionModel.id == revision_id,
                KnowledgeRevisionModel.workspace_id == workspace_id,
            )
        )
        return _row_to_revision(self._session, row) if row is not None else None

    def add_revision_and_advance_page(
        self, revision: KnowledgeRevision, expected_page_version: int
    ) -> None:
        page = self._session.scalar(
            select(KnowledgePageModel)
            .where(
                KnowledgePageModel.id == revision.page_id,
                KnowledgePageModel.workspace_id == revision.workspace_id,
            )
            .with_for_update()
        )
        if page is None:
            raise PersistenceNotFound("page not found in workspace")
        if page.version != expected_page_version:
            raise PersistenceConflict("page version conflict")

        self._session.add(
            KnowledgeRevisionModel(
                id=revision.revision_id,
                workspace_id=revision.workspace_id,
                page_id=revision.page_id,
                revision_number=revision.revision_number,
                title=revision.title,
                summary=revision.summary,
                tags=list(revision.tags),
                snapshot=revision.snapshot,
                change_reason=revision.change_reason,
                accepted_candidate_id=revision.accepted_candidate_id,
                created_by=revision.created_by,
                created_at=revision.created_at,
                content_hash=revision.content_hash,
                search_document=_search_document(revision),
            )
        )
        self._session.add_all(
            [
                KnowledgeSectionSnapshotModel(
                    revision_id=revision.revision_id,
                    workspace_id=revision.workspace_id,
                    section_key=section.section_key,
                    heading=section.heading,
                    summary=section.summary,
                    body_markdown=section.body_markdown,
                    ordinal=section.ordinal,
                    section_hash=section.section_hash,
                    content_byte_count=section.utf8_byte_count,
                )
                for section in revision.sections
            ]
        )
        self._session.add_all(
            [
                KnowledgeRelationSnapshotModel(
                    revision_id=revision.revision_id,
                    workspace_id=revision.workspace_id,
                    relation_key=relation.relation_key,
                    relation_kind=relation.relation_kind,
                    target_page_id=relation.target_page_id,
                    target_revision_id=relation.target_revision_id,
                )
                for relation in revision.relations
            ]
        )
        self._session.add_all(
            [
                KnowledgeRevisionSourceModel(
                    revision_id=revision.revision_id,
                    source_id=source_id,
                    workspace_id=revision.workspace_id,
                    ordinal=ordinal,
                    provenance_role="revision",
                )
                for ordinal, source_id in enumerate(revision.source_refs)
            ]
        )
        page.current_revision_id = revision.revision_id
        page.version += 1

    def archive_page(
        self, page_id: UUID, workspace_id: UUID, expected_version: int
    ) -> KnowledgePage:
        page = self._session.scalar(
            select(KnowledgePageModel)
            .where(
                KnowledgePageModel.id == page_id,
                KnowledgePageModel.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if page is None:
            raise PersistenceNotFound("page not found in workspace")
        if page.version != expected_version:
            raise PersistenceConflict("page version conflict")
        if page.status == "archived":
            raise PersistenceConflict("page already archived")
        page.status = "archived"
        page.version += 1
        return _row_to_page(page)


class SqlAlchemyKnowledgeSourceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_source(self, source: KnowledgeSource) -> None:
        self._session.add(
            KnowledgeSourceModel(
                id=source.source_id,
                workspace_id=source.workspace_id,
                source_type=source.source_type,
                label=source.label,
                uri=source.uri,
                workspace_relative_path=source.workspace_relative_path,
                git_revision=source.git_revision,
                digest=source.digest,
                source_metadata=source.metadata,
                availability=source.availability,
                safe_reason_code=source.safe_reason_code,
                last_checked_at=source.last_checked_at,
                version=source.version,
            )
        )

    def get_source(self, source_id: UUID, workspace_id: UUID) -> KnowledgeSource | None:
        row = self._session.scalar(
            select(KnowledgeSourceModel).where(
                KnowledgeSourceModel.id == source_id,
                KnowledgeSourceModel.workspace_id == workspace_id,
            )
        )
        if row is None:
            return None
        return KnowledgeSource(
            source_id=row.id,
            workspace_id=row.workspace_id,
            source_type=cast(Any, row.source_type),
            label=row.label,
            uri=row.uri,
            workspace_relative_path=row.workspace_relative_path,
            git_revision=row.git_revision,
            digest=row.digest,
            metadata=row.source_metadata,
            availability=cast(Any, row.availability),
            safe_reason_code=row.safe_reason_code,
            last_checked_at=row.last_checked_at,
            version=row.version,
        )

    def find_by_digest(
        self, workspace_id: UUID, source_type: str, digest: str
    ) -> KnowledgeSource | None:
        row = self._session.scalar(
            select(KnowledgeSourceModel).where(
                KnowledgeSourceModel.workspace_id == workspace_id,
                KnowledgeSourceModel.source_type == source_type,
                KnowledgeSourceModel.digest == digest,
            )
        )
        if row is None:
            return None
        return self.get_source(row.id, workspace_id)

    def mark_unavailable(
        self,
        source_id: UUID,
        workspace_id: UUID,
        expected_version: int,
        reason_code: str,
        checked_at: datetime,
    ) -> KnowledgeSource:
        row = self._session.scalar(
            select(KnowledgeSourceModel)
            .where(
                KnowledgeSourceModel.id == source_id,
                KnowledgeSourceModel.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if row is None:
            raise PersistenceNotFound("source not found in workspace")
        if row.availability == "unavailable" and row.safe_reason_code == reason_code:
            source = self.get_source(source_id, workspace_id)
            if source is None:
                raise PersistenceNotFound("source not found in workspace")
            return source
        if row.version != expected_version:
            raise PersistenceConflict("source version conflict")
        row.availability = "unavailable"
        row.safe_reason_code = reason_code
        row.last_checked_at = checked_at
        row.version += 1
        source = self.get_source(source_id, workspace_id)
        if source is None:
            raise PersistenceNotFound("source not found in workspace")
        return source


class SqlAlchemyKnowledgeCandidateRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_candidate(self, candidate: KnowledgeCandidate) -> None:
        self._session.add(
            KnowledgeCandidateModel(
                id=candidate.candidate_id,
                workspace_id=candidate.workspace_id,
                operation=candidate.operation,
                target_page_id=candidate.target_page_id,
                expected_revision_id=candidate.expected_revision_id,
                expected_revision_hash=candidate.expected_revision_hash,
                proposed_snapshot=candidate.proposed_snapshot,
                candidate_hash=candidate.candidate_hash,
                producer_type=candidate.producer_type,
                producer_id=candidate.producer_id,
                task_id=candidate.task_id,
                subtask_id=candidate.subtask_id,
                worker_item_id=candidate.worker_item_id,
                execution_id=candidate.execution_id,
                supersedes_candidate_id=candidate.supersedes_candidate_id,
                status=candidate.status,
                version=candidate.version,
            )
        )
        self._session.add_all(
            [
                KnowledgeCandidateSourceModel(
                    candidate_id=candidate.candidate_id,
                    source_id=source_id,
                    workspace_id=candidate.workspace_id,
                    ordinal=ordinal,
                    provenance_role="candidate",
                )
                for ordinal, source_id in enumerate(candidate.source_refs)
            ]
        )

    def get_candidate(
        self, candidate_id: UUID, workspace_id: UUID
    ) -> KnowledgeCandidate | None:
        row = self._session.scalar(
            select(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.id == candidate_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
        )
        return self._row_to_candidate(row) if row is not None else None

    def get_candidate_for_update(
        self, candidate_id: UUID, workspace_id: UUID
    ) -> KnowledgeCandidate | None:
        row = self._session.scalar(
            select(KnowledgeCandidateModel)
            .where(
                KnowledgeCandidateModel.id == candidate_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        return self._row_to_candidate(row) if row is not None else None

    def _row_to_candidate(
        self, row: KnowledgeCandidateModel
    ) -> KnowledgeCandidate:
        snapshot = cast(dict[str, Any], row.proposed_snapshot)
        sections = tuple(
            KnowledgeSectionSnapshot(
                section_key=item["section_key"],
                heading=item["heading"],
                summary=item["summary"],
                body_markdown=item["body_markdown"],
                ordinal=item["ordinal"],
            )
            for item in cast(list[dict[str, Any]], snapshot["sections"])
        )
        relations = tuple(
            KnowledgeRelationSnapshot(
                relation_key=item["relation_key"],
                relation_kind=cast(Any, item["relation_kind"]),
                target_page_id=UUID(item["target_page_id"]),
                target_revision_id=(
                    UUID(item["target_revision_id"])
                    if item["target_revision_id"]
                    else None
                ),
            )
            for item in cast(list[dict[str, Any]], snapshot.get("relations", []))
        )
        return KnowledgeCandidate(
            candidate_id=row.id,
            workspace_id=row.workspace_id,
            operation=cast(Any, row.operation),
            target_page_id=row.target_page_id,
            expected_revision_id=row.expected_revision_id,
            expected_revision_hash=row.expected_revision_hash,
            proposed_title=snapshot["title"],
            proposed_slug=snapshot.get("slug", snapshot["title"]),
            proposed_summary=snapshot["summary"],
            proposed_tags=tuple(cast(list[str], snapshot.get("tags", []))),
            proposed_sections=sections,
            proposed_relations=relations,
            source_refs=tuple(UUID(item) for item in snapshot.get("source_refs", [])),
            producer_type=row.producer_type,
            producer_id=row.producer_id,
            task_id=row.task_id,
            subtask_id=row.subtask_id,
            worker_item_id=row.worker_item_id,
            execution_id=row.execution_id,
            supersedes_candidate_id=row.supersedes_candidate_id,
            status=cast(Any, row.status),
            version=row.version,
        )

    def transition_candidate(
        self,
        candidate_id: UUID,
        workspace_id: UUID,
        status: str,
        expected_version: int,
    ) -> None:
        row = self._session.scalar(
            select(KnowledgeCandidateModel)
            .where(
                KnowledgeCandidateModel.id == candidate_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if row is None:
            raise PersistenceNotFound("candidate not found in workspace")
        if row.version != expected_version:
            raise PersistenceConflict("candidate version conflict")
        row.status = status
        row.version += 1

    def add_review(
        self, review: KnowledgeCandidateReview, workspace_id: UUID
    ) -> None:
        self._session.add(
            KnowledgeCandidateReviewModel(
                id=review.review_id,
                workspace_id=workspace_id,
                candidate_id=review.candidate_id,
                reviewer_actor=review.reviewer_actor,
                decision=review.decision,
                reason=review.reason,
                observed_candidate_hash=review.observed_candidate_hash,
                observed_target_revision_id=review.observed_target_revision_id,
                created_revision_id=review.created_revision_id,
            )
        )


class SqlAlchemyCommandRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save_or_replay(self, command: CommandEnvelope) -> CommandModel:
        existing = self._session.scalar(
            select(CommandModel).where(
                CommandModel.id == command.command_id,
                CommandModel.workspace_id == command.workspace_id,
            )
        )
        if existing is not None:
            if existing.payload_hash != command.request_hash:
                raise PersistenceConflict("idempotency_conflict")
            return existing

        row = CommandModel(
            id=command.command_id,
            workspace_id=command.workspace_id,
            command_type=command.command_type,
            actor_type=command.actor_type,
            actor_id=command.actor_id,
            target_type=command.target_type,
            target_id=command.target_id,
            payload=command.payload,
            payload_hash=command.request_hash,
            expected_version=command.expected_version,
            idempotency_key=command.idempotency_key,
            correlation_id=command.correlation_id,
            status="pending",
        )
        self._session.add(row)
        return row

    def mark_applied(
        self, command_id: UUID, workspace_id: UUID, result: dict[str, object]
    ) -> None:
        row = self._session.scalar(
            select(CommandModel).where(
                CommandModel.id == command_id,
                CommandModel.workspace_id == workspace_id,
            )
        )
        if row is None:
            raise PersistenceNotFound("command not found in workspace")
        row.status = "applied"
        row.result = dict(result)
        row.applied_at = datetime.now().astimezone()

    def mark_rejected(
        self,
        command_id: UUID,
        workspace_id: UUID,
        error_code: str,
        details: dict[str, object],
    ) -> None:
        row = self._session.scalar(
            select(CommandModel).where(
                CommandModel.id == command_id,
                CommandModel.workspace_id == workspace_id,
            )
        )
        if row is None:
            raise PersistenceNotFound("command not found in workspace")
        row.status = "rejected"
        row.error_code = error_code
        row.result = dict(details)
        row.applied_at = datetime.now().astimezone()


class SqlAlchemyEventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append_with_outbox(self, event: EventEnvelope) -> None:
        self._session.add(
            EventModel(
                id=event.event_id,
                workspace_id=event.workspace_id,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                aggregate_version=event.aggregate_version,
                event_type=event.event_type,
                payload=event.payload,
                occurred_at=event.occurred_at,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
            )
        )
        self._session.add(
            OutboxMessageModel(
                id=UUID(int=event.event_id.int ^ 1),
                workspace_id=event.workspace_id,
                event_id=event.event_id,
                topic=topic_for_event(event.event_type),
                payload=event.payload,
                available_at=event.occurred_at,
                publish_attempts=0,
            )
        )


def translate_integrity_error(error: IntegrityError) -> PersistenceConflict:
    return PersistenceConflict("unique or foreign-key persistence conflict")
