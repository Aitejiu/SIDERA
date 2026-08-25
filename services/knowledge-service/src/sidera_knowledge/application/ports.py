from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from sidera_knowledge.domain.candidates import (
    KnowledgeCandidate,
    KnowledgeCandidateReview,
)
from sidera_knowledge.domain.pages import (
    KnowledgePage,
    KnowledgeRevision,
    KnowledgeSource,
)

if TYPE_CHECKING:
    from sidera_knowledge.application.outbox_dispatcher import OutboxMessage
    from sidera_knowledge.application.retrieval import SearchFilters, SearchHit


class KnowledgePageRepositoryPort(Protocol):
    def add_page(self, page: KnowledgePage) -> None: ...

    def get_page(self, page_id: UUID, workspace_id: UUID) -> KnowledgePage | None: ...

    def get_page_for_update(
        self, page_id: UUID, workspace_id: UUID
    ) -> KnowledgePage | None: ...

    def get_revision(
        self, revision_id: UUID, workspace_id: UUID
    ) -> KnowledgeRevision | None: ...

    def add_revision_and_advance_page(
        self, revision: KnowledgeRevision, expected_page_version: int
    ) -> None: ...

    def archive_page(
        self, page_id: UUID, workspace_id: UUID, expected_version: int
    ) -> KnowledgePage: ...


class KnowledgeSourceRepositoryPort(Protocol):
    def add_source(self, source: KnowledgeSource) -> None: ...

    def get_source(self, source_id: UUID, workspace_id: UUID) -> KnowledgeSource | None: ...

    def find_by_digest(
        self, workspace_id: UUID, source_type: str, digest: str
    ) -> KnowledgeSource | None: ...

    def mark_unavailable(
        self,
        source_id: UUID,
        workspace_id: UUID,
        expected_version: int,
        reason_code: str,
        checked_at: datetime,
    ) -> KnowledgeSource: ...


class KnowledgeCandidateRepositoryPort(Protocol):
    def add_candidate(self, candidate: KnowledgeCandidate) -> None: ...

    def get_candidate(
        self, candidate_id: UUID, workspace_id: UUID
    ) -> KnowledgeCandidate | None: ...

    def get_candidate_for_update(
        self, candidate_id: UUID, workspace_id: UUID
    ) -> KnowledgeCandidate | None: ...

    def transition_candidate(
        self, candidate_id: UUID, workspace_id: UUID, status: str, expected_version: int
    ) -> None: ...

    def add_review(self, review: KnowledgeCandidateReview, workspace_id: UUID) -> None: ...


class CommandRepositoryPort(Protocol):
    def save_or_replay(self, command: CommandEnvelope): ...

    def mark_applied(
        self, command_id: UUID, workspace_id: UUID, result: dict[str, object]
    ) -> None: ...

    def mark_rejected(
        self,
        command_id: UUID,
        workspace_id: UUID,
        error_code: str,
        details: dict[str, object],
    ) -> None: ...


class EventRepositoryPort(Protocol):
    def append_with_outbox(self, event: EventEnvelope) -> None: ...


class OutboxMessageStorePort(Protocol):
    def claim_batch(
        self,
        *,
        now: datetime,
        lease_id: UUID,
        lease_until: datetime,
        limit: int,
    ) -> tuple[OutboxMessage, ...]: ...

    def mark_published(
        self, *, message_id: UUID, lease_id: UUID, published_at: datetime
    ) -> None: ...

    def mark_failed(
        self,
        *,
        message_id: UUID,
        lease_id: UUID,
        available_at: datetime,
        error_code: str,
        dead_lettered_at: datetime | None,
    ) -> None: ...


class OutboxSinkPort(Protocol):
    def publish(self, message: OutboxMessage) -> None: ...


class KnowledgeUnitOfWorkPort(Protocol):
    pages: KnowledgePageRepositoryPort
    sources: KnowledgeSourceRepositoryPort
    candidates: KnowledgeCandidateRepositoryPort
    commands: CommandRepositoryPort
    events: EventRepositoryPort

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def flush(self) -> None: ...


class RetrievalAdapter(Protocol):
    def search(
        self,
        workspace_id: UUID,
        query: str,
        filters: SearchFilters,
        limit: int = 20,
    ) -> tuple[SearchHit, ...]: ...
from sidera_knowledge.domain.commands import CommandEnvelope, EventEnvelope
