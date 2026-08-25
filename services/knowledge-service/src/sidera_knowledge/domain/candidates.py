from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sidera_knowledge.domain.common import (
    DomainValidationError,
    canonical_json_bytes,
    normalize_tags,
    require_text,
    sha256_hex,
    validate_sha256,
)
from sidera_knowledge.domain.pages import (
    KnowledgeRelationSnapshot,
    KnowledgeSectionSnapshot,
)

CandidateStatus = Literal[
    "pending",
    "accepted",
    "rejected",
    "changes_requested",
    "stale",
    "withdrawn",
]
CandidateOperation = Literal["create_page", "revise_page"]
ReviewDecision = Literal["accept", "reject", "request_changes"]


class CandidateStateError(DomainValidationError):
    """Raised when a candidate cannot be reviewed or is internally inconsistent."""


@dataclass(frozen=True)
class KnowledgeCandidate:
    candidate_id: UUID
    workspace_id: UUID
    operation: CandidateOperation
    target_page_id: UUID | None
    expected_revision_id: UUID | None
    expected_revision_hash: str | None
    proposed_title: str
    proposed_slug: str
    proposed_summary: str
    proposed_tags: tuple[str, ...]
    proposed_sections: tuple[KnowledgeSectionSnapshot, ...]
    proposed_relations: tuple[KnowledgeRelationSnapshot, ...] = ()
    source_refs: tuple[UUID, ...] = ()
    producer_type: str = "unknown"
    producer_id: str = "unknown"
    task_id: UUID | None = None
    subtask_id: UUID | None = None
    worker_item_id: UUID | None = None
    execution_id: UUID | None = None
    supersedes_candidate_id: UUID | None = None
    status: CandidateStatus = "pending"
    version: int = 1

    def __post_init__(self) -> None:
        if self.operation == "create_page":
            if self.target_page_id is not None:
                raise CandidateStateError("create_page cannot have target_page_id")
            if self.expected_revision_id is not None or self.expected_revision_hash is not None:
                raise CandidateStateError("create_page cannot have expected revision")
        elif self.operation == "revise_page":
            if self.target_page_id is None:
                raise CandidateStateError("revise_page requires target_page_id")
            if self.expected_revision_id is None:
                raise CandidateStateError("revise_page requires expected_revision_id")
            if self.expected_revision_hash is None:
                raise CandidateStateError("revise_page requires expected_revision_hash")
            object.__setattr__(
                self,
                "expected_revision_hash",
                validate_sha256(self.expected_revision_hash, "expected_revision_hash"),
            )
        else:
            raise CandidateStateError("unsupported candidate operation")
        object.__setattr__(self, "proposed_title", require_text(self.proposed_title, "proposed_title"))
        object.__setattr__(self, "proposed_slug", require_text(self.proposed_slug, "proposed_slug"))
        if any(char.isspace() for char in self.proposed_slug) or "/" in self.proposed_slug:
            raise CandidateStateError("proposed_slug must be a path segment")
        object.__setattr__(self, "proposed_summary", self.proposed_summary.strip())
        object.__setattr__(self, "proposed_tags", normalize_tags(self.proposed_tags))
        sections = tuple(self.proposed_sections)
        if not sections:
            raise CandidateStateError("candidate requires a complete section snapshot")
        if len({section.section_key for section in sections}) != len(sections):
            raise CandidateStateError("candidate section_key must be unique")
        object.__setattr__(self, "proposed_sections", sections)
        relations = tuple(self.proposed_relations)
        if len({relation.relation_key for relation in relations}) != len(relations):
            raise CandidateStateError("candidate relation_key must be unique")
        object.__setattr__(self, "proposed_relations", relations)
        source_refs = tuple(self.source_refs)
        if len(set(source_refs)) != len(source_refs):
            raise CandidateStateError("candidate source refs must be unique")
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "producer_type", require_text(self.producer_type, "producer_type"))
        object.__setattr__(self, "producer_id", require_text(self.producer_id, "producer_id"))
        if self.status not in {
            "pending",
            "accepted",
            "rejected",
            "changes_requested",
            "stale",
            "withdrawn",
        }:
            raise CandidateStateError("unsupported candidate status")
        if self.version < 1:
            raise CandidateStateError("candidate version must be positive")
        if self.supersedes_candidate_id == self.candidate_id:
            raise CandidateStateError("candidate cannot supersede itself")

    @property
    def proposed_snapshot(self) -> dict[str, Any]:
        return {
            "title": self.proposed_title,
            "summary": self.proposed_summary,
            "tags": list(self.proposed_tags),
            "sections": [section.snapshot for section in self.proposed_sections],
            "relations": [relation.snapshot for relation in self.proposed_relations],
            "source_refs": [str(source_id) for source_id in self.source_refs],
        }

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "target_page_id": str(self.target_page_id) if self.target_page_id else None,
            "expected_revision_id": (
                str(self.expected_revision_id) if self.expected_revision_id else None
            ),
            "expected_revision_hash": self.expected_revision_hash,
            "producer_type": self.producer_type,
            "producer_id": self.producer_id,
            "task_id": str(self.task_id) if self.task_id else None,
            "subtask_id": str(self.subtask_id) if self.subtask_id else None,
            "worker_item_id": str(self.worker_item_id) if self.worker_item_id else None,
            "execution_id": str(self.execution_id) if self.execution_id else None,
        }

    @property
    def candidate_hash(self) -> str:
        return sha256_hex(
            canonical_json_bytes(
                {"provenance": self.provenance, "snapshot": self.proposed_snapshot}
            )
        )

    @property
    def is_reviewable(self) -> bool:
        return self.status == "pending"

    def transition(self, status: CandidateStatus) -> KnowledgeCandidate:
        if not self.is_reviewable:
            raise CandidateStateError("terminal or already requested candidate cannot be reviewed")
        if status == "pending":
            raise CandidateStateError("candidate transition must be terminal")
        return KnowledgeCandidate(
            candidate_id=self.candidate_id,
            workspace_id=self.workspace_id,
            operation=self.operation,
            target_page_id=self.target_page_id,
            expected_revision_id=self.expected_revision_id,
            expected_revision_hash=self.expected_revision_hash,
            proposed_title=self.proposed_title,
            proposed_slug=self.proposed_slug,
            proposed_summary=self.proposed_summary,
            proposed_tags=self.proposed_tags,
            proposed_sections=self.proposed_sections,
            proposed_relations=self.proposed_relations,
            source_refs=self.source_refs,
            producer_type=self.producer_type,
            producer_id=self.producer_id,
            task_id=self.task_id,
            subtask_id=self.subtask_id,
            worker_item_id=self.worker_item_id,
            execution_id=self.execution_id,
            supersedes_candidate_id=self.supersedes_candidate_id,
            status=status,
            version=self.version + 1,
        )


@dataclass(frozen=True)
class KnowledgeCandidateReview:
    review_id: UUID
    candidate_id: UUID
    reviewer_actor: str
    decision: ReviewDecision
    reason: str
    observed_candidate_hash: str
    observed_target_revision_id: UUID | None = None
    created_revision_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.decision not in {"accept", "reject", "request_changes"}:
            raise CandidateStateError("unsupported review decision")
        object.__setattr__(self, "reviewer_actor", require_text(self.reviewer_actor, "reviewer_actor"))
        object.__setattr__(self, "reason", self.reason.strip())
        object.__setattr__(
            self,
            "observed_candidate_hash",
            validate_sha256(self.observed_candidate_hash, "observed_candidate_hash"),
        )
        if self.decision == "accept" and self.created_revision_id is None:
            raise CandidateStateError("accept review requires created_revision_id")
        if self.decision != "accept" and self.created_revision_id is not None:
            raise CandidateStateError("created_revision_id is only valid for accept")


def ensure_candidate_hash(candidate: KnowledgeCandidate, observed_hash: str) -> None:
    if observed_hash != candidate.candidate_hash:
        raise CandidateStateError("candidate hash does not match observed snapshot")
