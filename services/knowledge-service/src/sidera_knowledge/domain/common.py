from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID


class DomainValidationError(ValueError):
    """Raised when a Knowledge domain value cannot be safely persisted."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        raise DomainValidationError("float values are not allowed in canonical JSON")
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise DomainValidationError("binary values are not allowed in canonical JSON")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise DomainValidationError("naive datetime is not allowed in canonical JSON")
        return value.isoformat()
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise DomainValidationError("canonical JSON object keys must be strings")
            result[key] = _canonical_value(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_value(item) for item in value]
    raise DomainValidationError(
        f"unsupported canonical JSON value: {type(value).__name__}"
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a domain value with one deterministic JSON representation."""
    try:
        return json.dumps(
            _canonical_value(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, DomainValidationError):
            raise
        raise DomainValidationError("value cannot be serialized canonically") from exc


def sha256_hex(value: bytes | str) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def normalize_markdown(value: str) -> str:
    if not isinstance(value, str):
        raise DomainValidationError("markdown must be text")
    if "\x00" in value:
        raise DomainValidationError("markdown cannot contain NUL")
    normalized = value.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    return normalized.rstrip("\n") + "\n"


def normalize_tags(values: Sequence[str]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise DomainValidationError("tags must be text")
        item = value.strip()
        if not item:
            raise DomainValidationError("tags cannot be empty")
        normalized.add(item)
    return tuple(sorted(normalized))


def require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} cannot be empty")
    return value.strip()


def validate_sha256(value: str, field_name: str = "hash") -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise DomainValidationError(f"{field_name} must be a lowercase SHA-256 hash")
    return value


def validate_workspace_relative_posix_path(value: str) -> str:
    normalized = require_text(value, "workspace_relative_path")
    if "\\" in normalized or normalized.startswith("~"):
        raise DomainValidationError("source path must be a POSIX relative path")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DomainValidationError("source path must stay inside the workspace")
    return str(path)
