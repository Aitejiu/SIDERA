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
