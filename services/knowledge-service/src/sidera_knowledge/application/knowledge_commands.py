from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sidera_knowledge.domain.candidates import (
    KnowledgeCandidate,
    KnowledgeCandidateReview,
)
from sidera_knowledge.domain.commands import CommandEnvelope, EventEnvelope
from sidera_knowledge.domain.common import (
    DomainValidationError,
    canonical_json_bytes,
    validate_sha256,
)
from sidera_knowledge.domain.knowledge_errors import CommandFailure
from sidera_knowledge.domain.pages import (
    KnowledgePage,
    KnowledgeRelationSnapshot,
    KnowledgeRevision,
    KnowledgeSectionSnapshot,
    KnowledgeSource,
)
from sidera_knowledge.persistence.repositories import (
    PersistenceConflict,
    PersistenceNotFound,
)

CANDIDATE_SCHEMA_VERSION = 1
DEFAULT_MAX_CANDIDATE_BYTES = 256 * 1024
HARD_MAX_CANDIDATE_BYTES = 512 * 1024
DEFAULT_MAX_SECTION_BYTES = 32 * 1024
HARD_MAX_SECTION_BYTES = 128 * 1024
DEFAULT_MAX_SECTIONS = 32
HARD_MAX_SECTIONS = 128


def _uuid(value: object, field_name: str, *, required: bool = True) -> UUID | None:
    if value is None and not required:
        return None
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CommandFailure("candidate_invalid", {"field": field_name}) from exc


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommandFailure("candidate_invalid", {"field": field_name})
    return value


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CommandFailure("candidate_invalid", {"field": field_name})
    return value


def _source_allowed(actor_type: str, source_type: str) -> bool:
    if actor_type == "local_owner":
        return True
    if actor_type == "main_agent":
        return source_type in {"manual", "markdown_file", "external_url"}
    if actor_type == "control_plane":
        return source_type in {"task", "artifact", "evidence"}
    return False


def _parse_optional_provenance_id(
    provenance: Mapping[str, Any], key: str
) -> UUID | None:
    value = provenance.get(key)
    if value is None:
        return None
    return _uuid(value, f"provenance.{key}", required=False)


class SubmitKnowledgeCandidateHandler:
    def __init__(
        self,
        *,
        max_candidate_bytes: int = DEFAULT_MAX_CANDIDATE_BYTES,
        max_section_bytes: int = DEFAULT_MAX_SECTION_BYTES,
        max_sections: int = DEFAULT_MAX_SECTIONS,
    ) -> None:
        self._max_candidate_bytes = min(max_candidate_bytes, HARD_MAX_CANDIDATE_BYTES)
        self._max_section_bytes = min(max_section_bytes, HARD_MAX_SECTION_BYTES)
        self._max_sections = min(max_sections, HARD_MAX_SECTIONS)

    def handle(
        self, command: CommandEnvelope, uow: Any
    ) -> dict[str, Any]:
        payload = _mapping(command.payload, "payload")
        if payload.get("candidate_schema_version") != CANDIDATE_SCHEMA_VERSION:
            raise CommandFailure("candidate_invalid", {"field": "candidate_schema_version"})
        candidate_id = _uuid(payload.get("candidate_id"), "candidate_id")
        assert candidate_id is not None
        operation = payload.get("operation")
        if operation not in {"create_page", "revise_page"}:
            raise CommandFailure("candidate_invalid", {"field": "operation"})

        target_page_id = _uuid(
            payload.get("target_page_id"), "target_page_id", required=False
        )
        expected_revision_id = _uuid(
            payload.get("expected_revision_id"), "expected_revision_id", required=False
        )
        expected_revision_hash = payload.get("expected_revision_hash")
        if operation == "create_page":
            if any(
                value is not None
                for value in (target_page_id, expected_revision_id, expected_revision_hash)
            ):
                raise CommandFailure("candidate_invalid", {"field": "target"})
        else:
            if target_page_id is None or expected_revision_id is None:
                raise CommandFailure("candidate_invalid", {"field": "target"})
            if not isinstance(expected_revision_hash, str):
                raise CommandFailure("candidate_invalid", {"field": "expected_revision_hash"})
            try:
                expected_revision_hash = validate_sha256(
                    expected_revision_hash, "expected_revision_hash"
                )
            except DomainValidationError as exc:
                raise CommandFailure(
                    "candidate_invalid", {"field": "expected_revision_hash"}
                ) from exc

        sections_payload = payload.get("sections")
        if not isinstance(sections_payload, list) or not sections_payload:
            raise CommandFailure("candidate_invalid", {"field": "sections"})
        if len(sections_payload) > self._max_sections:
            raise CommandFailure("content_limit_exceeded", {"field": "sections"})
        sections: list[KnowledgeSectionSnapshot] = []
        try:
            for item in sections_payload:
                section = _mapping(item, "sections")
                parsed = KnowledgeSectionSnapshot(
                    section_key=_text(section.get("section_key"), "section_key"),
                    heading=_text(section.get("heading"), "heading"),
                    summary=cast(str, section.get("summary", "")),
                    body_markdown=_text(
                        section.get("body_markdown"), "body_markdown"
                    ),
                    ordinal=int(section.get("ordinal", len(sections))),
                )
                if parsed.utf8_byte_count > self._max_section_bytes:
                    raise CommandFailure(
                        "content_limit_exceeded", {"field": "section.body_markdown"}
                    )
                if parsed.utf8_byte_count > HARD_MAX_SECTION_BYTES:
                    raise CommandFailure(
                        "content_limit_exceeded", {"field": "section.body_markdown"}
                    )
                sections.append(parsed)
        except CommandFailure:
            raise
        except (DomainValidationError, TypeError, ValueError) as exc:
            raise CommandFailure("candidate_invalid", {"field": "sections"}) from exc

        relations: list[KnowledgeRelationSnapshot] = []
        try:
            for item in payload.get("relations", []):
                relation = _mapping(item, "relations")
                target = _uuid(relation.get("target_page_id"), "target_page_id")
                assert target is not None
                relation_target_revision = _uuid(
                    relation.get("target_revision_id"),
                    "target_revision_id",
                    required=False,
                )
                relations.append(
                    KnowledgeRelationSnapshot(
                        relation_key=_text(relation.get("relation_key"), "relation_key"),
                        relation_kind=cast(Any, relation.get("relation_kind")),
                        target_page_id=target,
                        target_revision_id=relation_target_revision,
                    )
                )
        except (DomainValidationError, TypeError, ValueError) as exc:
            raise CommandFailure("candidate_invalid", {"field": "relations"}) from exc

        provenance = _mapping(payload.get("provenance", {}), "provenance")
        producer_type = str(provenance.get("producer_type", command.actor_type))
        producer_id = str(provenance.get("producer_id", command.actor_id))
        supersedes_candidate_id = _uuid(
            payload.get("supersedes_candidate_id"),
            "supersedes_candidate_id",
            required=False,
        )

        source_refs: list[UUID] = []
        new_sources: list[KnowledgeSource] = []
        sources = payload.get("sources", [])
        if not isinstance(sources, list):
            raise CommandFailure("candidate_invalid", {"field": "sources"})
        for item in sources:
            source_payload = _mapping(item, "sources")
            source_type = _text(source_payload.get("source_type"), "source_type")
            if not _source_allowed(command.actor_type, source_type):
                raise CommandFailure("permission_denied", {"source_type": source_type})
            metadata = dict(_mapping(source_payload.get("metadata", {}), "metadata"))
            self._validate_operational_source(
                command.actor_type, source_type, provenance, metadata
            )
            source_id = _uuid(
                source_payload.get("source_id"), "source_id", required=False
            ) or uuid4()
            digest = source_payload.get("digest")
            if digest is not None and not isinstance(digest, str):
                raise CommandFailure("source_invalid", {"field": "digest"})
            existing = (
                uow.sources.find_by_digest(command.workspace_id, source_type, digest)
                if digest
                else None
            )
            if existing is not None:
                source_id = existing.source_id
            else:
                try:
                    source = KnowledgeSource(
                        source_id=source_id,
                        workspace_id=command.workspace_id,
                        source_type=cast(Any, source_type),
                        label=_text(source_payload.get("label"), "source.label"),
                        uri=cast(str | None, source_payload.get("uri")),
                        workspace_relative_path=cast(
                            str | None, source_payload.get("workspace_relative_path")
                        ),
                        git_revision=cast(
                            str | None, source_payload.get("git_revision")
                        ),
                        digest=digest,
                        metadata=metadata,
                    )
                except (DomainValidationError, TypeError, ValueError) as exc:
                    raise CommandFailure("source_invalid", {"source_type": source_type}) from exc
                new_sources.append(source)
            source_refs.append(source_id)

        try:
            candidate = KnowledgeCandidate(
                candidate_id=candidate_id,
                workspace_id=command.workspace_id,
                operation=cast(Any, operation),
                target_page_id=target_page_id,
                expected_revision_id=expected_revision_id,
                expected_revision_hash=cast(str | None, expected_revision_hash),
                proposed_title=_text(payload.get("title"), "title"),
                proposed_slug=_text(payload.get("slug"), "slug"),
                proposed_summary=cast(str, payload.get("summary", "")),
                proposed_tags=tuple(cast(list[str], payload.get("tags", []))),
                proposed_sections=tuple(sections),
                proposed_relations=tuple(relations),
                source_refs=tuple(source_refs),
                producer_type=producer_type,
                producer_id=producer_id,
                task_id=_parse_optional_provenance_id(provenance, "task_id"),
                subtask_id=_parse_optional_provenance_id(provenance, "subtask_id"),
                worker_item_id=_parse_optional_provenance_id(provenance, "worker_item_id"),
                execution_id=_parse_optional_provenance_id(provenance, "execution_id"),
                supersedes_candidate_id=supersedes_candidate_id,
            )
            snapshot_size = len(canonical_json_bytes(candidate.proposed_snapshot))
        except CommandFailure:
            raise
        except (DomainValidationError, TypeError, ValueError) as exc:
            raise CommandFailure("candidate_invalid", {"field": "snapshot"}) from exc
        if snapshot_size > self._max_candidate_bytes or snapshot_size > HARD_MAX_CANDIDATE_BYTES:
            raise CommandFailure("content_limit_exceeded", {"field": "candidate"})

        for source in new_sources:
            uow.sources.add_source(source)
        if new_sources:
            uow.flush()
        uow.candidates.add_candidate(candidate)
        uow.events.append_with_outbox(
            EventEnvelope(
                event_id=uuid4(),
                workspace_id=command.workspace_id,
                aggregate_type="knowledge_candidate",
                aggregate_id=candidate.candidate_id,
                aggregate_version=1,
                event_type="KnowledgeCandidateSubmitted",
                payload={
                    "candidate_id": str(candidate.candidate_id),
                    "target_page_id": (
                        str(candidate.target_page_id)
                        if candidate.target_page_id is not None
                        else None
                    ),
                    "source_ids": [str(source_id) for source_id in candidate.source_refs],
                    "candidate_hash": candidate.candidate_hash,
                    "status": candidate.status,
                },
                occurred_at=datetime.now().astimezone(),
                correlation_id=command.correlation_id,
                causation_id=command.command_id,
            )
        )
        return {"candidate_id": str(candidate.candidate_id), "status": candidate.status}

    @staticmethod
    def _validate_operational_source(
        actor_type: str,
        source_type: str,
        provenance: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> None:
        if actor_type != "control_plane":
            return
        required: tuple[str, ...]
        if source_type == "task":
            required = ("task_id", "subtask_id")
        elif source_type == "artifact":
            required = ("artifact_id",)
        elif source_type == "evidence":
            required = ("evidence_id",)
        else:
            required = ()
        for key in required:
            if provenance.get(key) is None and metadata.get(key) is None:
                raise CommandFailure("source_invalid", {"field": key})


class ReviewKnowledgeCandidateHandler:
    def handle(self, command: CommandEnvelope, uow: Any) -> dict[str, Any]:
        if command.actor_type != "local_owner":
            raise CommandFailure("permission_denied")
        payload = _mapping(command.payload, "payload")
        candidate_id = _uuid(payload.get("candidate_id"), "candidate_id")
        assert candidate_id is not None
        decision = payload.get("decision")
        if decision not in {"accept", "reject", "request_changes"}:
            raise CommandFailure("candidate_invalid", {"field": "decision"})
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise CommandFailure("candidate_invalid", {"field": "reason"})
        observed_hash = payload.get("observed_candidate_hash")
        if not isinstance(observed_hash, str):
            raise CommandFailure("candidate_invalid", {"field": "observed_candidate_hash"})
        expected_version = payload.get("expected_candidate_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise CommandFailure("candidate_invalid", {"field": "expected_candidate_version"})

        candidate = uow.candidates.get_candidate_for_update(
            candidate_id, command.workspace_id
        )
        if candidate is None:
            raise CommandFailure("not_found", {"resource": "candidate"})
        if candidate.version != expected_version:
            raise CommandFailure("expected_version_conflict", {"resource": "candidate"})
        if not candidate.is_reviewable:
            raise CommandFailure("expected_version_conflict", {"resource": "candidate"})
        if observed_hash != candidate.candidate_hash:
            raise CommandFailure("hash_mismatch", {"resource": "candidate"})

        observed_target_revision_id = _uuid(
            payload.get("observed_target_revision_id"),
            "observed_target_revision_id",
            required=False,
        )
        observed_target_hash = payload.get("observed_target_revision_hash")
        if candidate.operation == "revise_page":
            if observed_target_revision_id != candidate.expected_revision_id:
                raise CommandFailure("candidate_invalid", {"field": "observed_target_revision_id"})
            if not isinstance(observed_target_hash, str):
                raise CommandFailure("candidate_invalid", {"field": "observed_target_revision_hash"})
            try:
                validate_sha256(observed_target_hash, "observed_target_revision_hash")
            except DomainValidationError as exc:
                raise CommandFailure(
                    "candidate_invalid", {"field": "observed_target_revision_hash"}
                ) from exc
            if observed_target_hash != candidate.expected_revision_hash:
                raise CommandFailure("hash_mismatch", {"resource": "target_revision"})

        if decision != "accept":
            next_status = "rejected" if decision == "reject" else "changes_requested"
            uow.candidates.transition_candidate(
                candidate_id, command.workspace_id, next_status, candidate.version
            )
            review = KnowledgeCandidateReview(
                review_id=uuid4(),
                candidate_id=candidate_id,
                reviewer_actor=f"{command.actor_type}:{command.actor_id}",
                decision=cast(Any, decision),
                reason=reason,
                observed_candidate_hash=observed_hash,
                observed_target_revision_id=observed_target_revision_id,
            )
            uow.candidates.add_review(review, command.workspace_id)
            self._append_review_event(
                uow,
                command,
                candidate,
                review,
                status=next_status,
            )
            return {"candidate_id": str(candidate_id), "status": next_status}

        page: Any
        if candidate.operation == "create_page":
            page = KnowledgePage(
                page_id=uuid4(),
                workspace_id=command.workspace_id,
                canonical_title=candidate.proposed_title,
                slug=candidate.proposed_slug,
            )
            uow.pages.add_page(page)
            uow.flush()
            revision_number = 1
        else:
            if candidate.target_page_id is None or candidate.expected_revision_id is None:
                raise CommandFailure("candidate_invalid", {"field": "target"})
            page = uow.pages.get_page_for_update(
                candidate.target_page_id, command.workspace_id
            )
            if page is None or page.current_revision_id != candidate.expected_revision_id:
                return self._mark_stale(
                    command, uow, candidate, reason, observed_hash, observed_target_revision_id
                )
            if page.status != "active":
                raise CommandFailure("expected_version_conflict", {"resource": "page"})
            current = uow.pages.get_revision(
                candidate.expected_revision_id, command.workspace_id
            )
            if current is None or current.content_hash != candidate.expected_revision_hash:
                return self._mark_stale(
                    command, uow, candidate, reason, observed_hash, observed_target_revision_id
                )
            revision_number = current.revision_number + 1

        revision = KnowledgeRevision(
            revision_id=uuid4(),
            page_id=page.page_id,
            workspace_id=command.workspace_id,
            revision_number=revision_number,
            title=candidate.proposed_title,
            summary=candidate.proposed_summary,
            tags=candidate.proposed_tags,
            sections=candidate.proposed_sections,
            relations=candidate.proposed_relations,
            source_refs=candidate.source_refs,
            change_reason=reason,
            created_by=f"{command.actor_type}:{command.actor_id}",
            created_at=datetime.now().astimezone(),
            accepted_candidate_id=candidate.candidate_id,
        )
        uow.pages.add_revision_and_advance_page(revision, page.version)
        uow.candidates.transition_candidate(
            candidate_id, command.workspace_id, "accepted", candidate.version
        )
        review = KnowledgeCandidateReview(
            review_id=uuid4(),
            candidate_id=candidate_id,
            reviewer_actor=f"{command.actor_type}:{command.actor_id}",
            decision="accept",
            reason=reason,
            observed_candidate_hash=observed_hash,
            observed_target_revision_id=observed_target_revision_id,
            created_revision_id=revision.revision_id,
        )
        uow.candidates.add_review(review, command.workspace_id)
        self._append_review_event(
            uow,
            command,
            candidate,
            review,
            status="accepted",
            revision=revision,
        )
        return {
            "candidate_id": str(candidate_id),
            "status": "accepted",
            "page_id": str(page.page_id),
            "revision_id": str(revision.revision_id),
        }

    def _mark_stale(
        self,
        command: CommandEnvelope,
        uow: Any,
        candidate: KnowledgeCandidate,
        reason: str,
        observed_hash: str,
        observed_target_revision_id: UUID | None,
    ) -> dict[str, Any]:
        uow.candidates.transition_candidate(
            candidate.candidate_id,
            command.workspace_id,
            "stale",
            candidate.version,
        )
        review = KnowledgeCandidateReview(
            review_id=uuid4(),
            candidate_id=candidate.candidate_id,
            reviewer_actor=f"{command.actor_type}:{command.actor_id}",
            # An accept request fenced against a changed target was not accepted;
            # the stale outcome is carried by the candidate and event.
            decision="reject",
            reason=reason,
            observed_candidate_hash=observed_hash,
            observed_target_revision_id=observed_target_revision_id,
        )
        uow.candidates.add_review(review, command.workspace_id)
        self._append_review_event(
            uow,
            command,
            candidate,
            review,
            status="stale",
        )
        return {"candidate_id": str(candidate.candidate_id), "status": "stale"}

    @staticmethod
    def _append_review_event(
        uow: Any,
        command: CommandEnvelope,
        candidate: KnowledgeCandidate,
        review: KnowledgeCandidateReview,
        *,
        status: str,
        revision: KnowledgeRevision | None = None,
    ) -> None:
        uow.events.append_with_outbox(
            EventEnvelope(
                event_id=uuid4(),
                workspace_id=command.workspace_id,
                aggregate_type="knowledge_candidate",
                aggregate_id=candidate.candidate_id,
                aggregate_version=candidate.version + 1,
                event_type="KnowledgeCandidateReviewed",
                payload={
                    "candidate_id": str(candidate.candidate_id),
                    "review_id": str(review.review_id),
                    "decision": review.decision,
                    "status": status,
                    "created_revision_id": (
                        str(review.created_revision_id)
                        if review.created_revision_id
                        else None
                    ),
                },
                occurred_at=datetime.now().astimezone(),
                correlation_id=command.correlation_id,
                causation_id=command.command_id,
            )
        )
        if revision is not None:
            uow.events.append_with_outbox(
                EventEnvelope(
                    event_id=uuid4(),
                    workspace_id=command.workspace_id,
                    aggregate_type="knowledge_revision",
                    aggregate_id=revision.revision_id,
                    aggregate_version=revision.revision_number,
                    event_type="KnowledgeRevisionPublished",
                    payload={
                        "revision_id": str(revision.revision_id),
                        "page_id": str(revision.page_id),
                        "candidate_id": str(candidate.candidate_id),
                        "revision_hash": revision.content_hash,
                    },
                    occurred_at=datetime.now().astimezone(),
                    correlation_id=command.correlation_id,
                    causation_id=command.command_id,
                )
            )


class WithdrawKnowledgeCandidateHandler:
    def handle(self, command: CommandEnvelope, uow: Any) -> dict[str, Any]:
        if command.actor_type != "local_owner":
            raise CommandFailure("permission_denied")
        payload = _mapping(command.payload, "payload")
        candidate_id = _uuid(payload.get("candidate_id"), "candidate_id")
        expected_version = payload.get("expected_candidate_version")
        if candidate_id is None or not isinstance(expected_version, int):
            raise CommandFailure("candidate_invalid", {"field": "candidate"})
        candidate = uow.candidates.get_candidate_for_update(
            candidate_id, command.workspace_id
        )
        if candidate is None:
            raise CommandFailure("not_found", {"resource": "candidate"})
        if candidate.version != expected_version or candidate.status not in {
            "pending",
            "changes_requested",
        }:
            raise CommandFailure("expected_version_conflict", {"resource": "candidate"})
        uow.candidates.transition_candidate(
            candidate_id, command.workspace_id, "withdrawn", candidate.version
        )
        uow.events.append_with_outbox(
            EventEnvelope(
                event_id=uuid4(),
                workspace_id=command.workspace_id,
                aggregate_type="knowledge_candidate",
                aggregate_id=candidate_id,
                aggregate_version=candidate.version + 1,
                event_type="KnowledgeCandidateWithdrawn",
                payload={"candidate_id": str(candidate_id), "status": "withdrawn"},
                occurred_at=datetime.now().astimezone(),
                correlation_id=command.correlation_id,
                causation_id=command.command_id,
            )
        )
        return {"candidate_id": str(candidate_id), "status": "withdrawn"}


class ArchiveKnowledgePageHandler:
    def handle(self, command: CommandEnvelope, uow: Any) -> dict[str, Any]:
        if command.actor_type != "local_owner":
            raise CommandFailure("permission_denied")
        payload = _mapping(command.payload, "payload")
        page_id = _uuid(payload.get("page_id"), "page_id")
        expected_version = payload.get("expected_page_version")
        if page_id is None or not isinstance(expected_version, int):
            raise CommandFailure("candidate_invalid", {"field": "page"})
        try:
            page = uow.pages.archive_page(page_id, command.workspace_id, expected_version)
        except PersistenceNotFound as exc:
            raise CommandFailure("not_found", {"resource": "page"}) from exc
        except PersistenceConflict as exc:
            raise CommandFailure(
                "expected_version_conflict", {"resource": "page"}
            ) from exc
        uow.events.append_with_outbox(
            EventEnvelope(
                event_id=uuid4(),
                workspace_id=command.workspace_id,
                aggregate_type="knowledge_page",
                aggregate_id=page.page_id,
                aggregate_version=page.version,
                event_type="KnowledgePageArchived",
                payload={"page_id": str(page.page_id), "status": page.status},
                occurred_at=datetime.now().astimezone(),
                correlation_id=command.correlation_id,
                causation_id=command.command_id,
            )
        )
        return {"page_id": str(page.page_id), "status": page.status}


def record_source_unavailable(
    uow: Any,
    *,
    workspace_id: UUID,
    source_id: UUID,
    expected_version: int,
    reason_code: str,
    checked_at: datetime,
    correlation_id: UUID,
) -> dict[str, Any]:
    reason_code = reason_code.strip()
    if not reason_code:
        raise CommandFailure("source_invalid", {"field": "reason_code"})
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise CommandFailure("source_invalid", {"field": "checked_at"})
    before = uow.sources.get_source(source_id, workspace_id)
    if before is None:
        raise CommandFailure("not_found", {"resource": "source"})
    if before.availability == "unavailable" and before.safe_reason_code == reason_code:
        return {"source_id": str(source_id), "status": "unavailable", "idempotent": True}
    try:
        updated = uow.sources.mark_unavailable(
            source_id,
            workspace_id,
            expected_version,
            reason_code,
            checked_at,
        )
    except PersistenceNotFound as exc:
        raise CommandFailure("not_found", {"resource": "source"}) from exc
    except PersistenceConflict as exc:
        raise CommandFailure(
            "expected_version_conflict", {"resource": "source"}
        ) from exc
    uow.events.append_with_outbox(
        EventEnvelope(
            event_id=uuid4(),
            workspace_id=workspace_id,
            aggregate_type="knowledge_source",
            aggregate_id=source_id,
            aggregate_version=updated.version,
            event_type="KnowledgeSourceUnavailable",
            payload={
                "source_id": str(source_id),
                "status": updated.availability,
                "reason_code": reason_code,
            },
            occurred_at=checked_at,
            correlation_id=correlation_id,
        )
    )
    return {"source_id": str(source_id), "status": updated.availability}


def knowledge_command_handlers() -> dict[str, Any]:
    return {
        "SubmitKnowledgeCandidate": SubmitKnowledgeCandidateHandler(),
        "ReviewKnowledgeCandidate": ReviewKnowledgeCandidateHandler(),
        "WithdrawKnowledgeCandidate": WithdrawKnowledgeCandidateHandler(),
        "ArchiveKnowledgePage": ArchiveKnowledgePageHandler(),
    }
