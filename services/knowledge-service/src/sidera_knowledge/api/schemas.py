from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from sidera_knowledge.domain.commands import CommandEnvelope


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActorRequest(StrictModel):
    type: Literal["local_owner", "main_agent", "control_plane"]
    id: str = Field(min_length=1, max_length=255)


class SectionPayload(StrictModel):
    section_key: str = Field(min_length=1, max_length=255)
    heading: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=16_384)
    body_markdown: str = Field(
        min_length=1,
        max_length=131_072,
        json_schema_extra={"contentMediaType": "text/markdown"},
    )
    ordinal: int = Field(ge=0)


class RelationPayload(StrictModel):
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


class SourcePayload(StrictModel):
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
    metadata: dict[str, object] = Field(default_factory=dict)


class ProvenancePayload(StrictModel):
    producer_type: str | None = Field(default=None, max_length=64)
    producer_id: str | None = Field(default=None, max_length=255)
    task_id: UUID | None = None
    subtask_id: UUID | None = None
    worker_item_id: UUID | None = None
    execution_id: UUID | None = None
    artifact_id: UUID | None = None
    evidence_id: UUID | None = None


class SubmitCandidatePayload(StrictModel):
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
    sections: list[SectionPayload] = Field(min_length=1, max_length=128)
    relations: list[RelationPayload] = Field(default_factory=list, max_length=128)
    sources: list[SourcePayload] = Field(default_factory=list, max_length=128)
    provenance: ProvenancePayload = Field(default_factory=ProvenancePayload)
    supersedes_candidate_id: UUID | None = None


class ReviewCandidatePayload(StrictModel):
    candidate_id: UUID
    decision: Literal["accept", "reject", "request_changes"]
    reason: str = Field(min_length=1, max_length=16_384)
    observed_candidate_hash: str = Field(min_length=64, max_length=64)
    observed_target_revision_id: UUID | None = None
    observed_target_revision_hash: str | None = None
    expected_candidate_version: int = Field(ge=1)


class WithdrawCandidatePayload(StrictModel):
    candidate_id: UUID
    expected_candidate_version: int = Field(ge=1)


class ArchivePagePayload(StrictModel):
    page_id: UUID
    expected_page_version: int = Field(ge=1)


CommandType = Literal[
    "SubmitKnowledgeCandidate",
    "ReviewKnowledgeCandidate",
    "WithdrawKnowledgeCandidate",
    "ArchiveKnowledgePage",
]


class CommandRequestBase(StrictModel):
    command_id: UUID
    workspace_id: UUID
    target_type: str = Field(min_length=1, max_length=64)
    actor: ActorRequest
    target_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(min_length=1, max_length=255)
    correlation_id: UUID

    def _to_domain(
        self, command_type: CommandType, payload: StrictModel
    ) -> CommandEnvelope:
        return CommandEnvelope(
            command_id=self.command_id,
            workspace_id=self.workspace_id,
            command_type=command_type,
            actor_type=self.actor.type,
            actor_id=self.actor.id,
            target_type=self.target_type,
            target_id=self.target_id,
            payload=payload.model_dump(mode="json", exclude_none=False),
            expected_version=self.expected_version,
            idempotency_key=self.idempotency_key,
            correlation_id=self.correlation_id,
        )


class SubmitCommandRequest(CommandRequestBase):
    command_type: Literal["SubmitKnowledgeCandidate"]
    payload: SubmitCandidatePayload

    def to_domain(self) -> CommandEnvelope:
        return self._to_domain(self.command_type, self.payload)


class ReviewCommandRequest(CommandRequestBase):
    command_type: Literal["ReviewKnowledgeCandidate"]
    payload: ReviewCandidatePayload

    def to_domain(self) -> CommandEnvelope:
        return self._to_domain(self.command_type, self.payload)


class WithdrawCommandRequest(CommandRequestBase):
    command_type: Literal["WithdrawKnowledgeCandidate"]
    payload: WithdrawCandidatePayload

    def to_domain(self) -> CommandEnvelope:
        return self._to_domain(self.command_type, self.payload)


class ArchiveCommandRequest(CommandRequestBase):
    command_type: Literal["ArchiveKnowledgePage"]
    payload: ArchivePagePayload

    def to_domain(self) -> CommandEnvelope:
        return self._to_domain(self.command_type, self.payload)


CommandRequest = Annotated[
    SubmitCommandRequest
    | ReviewCommandRequest
    | WithdrawCommandRequest
    | ArchiveCommandRequest,
    Field(discriminator="command_type"),
]


class KnowledgeSectionRefRequest(StrictModel):
    ordinal: int = Field(ge=0)
    page_id: UUID
    revision_id: UUID
    revision_hash: str = Field(min_length=64, max_length=64)
    section_key: str = Field(min_length=1, max_length=255)
    section_hash: str = Field(min_length=64, max_length=64)
    source_ids: list[UUID] = Field(default_factory=list, max_length=128)

    def to_domain(self):
        from sidera_knowledge.application.knowledge_queries import KnowledgeSectionRef

        return KnowledgeSectionRef(
            ordinal=self.ordinal,
            page_id=self.page_id,
            revision_id=self.revision_id,
            revision_hash=self.revision_hash,
            section_key=self.section_key,
            section_hash=self.section_hash,
            source_ids=tuple(self.source_ids),
        )


class MaterializeRequest(StrictModel):
    sections: list[KnowledgeSectionRefRequest] = Field(
        default_factory=list, max_length=32
    )
