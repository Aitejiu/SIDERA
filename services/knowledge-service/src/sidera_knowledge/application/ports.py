from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sidera_knowledge.domain.candidates import KnowledgeCandidate
from sidera_knowledge.domain.pages import (
    KnowledgePage,
    KnowledgeRevision,
    KnowledgeSource,
)


class KnowledgePageRepositoryPort(Protocol):
    def add_page(self, page: KnowledgePage) -> None: ...

    def get_page(self, page_id: UUID, workspace_id: UUID) -> KnowledgePage | None: ...

    def get_revision(
        self, revision_id: UUID, workspace_id: UUID
    ) -> KnowledgeRevision | None: ...

    def add_revision_and_advance_page(
        self, revision: KnowledgeRevision, expected_page_version: int
    ) -> None: ...


class KnowledgeSourceRepositoryPort(Protocol):
    def add_source(self, source: KnowledgeSource) -> None: ...

    def get_source(self, source_id: UUID, workspace_id: UUID) -> KnowledgeSource | None: ...


class KnowledgeCandidateRepositoryPort(Protocol):
    def add_candidate(self, candidate: KnowledgeCandidate) -> None: ...

    def get_candidate(
        self, candidate_id: UUID, workspace_id: UUID
    ) -> KnowledgeCandidate | None: ...


class KnowledgeUnitOfWorkPort(Protocol):
    pages: KnowledgePageRepositoryPort
    sources: KnowledgeSourceRepositoryPort
    candidates: KnowledgeCandidateRepositoryPort

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def flush(self) -> None: ...
