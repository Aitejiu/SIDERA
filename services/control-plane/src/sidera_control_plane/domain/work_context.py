from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

from sidera_control_plane.domain.errors import (
    InvalidTransition,
    ValidationError,
    VersionConflict,
)


class WorkContextStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    LEGACY_INCOMPLETE = "legacy_incomplete"


class WorkContextRevisionReason(StrEnum):
    REQUEST_CREATED = "request_created"
    CLARIFICATION_ACCEPTED = "clarification_accepted"
    PROPOSAL_CONFIRMED = "proposal_confirmed"
    USER_INTERVENTION = "user_intervention"
    REPLANNED = "replanned"
    KNOWLEDGE_ATTACHED = "knowledge_attached"
    LEGACY_BACKFILL = "legacy_backfill"


_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_MAPPING_KEY_PARTS = ("conversation", "trace", "body", "markdown", "snapshot")


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} is required")
    return value.strip()


def _positive(value: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{field_name} must be positive")
    return value


def _ordinal(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError("ordinal must be non-negative")
    return value


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must be timezone aware")
    return value


def _hash(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{field_name} must be a lowercase SHA-256 hash")
    return value


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field_name} must be a mapping")
    for key in value:
        if not isinstance(key, str):
            raise ValidationError(f"{field_name} keys must be strings")
        lowered = key.lower()
        if any(part in lowered for part in _FORBIDDEN_MAPPING_KEY_PARTS):
            raise ValidationError(f"{field_name} contains forbidden field {key}")
    return value


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("work context snapshot cannot be canonicalized") from exc


def _sorted_items[T](items: Sequence[T], *, preserve_ordinal: bool) -> tuple[T, ...]:
    if preserve_ordinal:
        return tuple(
            sorted(items, key=lambda item: (_item_ordinal(item), _item_sort_key(item)))
        )
    return tuple(sorted(items, key=_item_sort_key))


def _item_ordinal(item: object) -> int:
    ordinal = getattr(item, "ordinal", None)
    if not isinstance(ordinal, int):
        raise ValidationError("typed context item must have an ordinal")
    return ordinal


def _item_sort_key(item: object) -> tuple[str, ...]:
    return (
        str(getattr(item, "key", "")),
        str(getattr(item, "section_key", "")),
        str(getattr(item, "ref_type", "")),
        str(getattr(item, "page_id", "")),
        str(getattr(item, "revision_id", "")),
        str(getattr(item, "ref_id", "")),
    )


@dataclass(frozen=True, slots=True)
class ContextTextItem:
    key: str
    text: str
    ordinal: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _required_text(self.key, "item key"))
        object.__setattr__(self, "text", _required_text(self.text, "item text"))
        _ordinal(self.ordinal)

    def to_mapping(self) -> dict[str, object]:
        return {"key": self.key, "text": self.text, "ordinal": self.ordinal}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ContextTextItem:
        _ensure_fields(value, {"key", "text", "ordinal"}, "context text item")
        return cls(
            key=_string(value, "key"),
            text=_string(value, "text"),
            ordinal=_integer(value, "ordinal"),
        )


@dataclass(frozen=True, slots=True)
class ClarificationDecision:
    key: str
    question: str
    answer: str
    answered_by: str
    ordinal: int

    def __post_init__(self) -> None:
        for name in ("key", "question", "answer", "answered_by"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        _ordinal(self.ordinal)

    def to_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "question": self.question,
            "answer": self.answer,
            "answered_by": self.answered_by,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ClarificationDecision:
        _ensure_fields(
            value,
            {"key", "question", "answer", "answered_by", "ordinal"},
            "clarification decision",
        )
        return cls(
            key=_string(value, "key"),
            question=_string(value, "question"),
            answer=_string(value, "answer"),
            answered_by=_string(value, "answered_by"),
            ordinal=_integer(value, "ordinal"),
        )


@dataclass(frozen=True, slots=True)
class AcceptedDecision:
    key: str
    statement: str
    source: str
    ordinal: int

    def __post_init__(self) -> None:
        for name in ("key", "statement", "source"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        _ordinal(self.ordinal)

    def to_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "statement": self.statement,
            "source": self.source,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> AcceptedDecision:
        _ensure_fields(
            value, {"key", "statement", "source", "ordinal"}, "accepted decision"
        )
        return cls(
            key=_string(value, "key"),
            statement=_string(value, "statement"),
            source=_string(value, "source"),
            ordinal=_integer(value, "ordinal"),
        )


@dataclass(frozen=True, slots=True)
class UnresolvedQuestion:
    key: str
    question: str
    blocking: bool
    ordinal: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _required_text(self.key, "question key"))
        object.__setattr__(self, "question", _required_text(self.question, "question"))
        if not isinstance(self.blocking, bool):
            raise ValidationError("question blocking must be boolean")
        _ordinal(self.ordinal)

    def to_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "question": self.question,
            "blocking": self.blocking,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> UnresolvedQuestion:
        _ensure_fields(
            value, {"key", "question", "blocking", "ordinal"}, "unresolved question"
        )
        return cls(
            key=_string(value, "key"),
            question=_string(value, "question"),
            blocking=_boolean(value, "blocking"),
            ordinal=_integer(value, "ordinal"),
        )


@dataclass(frozen=True, slots=True)
class KnowledgeRevisionRef:
    page_id: UUID
    revision_id: UUID
    revision_hash: str
    section_key: str
    section_hash: str
    source_ids: tuple[UUID, ...]
    ordinal: int

    def __post_init__(self) -> None:
        if not isinstance(self.page_id, UUID) or not isinstance(self.revision_id, UUID):
            raise ValidationError("knowledge page/revision IDs must be UUIDs")
        object.__setattr__(self, "revision_hash", _hash(self.revision_hash, "revision_hash"))
        object.__setattr__(self, "section_hash", _hash(self.section_hash, "section_hash"))
        object.__setattr__(self, "section_key", _required_text(self.section_key, "section_key"))
        if any(not isinstance(item, UUID) for item in self.source_ids):
            raise ValidationError("knowledge source IDs must be UUIDs")
        object.__setattr__(self, "source_ids", tuple(sorted(set(self.source_ids), key=str)))
        _ordinal(self.ordinal)

    def to_mapping(self) -> dict[str, object]:
        return {
            "page_id": str(self.page_id),
            "revision_id": str(self.revision_id),
            "revision_hash": self.revision_hash,
            "section_key": self.section_key,
            "section_hash": self.section_hash,
            "source_ids": [str(item) for item in self.source_ids],
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> KnowledgeRevisionRef:
        _ensure_fields(
            value,
            {
                "page_id",
                "revision_id",
                "revision_hash",
                "section_key",
                "section_hash",
                "source_ids",
                "ordinal",
            },
            "knowledge revision ref",
        )
        raw_sources = value["source_ids"]
        if not isinstance(raw_sources, Sequence) or isinstance(raw_sources, (str, bytes)):
            raise ValidationError("knowledge source_ids must be a list")
        return cls(
            page_id=_uuid(value, "page_id"),
            revision_id=_uuid(value, "revision_id"),
            revision_hash=_string(value, "revision_hash"),
            section_key=_string(value, "section_key"),
            section_hash=_string(value, "section_hash"),
            source_ids=tuple(UUID(str(item)) for item in raw_sources),
            ordinal=_integer(value, "ordinal"),
        )


@dataclass(frozen=True, slots=True)
class WorkReference:
    ref_id: UUID
    ref_type: str
    ordinal: int

    def __post_init__(self) -> None:
        if not isinstance(self.ref_id, UUID):
            raise ValidationError("reference ID must be a UUID")
        object.__setattr__(self, "ref_type", _required_text(self.ref_type, "ref_type"))
        _ordinal(self.ordinal)

    def to_mapping(self) -> dict[str, object]:
        return {"ref_id": str(self.ref_id), "ref_type": self.ref_type, "ordinal": self.ordinal}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> WorkReference:
        _ensure_fields(value, {"ref_id", "ref_type", "ordinal"}, "work reference")
        return cls(
            ref_id=_uuid(value, "ref_id"),
            ref_type=_string(value, "ref_type"),
            ordinal=_integer(value, "ordinal"),
        )


@dataclass(frozen=True, slots=True)
class WorkContextSnapshotV1:
    objective: str
    constraints: tuple[ContextTextItem, ...]
    acceptance_criteria: tuple[ContextTextItem, ...]
    requested_outputs: tuple[ContextTextItem, ...]
    clarification_decisions: tuple[ClarificationDecision, ...]
    accepted_decisions: tuple[AcceptedDecision, ...]
    unresolved_questions: tuple[UnresolvedQuestion, ...]
    knowledge_refs: tuple[KnowledgeRevisionRef, ...]
    artifact_refs: tuple[WorkReference, ...]
    evidence_refs: tuple[WorkReference, ...]
    checkpoint_refs: tuple[WorkReference, ...]
    missing_fact_codes: tuple[str, ...] = ()
    schema_version: int = 1

    @classmethod
    def create(
        cls,
        *,
        objective: str,
        constraints: Sequence[ContextTextItem],
        acceptance_criteria: Sequence[ContextTextItem],
        requested_outputs: Sequence[ContextTextItem],
        clarification_decisions: Sequence[ClarificationDecision],
        accepted_decisions: Sequence[AcceptedDecision],
        unresolved_questions: Sequence[UnresolvedQuestion],
        knowledge_refs: Sequence[KnowledgeRevisionRef],
        artifact_refs: Sequence[WorkReference],
        evidence_refs: Sequence[WorkReference],
        checkpoint_refs: Sequence[WorkReference],
        missing_fact_codes: Sequence[str] = (),
    ) -> WorkContextSnapshotV1:
        return cls(
            objective=objective,
            constraints=tuple(constraints),
            acceptance_criteria=tuple(acceptance_criteria),
            requested_outputs=tuple(requested_outputs),
            clarification_decisions=tuple(clarification_decisions),
            accepted_decisions=tuple(accepted_decisions),
            unresolved_questions=tuple(unresolved_questions),
            knowledge_refs=tuple(knowledge_refs),
            artifact_refs=tuple(artifact_refs),
            evidence_refs=tuple(evidence_refs),
            checkpoint_refs=tuple(checkpoint_refs),
            missing_fact_codes=tuple(missing_fact_codes),
        )

    def __post_init__(self) -> None:
        _positive(self.schema_version, "snapshot schema_version")
        object.__setattr__(self, "objective", _required_text(self.objective, "objective"))
        object.__setattr__(self, "constraints", _typed_tuple(self.constraints, ContextTextItem, "constraints"))
        object.__setattr__(
            self,
            "acceptance_criteria",
            _typed_tuple(self.acceptance_criteria, ContextTextItem, "acceptance_criteria"),
        )
        object.__setattr__(
            self,
            "requested_outputs",
            _typed_tuple(self.requested_outputs, ContextTextItem, "requested_outputs"),
        )
        object.__setattr__(
            self,
            "clarification_decisions",
            _typed_tuple(self.clarification_decisions, ClarificationDecision, "clarification_decisions"),
        )
        object.__setattr__(
            self,
            "accepted_decisions",
            _typed_tuple(self.accepted_decisions, AcceptedDecision, "accepted_decisions"),
        )
        object.__setattr__(
            self,
            "unresolved_questions",
            _typed_tuple(self.unresolved_questions, UnresolvedQuestion, "unresolved_questions"),
        )
        object.__setattr__(
            self,
            "knowledge_refs",
            _typed_tuple(self.knowledge_refs, KnowledgeRevisionRef, "knowledge_refs"),
        )
        for field_name in ("artifact_refs", "evidence_refs", "checkpoint_refs"):
            object.__setattr__(
                self,
                field_name,
                _typed_tuple(getattr(self, field_name), WorkReference, field_name),
            )
        object.__setattr__(
            self,
            "missing_fact_codes",
            tuple(sorted({_required_text(item, "missing fact code") for item in self.missing_fact_codes})),
        )
        object.__setattr__(self, "constraints", _sorted_items(self.constraints, preserve_ordinal=False))
        for field_name in (
            "acceptance_criteria",
            "requested_outputs",
            "clarification_decisions",
            "accepted_decisions",
            "unresolved_questions",
            "knowledge_refs",
            "artifact_refs",
            "evidence_refs",
            "checkpoint_refs",
        ):
            object.__setattr__(
                self,
                field_name,
                _sorted_items(getattr(self, field_name), preserve_ordinal=True),
            )

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "objective": self.objective,
            "constraints": [item.to_mapping() for item in self.constraints],
            "acceptance_criteria": [item.to_mapping() for item in self.acceptance_criteria],
            "requested_outputs": [item.to_mapping() for item in self.requested_outputs],
            "clarification_decisions": [item.to_mapping() for item in self.clarification_decisions],
            "accepted_decisions": [item.to_mapping() for item in self.accepted_decisions],
            "unresolved_questions": [item.to_mapping() for item in self.unresolved_questions],
            "knowledge_refs": [item.to_mapping() for item in self.knowledge_refs],
            "artifact_refs": [item.to_mapping() for item in self.artifact_refs],
            "evidence_refs": [item.to_mapping() for item in self.evidence_refs],
            "checkpoint_refs": [item.to_mapping() for item in self.checkpoint_refs],
        }
        if self.missing_fact_codes:
            result["missing_fact_codes"] = list(self.missing_fact_codes)
        return result

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_mapping())

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> WorkContextSnapshotV1:
        allowed = {
            "schema_version",
            "objective",
            "constraints",
            "acceptance_criteria",
            "requested_outputs",
            "clarification_decisions",
            "accepted_decisions",
            "unresolved_questions",
            "knowledge_refs",
            "artifact_refs",
            "evidence_refs",
            "checkpoint_refs",
            "missing_fact_codes",
        }
        _ensure_fields(value, allowed, "work context snapshot")
        return cls(
            schema_version=_integer(value, "schema_version"),
            objective=_string(value, "objective"),
            constraints=_parse_items(value, "constraints", ContextTextItem),
            acceptance_criteria=_parse_items(value, "acceptance_criteria", ContextTextItem),
            requested_outputs=_parse_items(value, "requested_outputs", ContextTextItem),
            clarification_decisions=_parse_items(value, "clarification_decisions", ClarificationDecision),
            accepted_decisions=_parse_items(value, "accepted_decisions", AcceptedDecision),
            unresolved_questions=_parse_items(value, "unresolved_questions", UnresolvedQuestion),
            knowledge_refs=_parse_items(value, "knowledge_refs", KnowledgeRevisionRef),
            artifact_refs=_parse_items(value, "artifact_refs", WorkReference),
            evidence_refs=_parse_items(value, "evidence_refs", WorkReference),
            checkpoint_refs=_parse_items(value, "checkpoint_refs", WorkReference),
            missing_fact_codes=_parse_texts(value, "missing_fact_codes"),
        )


@dataclass(frozen=True, slots=True)
class WorkContextRevision:
    revision_id: UUID
    work_context_id: UUID
    workspace_id: UUID
    revision_number: int
    brief_planning_request_id: UUID
    brief_revision: int
    proposal_revision_id: UUID | None
    plan_snapshot_id: UUID | None
    reason: WorkContextRevisionReason
    snapshot: WorkContextSnapshotV1
    content_hash: str
    created_by: str
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        revision_id: UUID,
        work_context_id: UUID,
        workspace_id: UUID,
        revision_number: int,
        brief_planning_request_id: UUID,
        brief_revision: int,
        proposal_revision_id: UUID | None,
        plan_snapshot_id: UUID | None,
        reason: WorkContextRevisionReason,
        snapshot: WorkContextSnapshotV1,
        created_by: str,
        created_at: datetime,
    ) -> WorkContextRevision:
        return cls(
            revision_id=revision_id,
            work_context_id=work_context_id,
            workspace_id=workspace_id,
            revision_number=revision_number,
            brief_planning_request_id=brief_planning_request_id,
            brief_revision=brief_revision,
            proposal_revision_id=proposal_revision_id,
            plan_snapshot_id=plan_snapshot_id,
            reason=reason,
            snapshot=snapshot,
            content_hash=snapshot.content_hash,
            created_by=created_by,
            created_at=created_at,
        )

    def __post_init__(self) -> None:
        for name in ("revision_id", "work_context_id", "workspace_id", "brief_planning_request_id"):
            if not isinstance(getattr(self, name), UUID):
                raise ValidationError(f"work context revision {name} must be a UUID")
        if self.proposal_revision_id is not None and not isinstance(self.proposal_revision_id, UUID):
            raise ValidationError("proposal_revision_id must be a UUID")
        if self.plan_snapshot_id is not None and not isinstance(self.plan_snapshot_id, UUID):
            raise ValidationError("plan_snapshot_id must be a UUID")
        _positive(self.revision_number, "work context revision number")
        _positive(self.brief_revision, "brief revision")
        try:
            object.__setattr__(self, "reason", WorkContextRevisionReason(self.reason))
        except (TypeError, ValueError) as exc:
            raise ValidationError("unsupported work context revision reason") from exc
        if not isinstance(self.snapshot, WorkContextSnapshotV1):
            raise ValidationError("work context revision snapshot must be typed")
        if self.content_hash != self.snapshot.content_hash:
            raise ValidationError("work context revision content hash mismatch")
        object.__setattr__(self, "created_by", _required_text(self.created_by, "created_by"))
        _aware(self.created_at, "work context revision created_at")


@dataclass(slots=True)
class WorkContext:
    id: UUID
    workspace_id: UUID
    planning_request_id: UUID
    root_task_id: UUID | None
    status: WorkContextStatus
    current_revision_id: UUID
    current_revision_number: int
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        work_context_id: UUID,
        workspace_id: UUID,
        planning_request_id: UUID,
        first_revision: WorkContextRevision,
        created_at: datetime,
    ) -> WorkContext:
        _validate_first_revision(
            first_revision,
            work_context_id=work_context_id,
            workspace_id=workspace_id,
            planning_request_id=planning_request_id,
            reason=WorkContextRevisionReason.REQUEST_CREATED,
        )
        timestamp = _aware(created_at, "work context created_at")
        return cls(
            id=work_context_id,
            workspace_id=workspace_id,
            planning_request_id=planning_request_id,
            root_task_id=None,
            status=WorkContextStatus.ACTIVE,
            current_revision_id=first_revision.revision_id,
            current_revision_number=1,
            version=1,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @classmethod
    def create_legacy(
        cls,
        *,
        work_context_id: UUID,
        workspace_id: UUID,
        planning_request_id: UUID,
        first_revision: WorkContextRevision,
        created_at: datetime,
    ) -> WorkContext:
        _validate_first_revision(
            first_revision,
            work_context_id=work_context_id,
            workspace_id=workspace_id,
            planning_request_id=planning_request_id,
            reason=WorkContextRevisionReason.LEGACY_BACKFILL,
        )
        timestamp = _aware(created_at, "work context created_at")
        return cls(
            id=work_context_id,
            workspace_id=workspace_id,
            planning_request_id=planning_request_id,
            root_task_id=None,
            status=WorkContextStatus.LEGACY_INCOMPLETE,
            current_revision_id=first_revision.revision_id,
            current_revision_number=1,
            version=1,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @classmethod
    def create_backfilled(
        cls,
        *,
        work_context_id: UUID,
        workspace_id: UUID,
        planning_request_id: UUID,
        root_task_id: UUID | None,
        first_revision: WorkContextRevision,
        status: WorkContextStatus,
        created_at: datetime,
    ) -> WorkContext:
        _validate_first_revision(
            first_revision,
            work_context_id=work_context_id,
            workspace_id=workspace_id,
            planning_request_id=planning_request_id,
            reason=WorkContextRevisionReason.LEGACY_BACKFILL,
        )
        if status not in {
            WorkContextStatus.ACTIVE,
            WorkContextStatus.ARCHIVED,
            WorkContextStatus.LEGACY_INCOMPLETE,
        }:
            raise ValidationError("unsupported backfilled work context status")
        timestamp = _aware(created_at, "work context created_at")
        return cls(
            id=work_context_id,
            workspace_id=workspace_id,
            planning_request_id=planning_request_id,
            root_task_id=root_task_id,
            status=status,
            current_revision_id=first_revision.revision_id,
            current_revision_number=1,
            version=1,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @property
    def can_create_consumer(self) -> bool:
        return self.status is WorkContextStatus.ACTIVE

    def append_revision(
        self, revision: WorkContextRevision, *, expected_version: int, now: datetime
    ) -> None:
        if self.status is not WorkContextStatus.ACTIVE:
            raise InvalidTransition("only an active work context can be revised")
        self._append_revision(revision, expected_version=expected_version, now=now)

    def append_external_revision(
        self, revision: WorkContextRevision, *, expected_version: int, now: datetime
    ) -> None:
        """Append a replan/confirmation revision sourced by another request.

        A root Task keeps one WorkContext lineage, while each replan owns a
        separate PlanningRequest and Brief. The revision therefore points at
        the newer brief without changing the WorkContext's original owner.
        """
        if self.status is not WorkContextStatus.ACTIVE:
            raise InvalidTransition("only an active work context can be revised")
        if revision.reason not in {
            WorkContextRevisionReason.REPLANNED,
            WorkContextRevisionReason.PROPOSAL_CONFIRMED,
        }:
            raise ValidationError(
                "external work context revisions must be replanned or proposal_confirmed"
            )
        self._append_revision(
            revision,
            expected_version=expected_version,
            now=now,
            allow_external_brief=True,
        )

    def complete_legacy(
        self, revision: WorkContextRevision, *, expected_version: int, now: datetime
    ) -> None:
        if self.status is not WorkContextStatus.LEGACY_INCOMPLETE:
            raise InvalidTransition("only a legacy-incomplete context can be completed")
        if revision.reason is not WorkContextRevisionReason.LEGACY_BACKFILL:
            raise ValidationError("legacy completion requires legacy_backfill reason")
        self._append_revision(revision, expected_version=expected_version, now=now)
        self.status = WorkContextStatus.ACTIVE

    def bind_root_task(
        self, root_task_id: UUID, *, expected_version: int, now: datetime
    ) -> None:
        if self.status is not WorkContextStatus.ACTIVE:
            raise InvalidTransition("archived work context cannot bind a root task")
        if not isinstance(root_task_id, UUID):
            raise ValidationError("root_task_id must be a UUID")
        self._check_version(expected_version)
        if self.root_task_id is not None and self.root_task_id != root_task_id:
            raise ValidationError("work context is already bound to another root task")
        self.root_task_id = root_task_id
        self.version += 1
        self.updated_at = _aware(now, "work context updated_at")

    def bind_root_task_with_revision(
        self,
        root_task_id: UUID,
        revision: WorkContextRevision,
        *,
        expected_version: int,
        now: datetime,
    ) -> None:
        if self.status is not WorkContextStatus.ACTIVE:
            raise InvalidTransition(
                "only an active work context can bind a revised root task"
            )
        if not isinstance(root_task_id, UUID):
            raise ValidationError("root_task_id must be a UUID")
        self._check_version(expected_version)
        self._validate_revision_for_append(revision)
        if self.root_task_id is not None and self.root_task_id != root_task_id:
            raise ValidationError("work context is already bound to another root task")
        self.root_task_id = root_task_id
        self.current_revision_id = revision.revision_id
        self.current_revision_number = revision.revision_number
        self.version += 1
        self.updated_at = _aware(now, "work context updated_at")

    def archive(self, *, expected_version: int, now: datetime) -> None:
        if self.status is WorkContextStatus.ARCHIVED:
            return
        if self.status is not WorkContextStatus.ACTIVE:
            raise InvalidTransition("legacy-incomplete work context cannot be archived")
        self._check_version(expected_version)
        self.status = WorkContextStatus.ARCHIVED
        self.version += 1
        self.updated_at = _aware(now, "work context updated_at")

    def _append_revision(
        self,
        revision: WorkContextRevision,
        *,
        expected_version: int,
        now: datetime,
        allow_external_brief: bool = False,
    ) -> None:
        self._check_version(expected_version)
        self._validate_revision_for_append(
            revision, allow_external_brief=allow_external_brief
        )
        self.current_revision_id = revision.revision_id
        self.current_revision_number = revision.revision_number
        self.version += 1
        self.updated_at = _aware(now, "work context updated_at")

    def _validate_revision_for_append(
        self, revision: WorkContextRevision, *, allow_external_brief: bool = False
    ) -> None:
        if revision.work_context_id != self.id or revision.workspace_id != self.workspace_id:
            raise ValidationError("work context revision belongs to another context")
        if (
            not allow_external_brief
            and revision.brief_planning_request_id != self.planning_request_id
        ):
            raise ValidationError("brief planning request does not match work context")
        if revision.revision_number != self.current_revision_number + 1:
            raise ValidationError("work context revision number must advance by one")

    def _check_version(self, expected_version: int) -> None:
        if expected_version != self.version:
            raise VersionConflict(expected_version, self.version)


def _validate_first_revision(
    revision: WorkContextRevision,
    *,
    work_context_id: UUID,
    workspace_id: UUID,
    planning_request_id: UUID,
    reason: WorkContextRevisionReason,
) -> None:
    if revision.work_context_id != work_context_id or revision.workspace_id != workspace_id:
        raise ValidationError("first work context revision belongs to another context")
    if revision.brief_planning_request_id != planning_request_id:
        raise ValidationError("brief planning request does not match work context")
    if revision.revision_number != 1:
        raise ValidationError("first work context revision must be revision one")
    if revision.reason is not reason:
        raise ValidationError(f"first work context revision must use {reason.value}")


def _typed_tuple[T](
    values: Sequence[object], expected: type[T], field_name: str
) -> tuple[T, ...]:
    if any(not isinstance(item, expected) for item in values):
        raise ValidationError(f"{field_name} contains an invalid typed item")
    return tuple(cast(T, item) for item in values)


def _ensure_fields(value: Mapping[str, object], allowed: set[str], field_name: str) -> None:
    _mapping(value, field_name)
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ValidationError(f"unknown {field_name} fields: {', '.join(unknown)}")


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValidationError(f"{key} must be text")
    return item


def _integer(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise ValidationError(f"{key} must be an integer")
    return item


def _boolean(value: Mapping[str, object], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise ValidationError(f"{key} must be boolean")
    return item


def _uuid(value: Mapping[str, object], key: str) -> UUID:
    try:
        return UUID(str(value[key]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(f"{key} must be a UUID") from exc


def _parse_items[T](
    value: Mapping[str, object], key: str, expected: type[T]
) -> tuple[T, ...]:
    raw = value.get(key, [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValidationError(f"{key} must be a list")
    parser = getattr(expected, "from_mapping", None)
    if parser is None:
        raise ValidationError(f"{key} has no mapping parser")
    return tuple(cast(T, parser(_mapping(item, key))) for item in raw)


def _parse_texts(value: Mapping[str, object], key: str) -> tuple[str, ...]:
    raw = value.get(key, [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValidationError(f"{key} must be a list")
    return tuple(_required_text(item, "missing fact code") for item in raw)
