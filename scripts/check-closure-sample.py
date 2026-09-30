#!/usr/bin/env python3
"""Check the fixed closure sample without importing or executing its contents."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

SAMPLE_VERSION = "closure-sample-v1"
_FILES = ("out/source-a.json", "out/source-b.json", "out/summary.json")
_SOURCE_A = {
    "marker": "SIDERA_CLOSURE_SOURCE_A_V1",
    "items": [
        {"id": "api", "status": "done"},
        {"id": "ui", "status": "queued"},
    ],
}
_SOURCE_B = {
    "marker": "SIDERA_CLOSURE_SOURCE_B_V1",
    "items": [
        {"id": "tests", "status": "running"},
        {"id": "docs", "status": "blocked"},
    ],
}
_SUMMARY = {
    "total": 4,
    "counts": {"done": 1, "queued": 1, "running": 1, "blocked": 1},
    "source_markers": [
        "SIDERA_CLOSURE_SOURCE_A_V1",
        "SIDERA_CLOSURE_SOURCE_B_V1",
    ],
}


class SampleCheckError(ValueError):
    """The candidate checkout does not contain the exact expected sample."""


def check_sample(repository: Path) -> dict[str, object]:
    root = _validated_root(repository)
    bodies = {relative: _read_json(root, relative) for relative in _FILES}
    _assert_exact(bodies["out/source-a.json"], _SOURCE_A, "source_a")
    _assert_exact(bodies["out/source-b.json"], _SOURCE_B, "source_b")
    _assert_exact(bodies["out/summary.json"], _SUMMARY, "summary")
    _assert_summary_recomputed(bodies["out/source-a.json"], bodies["out/source-b.json"], bodies["out/summary.json"])
    digest = hashlib.sha256()
    artifacts: dict[str, str] = {}
    for relative in _FILES:
        body = (root / relative).read_bytes()
        artifacts[relative] = hashlib.sha256(body).hexdigest()
        digest.update(body)
    return {
        "sample_version": SAMPLE_VERSION,
        "status": "PASS",
        "checks": (
            "source_a_exact",
            "source_b_exact",
            "summary_exact",
            "summary_recomputed",
        ),
        "artifact_sha256": digest.hexdigest(),
        "file_sha256": artifacts,
    }


def _validated_root(repository: Path) -> Path:
    root = repository.expanduser()
    if not root.is_absolute():
        raise SampleCheckError("repository path must be absolute")
    if root.is_symlink() or not root.is_dir():
        raise SampleCheckError("repository must be a real directory")
    resolved = root.resolve(strict=True)
    if resolved != root and root.is_symlink():
        raise SampleCheckError("repository symlink is not allowed")
    return resolved


def _read_json(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    parent = path.parent.resolve(strict=False)
    if path.is_symlink() or parent != root / "out":
        raise SampleCheckError(f"{relative}: symlink or path escape")
    try:
        info = path.stat()
    except OSError as exc:
        raise SampleCheckError(f"{relative}: missing") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024:
        raise SampleCheckError(f"{relative}: invalid file")
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise SampleCheckError(f"{relative}: invalid JSON") from exc
    if not isinstance(raw, dict):
        raise SampleCheckError(f"{relative}: object required")
    return raw


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _assert_exact(actual: object, expected: object, label: str) -> None:
    if actual != expected or _has_bool_number_confusion(actual, expected):
        raise SampleCheckError(f"{label}: exact structure mismatch")


def _has_bool_number_confusion(actual: object, expected: object) -> bool:
    if isinstance(actual, dict) and isinstance(expected, dict):
        return any(
            key not in actual or _has_bool_number_confusion(actual[key], expected[key])
            for key in expected
        )
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) != len(expected) or any(
            _has_bool_number_confusion(left, right)
            for left, right in zip(actual, expected)
        )
    if isinstance(expected, int) and not isinstance(expected, bool):
        return isinstance(actual, bool)
    return False


def _assert_summary_recomputed(
    source_a: dict[str, Any],
    source_b: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    items = [*source_a["items"], *source_b["items"]]
    counts = {status: 0 for status in ("done", "queued", "running", "blocked")}
    for item in items:
        status = item.get("status")
        if status not in counts:
            raise SampleCheckError("summary: unknown item status")
        counts[status] += 1
    expected = {
        "total": len(items),
        "counts": counts,
        "source_markers": [source_a["marker"], source_b["marker"]],
    }
    _assert_exact(summary, expected, "summary recomputation")


def main() -> int:
    parser = argparse.ArgumentParser()
    repository_group = parser.add_mutually_exclusive_group(required=True)
    repository_group.add_argument("--repository", type=Path)
    repository_group.add_argument(
        "--repository-cwd",
        action="store_true",
        help="check the current working directory",
    )
    args = parser.parse_args()
    try:
        result = check_sample(Path.cwd() if args.repository_cwd else args.repository)
    except (OSError, SampleCheckError, ValueError) as exc:
        print(json.dumps({"sample_version": SAMPLE_VERSION, "status": "FAIL", "error": str(exc)}))
        return 2 if "path" in str(exc) or "directory" in str(exc) else 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
