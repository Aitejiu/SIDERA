from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, case, exists, func, literal, or_, select, text
from sqlalchemy.orm import Session

from sidera_knowledge.application.knowledge_queries import (
    MATERIALIZATION_MAX_SECTION_BYTES,
    MATERIALIZATION_MAX_SECTIONS,
    MATERIALIZATION_MAX_TOTAL_BYTES,
    CandidateDetail,
    CandidateList,
    CandidateSummary,
    CommandStatusView,
    KnowledgeMaterialization,
    KnowledgeMaterializationError,
    KnowledgeSectionRef,
    MaterializedKnowledgeSection,
    PageDetail,
    PageList,
    PageSummary,
    QueryValidationError,
    RelationView,
    RevisionDetail,
    RevisionSummary,
    SectionView,
    SourceList,
    SourceView,
    decode_cursor,
    diff_revisions,
    encode_candidate_cursor,
    encode_page_cursor,
    encode_source_cursor,
)
from sidera_knowledge.application.retrieval import (
    SearchFilters,
    SearchHit,
    validate_search_request,
)
from sidera_knowledge.domain.common import canonical_json_bytes, sha256_hex
from sidera_knowledge.domain.pages import (
    KnowledgeRelationSnapshot,
    KnowledgeSectionSnapshot,
)
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.models import (
    CommandModel,
    KnowledgeCandidateModel,
    KnowledgeCandidateSourceModel,
    KnowledgePageModel,
    KnowledgeRelationSnapshotModel,
    KnowledgeRevisionModel,
    KnowledgeRevisionSourceModel,
    KnowledgeSectionSnapshotModel,
    KnowledgeSourceModel,
)


def _source_view(row: KnowledgeSourceModel) -> SourceView:
    return SourceView(
        source_id=row.id,
        workspace_id=row.workspace_id,
        source_type=row.source_type,
        label=row.label,
        uri=row.uri,
        workspace_relative_path=row.workspace_relative_path,
        git_revision=row.git_revision,
        digest=row.digest,
        metadata=dict(cast(dict[str, Any], row.source_metadata)),
        availability=row.availability,
        safe_reason_code=row.safe_reason_code,
        last_checked_at=row.last_checked_at,
        version=row.version,
    )


def _section_view(row: KnowledgeSectionSnapshotModel) -> SectionView:
    return SectionView(
        section_key=row.section_key,
        heading=row.heading,
        summary=row.summary,
        body_markdown=row.body_markdown,
        ordinal=row.ordinal,
        section_hash=row.section_hash,
        content_byte_count=row.content_byte_count,
    )


def _relation_view(row: KnowledgeRelationSnapshotModel) -> RelationView:
    return RelationView(
        relation_key=row.relation_key,
        relation_kind=row.relation_kind,
        target_page_id=row.target_page_id,
        target_revision_id=row.target_revision_id,
    )


def _revision_summary(row: KnowledgeRevisionModel) -> RevisionSummary:
    return RevisionSummary(
        revision_id=row.id,
        page_id=row.page_id,
        workspace_id=row.workspace_id,
        revision_number=row.revision_number,
        title=row.title,
        summary=row.summary,
        tags=tuple(cast(list[str], row.tags)),
        content_hash=row.content_hash,
        change_reason=row.change_reason,
        created_by=row.created_by,
        created_at=row.created_at,
        accepted_candidate_id=row.accepted_candidate_id,
    )


def _source_rows(
    session: Session, workspace_id: UUID, source_ids: Iterable[UUID]
) -> dict[UUID, SourceView]:
    ids = tuple(source_ids)
    if not ids:
        return {}
    rows = session.scalars(
        select(KnowledgeSourceModel).where(
            KnowledgeSourceModel.workspace_id == workspace_id,
            KnowledgeSourceModel.id.in_(ids),
        )
    ).all()
    return {row.id: _source_view(row) for row in rows}


def _revision_detail(
    session: Session, row: KnowledgeRevisionModel
) -> RevisionDetail:
    section_rows = session.scalars(
        select(KnowledgeSectionSnapshotModel)
        .where(
            KnowledgeSectionSnapshotModel.revision_id == row.id,
            KnowledgeSectionSnapshotModel.workspace_id == row.workspace_id,
        )
        .order_by(
            KnowledgeSectionSnapshotModel.ordinal,
            KnowledgeSectionSnapshotModel.section_key,
        )
    ).all()
    relation_rows = session.scalars(
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
    source_refs = tuple(link.source_id for link in source_links)
    sources = _source_rows(session, row.workspace_id, source_refs)
    return RevisionDetail(
        revision_id=row.id,
        page_id=row.page_id,
        workspace_id=row.workspace_id,
        revision_number=row.revision_number,
        title=row.title,
        summary=row.summary,
        tags=tuple(cast(list[str], row.tags)),
        content_hash=row.content_hash,
        change_reason=row.change_reason,
        created_by=row.created_by,
        created_at=row.created_at,
        accepted_candidate_id=row.accepted_candidate_id,
        sections=tuple(_section_view(item) for item in section_rows),
        relations=tuple(_relation_view(item) for item in relation_rows),
        source_refs=source_refs,
        sources=tuple(sources[item] for item in source_refs if item in sources),
    )


def _page_summary(
    session: Session, row: KnowledgePageModel
) -> PageSummary:
    current = None
    if row.current_revision_id is not None:
        current_row = session.scalar(
            select(KnowledgeRevisionModel).where(
                KnowledgeRevisionModel.id == row.current_revision_id,
                KnowledgeRevisionModel.workspace_id == row.workspace_id,
            )
        )
        if current_row is not None:
            current = _revision_summary(current_row)
    return PageSummary(
        page_id=row.id,
        workspace_id=row.workspace_id,
        canonical_title=row.canonical_title,
        slug=row.slug,
        status=row.status,
        version=row.version,
        current_revision=current,
        updated_at=row.updated_at,
    )


def _candidate_summary(row: KnowledgeCandidateModel) -> CandidateSummary:
    snapshot = cast(dict[str, Any], row.proposed_snapshot)
    return CandidateSummary(
        candidate_id=row.id,
        workspace_id=row.workspace_id,
        operation=row.operation,
        target_page_id=row.target_page_id,
        expected_revision_id=row.expected_revision_id,
        expected_revision_hash=row.expected_revision_hash,
        proposed_title=str(snapshot["title"]),
        proposed_slug=str(snapshot["slug"]),
        candidate_hash=row.candidate_hash,
        status=row.status,
        producer_type=row.producer_type,
        producer_id=row.producer_id,
        task_id=row.task_id,
        subtask_id=row.subtask_id,
        worker_item_id=row.worker_item_id,
        execution_id=row.execution_id,
        supersedes_candidate_id=row.supersedes_candidate_id,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@dataclass(frozen=True)
class _CandidateRevision:
    revision_id: UUID
    page_id: UUID
    workspace_id: UUID
    title: str
    summary: str
    tags: tuple[str, ...]
    sections: tuple[SectionView, ...]
    relations: tuple[RelationView, ...]
    source_refs: tuple[UUID, ...]


def _candidate_revision(
    row: KnowledgeCandidateModel,
) -> _CandidateRevision:
    if row.target_page_id is None:
        raise ValueError("revise candidate has no target page")
    snapshot = cast(dict[str, Any], row.proposed_snapshot)
    sections: list[SectionView] = []
    for item in cast(list[dict[str, Any]], snapshot["sections"]):
        section = KnowledgeSectionSnapshot(
            section_key=str(item["section_key"]),
            heading=str(item["heading"]),
            summary=str(item.get("summary", "")),
            body_markdown=str(item["body_markdown"]),
            ordinal=int(item["ordinal"]),
        )
        sections.append(
            SectionView(
                section_key=section.section_key,
                heading=section.heading,
                summary=section.summary,
                body_markdown=section.body_markdown,
                ordinal=section.ordinal,
                section_hash=section.section_hash,
                content_byte_count=section.utf8_byte_count,
            )
        )
    relations: list[RelationView] = []
    for item in cast(list[dict[str, Any]], snapshot.get("relations", [])):
        relation = KnowledgeRelationSnapshot(
            relation_key=str(item["relation_key"]),
            relation_kind=cast(Any, item["relation_kind"]),
            target_page_id=UUID(str(item["target_page_id"])),
            target_revision_id=(
                UUID(str(item["target_revision_id"]))
                if item.get("target_revision_id")
                else None
            ),
        )
        relations.append(
            RelationView(
                relation_key=relation.relation_key,
                relation_kind=relation.relation_kind,
                target_page_id=relation.target_page_id,
                target_revision_id=relation.target_revision_id,
            )
        )
    source_refs = tuple(
        UUID(str(item)) for item in cast(list[str], snapshot.get("source_refs", []))
    )
    return _CandidateRevision(
        revision_id=row.id,
        page_id=row.target_page_id,
        workspace_id=row.workspace_id,
        title=str(snapshot["title"]),
        summary=str(snapshot.get("summary", "")),
        tags=tuple(cast(list[str], snapshot.get("tags", []))),
        sections=tuple(sections),
        relations=tuple(relations),
        source_refs=source_refs,
    )


class SqlAlchemyKnowledgeQueryStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def list_pages(
        self,
        workspace_id: UUID,
        *,
        include_archived: bool,
        cursor: str | None,
        limit: int,
    ) -> PageList:
        with self._database.session() as session:
            statement = select(KnowledgePageModel).where(
                KnowledgePageModel.workspace_id == workspace_id
            )
            if not include_archived:
                statement = statement.where(KnowledgePageModel.status == "active")
            decoded = decode_cursor(cursor, 2)
            if decoded is not None:
                slug, page_id = decoded
                try:
                    page_uuid = UUID(page_id)
                except ValueError as exc:
                    raise QueryValidationError("invalid cursor") from exc
                statement = statement.where(
                    or_(
                        KnowledgePageModel.slug > slug,
                        and_(
                            KnowledgePageModel.slug == slug,
                            KnowledgePageModel.id > page_uuid,
                        ),
                    )
                )
            rows = session.scalars(
                statement.order_by(
                    KnowledgePageModel.slug,
                    KnowledgePageModel.id,
                ).limit(limit + 1)
            ).all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            return PageList(
                items=tuple(_page_summary(session, row) for row in rows),
                next_cursor=(
                    encode_page_cursor(rows[-1].slug, rows[-1].id)
                    if has_more and rows
                    else None
                ),
            )

    def get_page(self, workspace_id: UUID, page_id: UUID) -> PageDetail | None:
        with self._database.session() as session:
            page = session.scalar(
                select(KnowledgePageModel).where(
                    KnowledgePageModel.id == page_id,
                    KnowledgePageModel.workspace_id == workspace_id,
                )
            )
            if page is None:
                return None
            current = None
            if page.current_revision_id is not None:
                current_row = session.scalar(
                    select(KnowledgeRevisionModel).where(
                        KnowledgeRevisionModel.id == page.current_revision_id,
                        KnowledgeRevisionModel.workspace_id == workspace_id,
                    )
                )
                if current_row is not None:
                    current = _revision_detail(session, current_row)
            history_rows = session.scalars(
                select(KnowledgeRevisionModel)
                .where(
                    KnowledgeRevisionModel.page_id == page_id,
                    KnowledgeRevisionModel.workspace_id == workspace_id,
                )
                .order_by(KnowledgeRevisionModel.revision_number.desc())
            ).all()
            return PageDetail(
                summary=_page_summary(session, page),
                current_revision=current,
                revision_history=tuple(_revision_summary(row) for row in history_rows),
            )

    def get_revision(
        self, workspace_id: UUID, page_id: UUID, revision_id: UUID
    ) -> RevisionDetail | None:
        with self._database.session() as session:
            row = session.scalar(
                select(KnowledgeRevisionModel).where(
                    KnowledgeRevisionModel.id == revision_id,
                    KnowledgeRevisionModel.page_id == page_id,
                    KnowledgeRevisionModel.workspace_id == workspace_id,
                )
            )
            return _revision_detail(session, row) if row is not None else None

    def get_command_status(
        self, workspace_id: UUID, command_id: UUID
    ) -> CommandStatusView:
        with self._database.session() as session:
            row = session.scalar(
                select(CommandModel).where(
                    CommandModel.id == command_id,
                    CommandModel.workspace_id == workspace_id,
                )
            )
            if row is None:
                return CommandStatusView(
                    command_id=command_id,
                    workspace_id=workspace_id,
                    status="pending",
                    result=None,
                    error_code=None,
                )
            return CommandStatusView(
                command_id=row.id,
                workspace_id=row.workspace_id,
                status=row.status,
                result=(dict(row.result or {}) if row.status == "applied" else None),
                error_code=row.error_code if row.status == "rejected" else None,
            )

    def materialize_sections(
        self,
        workspace_id: UUID,
        refs: tuple[KnowledgeSectionRef, ...],
    ) -> KnowledgeMaterialization:
        if len(refs) > MATERIALIZATION_MAX_SECTIONS:
            raise KnowledgeMaterializationError(
                "content_limit_exceeded",
                "materialization cannot contain more than 32 sections",
            )
        if len({ref.ordinal for ref in refs}) != len(refs):
            raise KnowledgeMaterializationError(
                "source_invalid", "section ordinals must be unique"
            )
        identities = {
            (ref.page_id, ref.revision_id, ref.section_key) for ref in refs
        }
        if len(identities) != len(refs):
            raise KnowledgeMaterializationError(
                "source_invalid", "section references must be unique"
            )
        if not refs:
            return KnowledgeMaterialization(sections=(), total_byte_count=0)

        for ref in refs:
            if ref.source_ids != tuple(sorted(ref.source_ids, key=str)):
                raise KnowledgeMaterializationError(
                    "source_invalid", "source_ids must use stable sorted order"
                )

        with self._database.session() as session:
            # Materialization is a bounded read; keep a database-side ceiling even
            # when the caller's HTTP timeout is misconfigured.
            session.execute(text("SET LOCAL statement_timeout = '30000ms'"))
            conditions = tuple(
                and_(
                    KnowledgeRevisionModel.id == ref.revision_id,
                    KnowledgeRevisionModel.page_id == ref.page_id,
                    KnowledgeSectionSnapshotModel.revision_id == ref.revision_id,
                    KnowledgeSectionSnapshotModel.section_key == ref.section_key,
                )
                for ref in refs
            )
            rows = session.execute(
                select(
                    KnowledgeRevisionModel,
                    KnowledgePageModel,
                    KnowledgeSectionSnapshotModel,
                )
                .join(
                    KnowledgePageModel,
                    and_(
                        KnowledgePageModel.id == KnowledgeRevisionModel.page_id,
                        KnowledgePageModel.workspace_id
                        == KnowledgeRevisionModel.workspace_id,
                    ),
                )
                .join(
                    KnowledgeSectionSnapshotModel,
                    and_(
                        KnowledgeSectionSnapshotModel.revision_id
                        == KnowledgeRevisionModel.id,
                        KnowledgeSectionSnapshotModel.workspace_id
                        == KnowledgeRevisionModel.workspace_id,
                    ),
                )
                .where(
                    KnowledgeRevisionModel.workspace_id == workspace_id,
                    KnowledgePageModel.workspace_id == workspace_id,
                    KnowledgeSectionSnapshotModel.workspace_id == workspace_id,
                    or_(*conditions),
                )
            ).all()
            rows_by_identity = {
                (revision.page_id, revision.id, section.section_key): (
                    revision,
                    page,
                    section,
                )
                for revision, page, section in rows
            }
            if len(rows_by_identity) != len(refs):
                raise KnowledgeMaterializationError(
                    "not_found", "one or more exact knowledge references were not found"
                )

            revision_ids = tuple({ref.revision_id for ref in refs})
            source_rows = session.execute(
                select(KnowledgeRevisionSourceModel, KnowledgeSourceModel)
                .outerjoin(
                    KnowledgeSourceModel,
                    and_(
                        KnowledgeSourceModel.id
                        == KnowledgeRevisionSourceModel.source_id,
                        KnowledgeSourceModel.workspace_id
                        == KnowledgeRevisionSourceModel.workspace_id,
                    ),
                )
                .where(
                    KnowledgeRevisionSourceModel.workspace_id == workspace_id,
                    KnowledgeRevisionSourceModel.revision_id.in_(revision_ids),
                )
                .order_by(
                    KnowledgeRevisionSourceModel.revision_id,
                    KnowledgeRevisionSourceModel.ordinal,
                )
            ).all()
            sources_by_revision: dict[UUID, list[SourceView]] = {}
            for link, source in source_rows:
                if source is None:
                    raise KnowledgeMaterializationError(
                        "source_invalid", "a revision source is unavailable"
                    )
                sources_by_revision.setdefault(link.revision_id, []).append(
                    _source_view(source)
                )

            materialized: list[MaterializedKnowledgeSection] = []
            total_byte_count = 0
            for ref in refs:
                revision, _, section = rows_by_identity[
                    (ref.page_id, ref.revision_id, ref.section_key)
                ]
                revision_hash = sha256_hex(canonical_json_bytes(revision.snapshot))
                if revision_hash != revision.content_hash or ref.revision_hash != revision_hash:
                    raise KnowledgeMaterializationError(
                        "hash_mismatch", "revision hash does not match the exact snapshot"
                    )

                section_snapshot = KnowledgeSectionSnapshot(
                    section_key=section.section_key,
                    heading=section.heading,
                    summary=section.summary,
                    body_markdown=section.body_markdown,
                    ordinal=section.ordinal,
                )
                section_byte_count = len(section.body_markdown.encode("utf-8"))
                if (
                    section_byte_count != section.content_byte_count
                    or section_snapshot.section_hash != section.section_hash
                    or ref.section_hash != section.section_hash
                ):
                    raise KnowledgeMaterializationError(
                        "hash_mismatch", "section hash does not match the exact snapshot"
                    )
                if section_byte_count > MATERIALIZATION_MAX_SECTION_BYTES:
                    raise KnowledgeMaterializationError(
                        "content_limit_exceeded",
                        "a materialized section exceeds the byte limit",
                    )
                total_byte_count += section_byte_count
                if total_byte_count > MATERIALIZATION_MAX_TOTAL_BYTES:
                    raise KnowledgeMaterializationError(
                        "content_limit_exceeded",
                        "materialization exceeds the total byte limit",
                    )

                sources = tuple(sources_by_revision.get(revision.id, ()))
                source_ids = tuple(source.source_id for source in sources)
                if source_ids != ref.source_ids:
                    raise KnowledgeMaterializationError(
                        "source_invalid", "source_ids do not match the revision manifest"
                    )
                trust = (
                    "external_reference"
                    if any(source.source_type == "external_url" for source in sources)
                    else "workspace_confirmed"
                )
                materialized.append(
                    MaterializedKnowledgeSection(
                        ordinal=ref.ordinal,
                        page_id=revision.page_id,
                        revision_id=revision.id,
                        revision_hash=revision_hash,
                        section_key=section.section_key,
                        section_hash=section.section_hash,
                        body_markdown=section.body_markdown,
                        sources=sources,
                        trust=trust,
                        section_byte_count=section_byte_count,
                    )
                )

            return KnowledgeMaterialization(
                sections=tuple(sorted(materialized, key=lambda item: item.ordinal)),
                total_byte_count=total_byte_count,
            )

    @staticmethod
    def _source_filter(
        candidate_id: Any,
        workspace_id: UUID,
        key: str,
        value: UUID,
    ) -> Any:
        return exists(
            select(1)
            .select_from(KnowledgeCandidateSourceModel)
            .join(
                KnowledgeSourceModel,
                and_(
                    KnowledgeSourceModel.id == KnowledgeCandidateSourceModel.source_id,
                    KnowledgeSourceModel.workspace_id
                    == KnowledgeCandidateSourceModel.workspace_id,
                ),
            )
            .where(
                KnowledgeCandidateSourceModel.candidate_id == candidate_id,
                KnowledgeCandidateSourceModel.workspace_id == workspace_id,
                KnowledgeSourceModel.source_metadata[key].as_string() == str(value),
            )
        )

    def list_candidates(
        self,
        workspace_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        status: str | None,
        source_task_id: UUID | None,
        source_subtask_id: UUID | None,
        source_worker_item_id: UUID | None,
        source_execution_id: UUID | None,
        source_artifact_id: UUID | None,
        source_evidence_id: UUID | None,
    ) -> CandidateList:
        filters = (
            ("task_id", source_task_id),
            ("subtask_id", source_subtask_id),
            ("worker_item_id", source_worker_item_id),
            ("execution_id", source_execution_id),
            ("artifact_id", source_artifact_id),
            ("evidence_id", source_evidence_id),
        )
        with self._database.session() as session:
            statement = select(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.workspace_id == workspace_id
            )
            if status is not None:
                statement = statement.where(KnowledgeCandidateModel.status == status)
            for key, value in filters:
                if value is not None:
                    statement = statement.where(
                        self._source_filter(
                            KnowledgeCandidateModel.id, workspace_id, key, value
                        )
                    )
            decoded = decode_cursor(cursor, 2)
            if decoded is not None:
                try:
                    created_at = datetime.fromisoformat(decoded[0])
                    candidate_id = UUID(decoded[1])
                except ValueError as exc:
                    raise QueryValidationError("invalid cursor") from exc
                statement = statement.where(
                    or_(
                        KnowledgeCandidateModel.created_at < created_at,
                        and_(
                            KnowledgeCandidateModel.created_at == created_at,
                            KnowledgeCandidateModel.id < candidate_id,
                        ),
                    )
                )
            rows = session.scalars(
                statement.order_by(
                    KnowledgeCandidateModel.created_at.desc(),
                    KnowledgeCandidateModel.id.desc(),
                ).limit(limit + 1)
            ).all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            return CandidateList(
                items=tuple(_candidate_summary(row) for row in rows),
                next_cursor=(
                    encode_candidate_cursor(rows[-1].created_at, rows[-1].id)
                    if has_more and rows
                    else None
                ),
            )

    def get_candidate(
        self, workspace_id: UUID, candidate_id: UUID
    ) -> CandidateDetail | None:
        with self._database.session() as session:
            row = session.scalar(
                select(KnowledgeCandidateModel).where(
                    KnowledgeCandidateModel.id == candidate_id,
                    KnowledgeCandidateModel.workspace_id == workspace_id,
                )
            )
            if row is None:
                return None
            links = session.scalars(
                select(KnowledgeCandidateSourceModel)
                .where(
                    KnowledgeCandidateSourceModel.candidate_id == candidate_id,
                    KnowledgeCandidateSourceModel.workspace_id == workspace_id,
                )
                .order_by(KnowledgeCandidateSourceModel.ordinal)
            ).all()
            source_refs = tuple(link.source_id for link in links)
            sources = _source_rows(session, workspace_id, source_refs)
            observed_row = None
            if row.expected_revision_id is not None:
                observed_row = session.scalar(
                    select(KnowledgeRevisionModel).where(
                        KnowledgeRevisionModel.id == row.expected_revision_id,
                        KnowledgeRevisionModel.workspace_id == workspace_id,
                    )
                )
            current_row = None
            if row.target_page_id is not None:
                page = session.scalar(
                    select(KnowledgePageModel).where(
                        KnowledgePageModel.id == row.target_page_id,
                        KnowledgePageModel.workspace_id == workspace_id,
                    )
                )
                if page is not None and page.current_revision_id is not None:
                    current_row = session.scalar(
                        select(KnowledgeRevisionModel).where(
                            KnowledgeRevisionModel.id == page.current_revision_id,
                            KnowledgeRevisionModel.workspace_id == workspace_id,
                        )
                    )
            observed = _revision_summary(observed_row) if observed_row else None
            current = _revision_summary(current_row) if current_row else None
            stale = row.operation == "revise_page" and (
                current is None
                or current.revision_id != row.expected_revision_id
                or current.content_hash != row.expected_revision_hash
            )
            revision_diff = None
            if observed_row is not None and row.operation == "revise_page":
                revision_diff = diff_revisions(
                    _revision_detail(session, observed_row), _candidate_revision(row)
                )
            return CandidateDetail(
                summary=_candidate_summary(row),
                proposed_snapshot=dict(cast(dict[str, Any], row.proposed_snapshot)),
                provenance={
                    "operation": row.operation,
                    "target_page_id": str(row.target_page_id)
                    if row.target_page_id
                    else None,
                    "expected_revision_id": str(row.expected_revision_id)
                    if row.expected_revision_id
                    else None,
                    "expected_revision_hash": row.expected_revision_hash,
                    "producer_type": row.producer_type,
                    "producer_id": row.producer_id,
                    "task_id": str(row.task_id) if row.task_id else None,
                    "subtask_id": str(row.subtask_id) if row.subtask_id else None,
                    "worker_item_id": str(row.worker_item_id)
                    if row.worker_item_id
                    else None,
                    "execution_id": str(row.execution_id)
                    if row.execution_id
                    else None,
                },
                sources=tuple(sources[item] for item in source_refs if item in sources),
                observed_target_revision=observed,
                current_target_revision=current,
                target_is_stale=stale,
                diff=revision_diff,
            )

    def list_sources(
        self,
        workspace_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        availability: str | None,
    ) -> SourceList:
        with self._database.session() as session:
            statement = select(KnowledgeSourceModel).where(
                KnowledgeSourceModel.workspace_id == workspace_id
            )
            if availability is not None:
                statement = statement.where(
                    KnowledgeSourceModel.availability == availability
                )
            decoded = decode_cursor(cursor, 2)
            if decoded is not None:
                try:
                    source_id = UUID(decoded[1])
                except ValueError as exc:
                    raise QueryValidationError("invalid cursor") from exc
                label = decoded[0]
                statement = statement.where(
                    or_(
                        KnowledgeSourceModel.label > label,
                        and_(
                            KnowledgeSourceModel.label == label,
                            KnowledgeSourceModel.id > source_id,
                        ),
                    )
                )
            rows = session.scalars(
                statement.order_by(
                    KnowledgeSourceModel.label,
                    KnowledgeSourceModel.id,
                ).limit(limit + 1)
            ).all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            return SourceList(
                items=tuple(_source_view(row) for row in rows),
                next_cursor=(
                    encode_source_cursor(rows[-1].label, rows[-1].id)
                    if has_more and rows
                    else None
                ),
            )


class PostgresRetrievalAdapter:
    """PostgreSQL FTS adapter that returns bounded metadata-only search hits."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def search(
        self,
        workspace_id: UUID,
        query: str,
        filters: SearchFilters,
        limit: int = 20,
    ) -> tuple[SearchHit, ...]:
        validate_search_request(query, filters, limit)
        normalized_query = query.strip()

        with self._database.session() as session:
            if normalized_query:
                ts_query = func.websearch_to_tsquery("simple", normalized_query)
                matches_query = KnowledgeRevisionModel.search_vector.op("@@")(
                    ts_query
                )
                title_match = func.to_tsvector(
                    "simple", KnowledgeRevisionModel.title
                ).op("@@")(ts_query)
                rank = (
                    func.ts_rank_cd(
                        KnowledgeRevisionModel.search_vector,
                        ts_query,
                    )
                    + case((title_match, 1.0), else_=0.0)
                ).label("rank")
            else:
                matches_query = literal(True)
                rank = literal(0.0).label("rank")

            statement = (
                select(
                    KnowledgeRevisionModel,
                    KnowledgePageModel,
                    KnowledgeSectionSnapshotModel,
                    rank,
                )
                .join(
                    KnowledgePageModel,
                    and_(
                        KnowledgePageModel.id == KnowledgeRevisionModel.page_id,
                        KnowledgePageModel.workspace_id
                        == KnowledgeRevisionModel.workspace_id,
                    ),
                )
                .join(
                    KnowledgeSectionSnapshotModel,
                    and_(
                        KnowledgeSectionSnapshotModel.revision_id
                        == KnowledgeRevisionModel.id,
                        KnowledgeSectionSnapshotModel.workspace_id
                        == KnowledgeRevisionModel.workspace_id,
                    ),
                )
                .where(
                    KnowledgeRevisionModel.workspace_id == workspace_id,
                    KnowledgePageModel.workspace_id == workspace_id,
                    KnowledgePageModel.current_revision_id
                    == KnowledgeRevisionModel.id,
                    matches_query,
                )
            )
            if filters.status is not None:
                statement = statement.where(
                    KnowledgePageModel.status == filters.status
                )
            if filters.tags:
                statement = statement.where(
                    KnowledgeRevisionModel.tags.contains(list(filters.tags))
                )
            if filters.source_types:
                statement = statement.where(
                    exists(
                        select(1)
                        .select_from(KnowledgeRevisionSourceModel)
                        .join(
                            KnowledgeSourceModel,
                            and_(
                                KnowledgeSourceModel.id
                                == KnowledgeRevisionSourceModel.source_id,
                                KnowledgeSourceModel.workspace_id
                                == KnowledgeRevisionSourceModel.workspace_id,
                            ),
                        )
                        .where(
                            KnowledgeRevisionSourceModel.revision_id
                            == KnowledgeRevisionModel.id,
                            KnowledgeRevisionSourceModel.workspace_id == workspace_id,
                            KnowledgeSourceModel.source_type.in_(filters.source_types),
                        )
                    )
                )

            rows = session.execute(
                statement.order_by(
                    rank.desc(),
                    KnowledgeRevisionModel.title,
                    KnowledgePageModel.id,
                    KnowledgeSectionSnapshotModel.ordinal,
                    KnowledgeSectionSnapshotModel.section_key,
                ).limit(limit)
            ).all()
            if not rows:
                return ()

            revision_ids = tuple({revision.id for revision, _, _, _ in rows})
            source_rows = session.execute(
                select(
                    KnowledgeRevisionSourceModel.revision_id,
                    KnowledgeSourceModel.id,
                    KnowledgeSourceModel.source_type,
                    KnowledgeSourceModel.availability,
                )
                .join(
                    KnowledgeSourceModel,
                    and_(
                        KnowledgeSourceModel.id
                        == KnowledgeRevisionSourceModel.source_id,
                        KnowledgeSourceModel.workspace_id
                        == KnowledgeRevisionSourceModel.workspace_id,
                    ),
                )
                .where(
                    KnowledgeRevisionSourceModel.workspace_id == workspace_id,
                    KnowledgeRevisionSourceModel.revision_id.in_(revision_ids),
                )
                .order_by(
                    KnowledgeRevisionSourceModel.revision_id,
                    KnowledgeRevisionSourceModel.ordinal,
                )
            ).all()

            sources_by_revision: dict[UUID, list[tuple[UUID, str, str]]] = {}
            for revision_id, source_id, source_type, availability in source_rows:
                sources_by_revision.setdefault(revision_id, []).append(
                    (source_id, source_type, availability)
                )

            hits: list[SearchHit] = []
            for revision, page, section, row_rank in rows:
                sources = sources_by_revision.get(revision.id, [])
                source_ids = tuple(item[0] for item in sources)
                source_types = tuple(item[1] for item in sources)
                source_availability = tuple(item[2] for item in sources)
                trust = (
                    "external_reference"
                    if "external_url" in source_types
                    else "workspace_confirmed"
                )
                hits.append(
                    SearchHit(
                        page_id=page.id,
                        revision_id=revision.id,
                        section_key=section.section_key,
                        revision_number=revision.revision_number,
                        revision_hash=revision.content_hash,
                        section_hash=section.section_hash,
                        title=revision.title,
                        summary=revision.summary,
                        tags=tuple(revision.tags),
                        source_ids=source_ids,
                        source_types=source_types,
                        source_availability=source_availability,
                        trust=trust,
                        section_byte_count=section.content_byte_count,
                        rank=float(row_rank or 0.0),
                        rank_explanation="weighted title/tags > summary > section body",
                    )
                )
            return tuple(hits)
