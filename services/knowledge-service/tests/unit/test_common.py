from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sidera_knowledge.domain.common import (
    DomainValidationError,
    canonical_json_bytes,
    normalize_markdown,
    normalize_tags,
    sha256_hex,
    validate_workspace_relative_posix_path,
)


def test_canonical_json_is_compact_sorted_and_order_independent() -> None:
    left = canonical_json_bytes({"z": [1, True], "a": "知识"})
    right = canonical_json_bytes({"a": "知识", "z": [1, True]})

    assert left == right
    assert left == b'{"a":"\xe7\x9f\xa5\xe8\xaf\x86","z":[1,true]}'
    assert sha256_hex(left) == sha256_hex(right)
    assert len(sha256_hex(left)) == 64


@pytest.mark.parametrize(
    "value",
    [1.5, b"bytes", datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)],
)
def test_canonical_json_rejects_unsafe_values(value: object) -> None:
    with pytest.raises(DomainValidationError):
        canonical_json_bytes(value)


def test_canonical_json_accepts_timezone_aware_datetime() -> None:
    assert canonical_json_bytes(datetime(2026, 1, 1, tzinfo=UTC)) == (
        b'"2026-01-01T00:00:00+00:00"'
    )


def test_normalize_markdown_uses_lf_rejects_nul_and_keeps_one_trailing_lf() -> None:
    assert normalize_markdown("\ufeff# Title\r\n\r\nbody\n\n") == (
        "# Title\n\nbody\n"
    )

    with pytest.raises(DomainValidationError):
        normalize_markdown("unsafe\x00body")


def test_markdown_size_is_measured_as_utf8_bytes() -> None:
    value = normalize_markdown("知识")
    assert len(value.encode("utf-8")) == 7


def test_tags_are_trimmed_deduplicated_and_stable() -> None:
    assert normalize_tags([" beta ", "alpha", "beta", "alpha"]) == (
        "alpha",
        "beta",
    )


@pytest.mark.parametrize("path", ["/tmp/x", "../x", "a/../../x", "~/x", "a\\b"])
def test_workspace_path_rejects_absolute_parent_and_non_posix_paths(path: str) -> None:
    with pytest.raises(DomainValidationError):
        validate_workspace_relative_posix_path(path)


def test_workspace_path_returns_normalized_posix_path() -> None:
    assert validate_workspace_relative_posix_path(" docs/design.md ") == (
        "docs/design.md"
    )
