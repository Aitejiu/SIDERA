from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .contracts import JsonValue


class KnowledgeModel(BaseModel):
    """Strict models for the public Knowledge Service HTTP contract."""

    model_config = ConfigDict(extra="forbid")


class KnowledgeSearchHit(KnowledgeModel):
    page_id: UUID
    revision_id: UUID
    section_key: str
    revision_number: int
    revision_hash: str
    section_hash: str
    title: str
    summary: str
    tags: tuple[str, ...]
    source_ids: tuple[UUID, ...]
    source_types: tuple[str, ...]
    source_availability: tuple[str, ...]
    trust: str
    section_byte_count: int = Field(ge=0)
    rank: float
    rank_explanation: str


class KnowledgeSearchResponse(KnowledgeModel):
    items: tuple[KnowledgeSearchHit, ...]
    next_cursor: str | None


class KnowledgeSection(KnowledgeModel):
    section_key: str
    heading: str
    summary: str
    body_markdown: str
    ordinal: int = Field(ge=0)
    section_hash: str
    content_byte_count: int = Field(ge=0)


class KnowledgeRelation(KnowledgeModel):
    relation_key: str
    relation_kind: str
    target_page_id: UUID
    target_revision_id: UUID | None


class KnowledgeSource(KnowledgeModel):
    source_id: UUID
    workspace_id: UUID
    source_type: str
    label: str
    uri: str | None
    workspace_relative_path: str | None
    git_revision: str | None
    digest: str | None
    metadata: dict[str, JsonValue]
    availability: str
    safe_reason_code: str | None
    last_checked_at: datetime | None
    version: int = Field(ge=1)


class KnowledgeRevisionSummary(KnowledgeModel):
    revision_id: UUID
    page_id: UUID
    workspace_id: UUID
    revision_number: int = Field(ge=1)
    title: str
    summary: str
    tags: tuple[str, ...]
    content_hash: str
    change_reason: str
    created_by: str
    created_at: datetime
    accepted_candidate_id: UUID | None


class KnowledgeRevisionDetail(KnowledgeRevisionSummary):
    sections: tuple[KnowledgeSection, ...]
    relations: tuple[KnowledgeRelation, ...]
    source_refs: tuple[UUID, ...]
    sources: tuple[KnowledgeSource, ...]


class KnowledgePageSummary(KnowledgeModel):
    page_id: UUID
    workspace_id: UUID
    canonical_title: str
    slug: str
    status: str
    version: int = Field(ge=1)
    current_revision: KnowledgeRevisionSummary | None
    updated_at: datetime


class KnowledgePageDetail(KnowledgeModel):
    summary: KnowledgePageSummary
    current_revision: KnowledgeRevisionDetail | None
    revision_history: tuple[KnowledgeRevisionSummary, ...]


class KnowledgeCommandResult(KnowledgeModel):
    candidate_id: UUID
    status: str


class KnowledgeCommandExecution(KnowledgeModel):
    status: Literal["applied", "rejected"]
    command_id: UUID
    result: KnowledgeCommandResult | None = None
    error_code: str | None = None


class KnowledgeCommandStatus(KnowledgeModel):
    command_id: UUID
    workspace_id: UUID
    status: Literal["pending", "applied", "rejected"]
    result: KnowledgeCommandResult | None
    error_code: str | None


class KnowledgeCandidateSectionPayload(KnowledgeModel):
    section_key: str = Field(min_length=1, max_length=255)
    heading: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=16_384)
    body_markdown: str = Field(min_length=1, max_length=131_072)
    ordinal: int = Field(ge=0)


class KnowledgeCandidateRelationPayload(KnowledgeModel):
    relation_key: str = Field(min_length=1, max_length=255)
    relation_kind: Literal[
        "relates_to",
        "depends_on",
        "supersedes",
        "derived_from",
        "implements",
    ]
    target_page_id: UUID
    target_revision_id: UUID | None = None


class KnowledgeCandidateSourcePayload(KnowledgeModel):
    source_id: UUID | None = None
    source_type: Literal[
        "manual",
        "markdown_file",
        "task",
        "artifact",
        "evidence",
        "external_url",
    ]
    label: str = Field(min_length=1, max_length=500)
    uri: str | None = None
    workspace_relative_path: str | None = None
    git_revision: str | None = None
    digest: str | None = Field(default=None, min_length=64, max_length=64)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class KnowledgeCandidateProvenancePayload(KnowledgeModel):
    producer_type: str | None = Field(default=None, max_length=64)
    producer_id: str | None = Field(default=None, max_length=255)
    task_id: UUID | None = None
    subtask_id: UUID | None = None
    worker_item_id: UUID | None = None
    execution_id: UUID | None = None
    artifact_id: UUID | None = None
    evidence_id: UUID | None = None


class KnowledgeCandidatePayload(KnowledgeModel):
    candidate_schema_version: Literal[1]
    candidate_id: UUID
    operation: Literal["create_page", "revise_page"]
    target_page_id: UUID | None = None
    expected_revision_id: UUID | None = None
    expected_revision_hash: str | None = None
    title: str = Field(min_length=1, max_length=500)
    slug: str = Field(min_length=1, max_length=255)
    summary: str = Field(default="", max_length=65_536)
    tags: list[str] = Field(default_factory=list, max_length=64)
    sections: list[KnowledgeCandidateSectionPayload] = Field(
        min_length=1, max_length=128
    )
    relations: list[KnowledgeCandidateRelationPayload] = Field(
        default_factory=list, max_length=128
    )
    sources: list[KnowledgeCandidateSourcePayload] = Field(
        default_factory=list, max_length=128
    )
    provenance: KnowledgeCandidateProvenancePayload = Field(
        default_factory=KnowledgeCandidateProvenancePayload
    )
    supersedes_candidate_id: UUID | None = None
