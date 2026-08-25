from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sidera_knowledge.application.ports import RetrievalAdapter
from sidera_knowledge.domain.common import DomainValidationError


class RetrievalQueryError(DomainValidationError):
    """Raised when a search request cannot be safely evaluated."""


@dataclass(frozen=True)
class SearchFilters:
    tags: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    status: str | None = "active"

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", tuple(self.tags))
        object.__setattr__(self, "source_types", tuple(self.source_types))


@dataclass(frozen=True)
class SearchHit:
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
    section_byte_count: int
    rank: float
    rank_explanation: str


def validate_search_request(
    query: str,
    filters: SearchFilters,
    limit: int = 20,
) -> None:
    if not isinstance(query, str):
        raise RetrievalQueryError("query must be text")
    if limit < 1 or limit > 50:
        raise RetrievalQueryError("limit must be between 1 and 50")
    if filters.status not in {None, "active", "archived"}:
        raise RetrievalQueryError("status must be active or archived")
    if not query.strip() and not (
        filters.tags or filters.source_types or filters.status == "archived"
    ):
        raise RetrievalQueryError("query or filter is required")


__all__ = [
    "RetrievalAdapter",
    "RetrievalQueryError",
    "SearchFilters",
    "SearchHit",
    "validate_search_request",
]
