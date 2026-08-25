from __future__ import annotations

from uuid import uuid4

import pytest

from sidera_knowledge.application.retrieval import (
    RetrievalQueryError,
    SearchFilters,
    SearchHit,
    validate_search_request,
)


def test_search_hit_is_metadata_only() -> None:
    hit = SearchHit(
        page_id=uuid4(),
        revision_id=uuid4(),
        section_key="overview",
        revision_number=1,
        revision_hash="a" * 64,
        section_hash="b" * 64,
        title="Guide",
        summary="Summary",
        tags=("sidera",),
        source_ids=(),
        source_types=(),
        source_availability=(),
        trust="standard",
        section_byte_count=12,
        rank=1.0,
        rank_explanation="weighted title/tags > summary > section body",
    )

    assert hit.section_key == "overview"
    assert not hasattr(hit, "body_markdown")


def test_empty_query_requires_an_explicit_filter() -> None:
    with pytest.raises(RetrievalQueryError, match="query or filter"):
        validate_search_request("", SearchFilters())

    validate_search_request("", SearchFilters(tags=("sidera",)))
    validate_search_request("guide", SearchFilters())


def test_search_filter_limit_is_bounded() -> None:
    with pytest.raises(RetrievalQueryError, match="limit"):
        validate_search_request("guide", SearchFilters(), limit=51)
    with pytest.raises(RetrievalQueryError, match="status"):
        validate_search_request(
            "guide", SearchFilters(status="unsupported")
        )
