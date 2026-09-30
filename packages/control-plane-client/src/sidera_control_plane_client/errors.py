from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from .contracts import JsonValue

_SECRET_MARKER_PATTERN = re.compile(
    r"(?:api[_ -]?key|authorization|bearer|password|secret|token)", re.IGNORECASE
)
_PROTOCOL_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,127}")
_DROP = object()


class TransportUnavailable(RuntimeError):
    """The control plane could not be read or reached."""


class ProjectionResyncRequired(RuntimeError):
    """The stream cannot safely continue from its current cursor."""

    def __init__(self, reason: str, last_confirmed_cursor: int) -> None:
        self.reason = reason
        self.last_confirmed_cursor = last_confirmed_cursor
        super().__init__(f"projection stream requires resync: {reason}")


class ProjectionStreamDisconnected(RuntimeError):
    """The stream ended or lost transport before a safe terminal event."""

    def __init__(self, last_confirmed_cursor: int) -> None:
        self.last_confirmed_cursor = last_confirmed_cursor
        super().__init__("projection stream disconnected")


class CommandResultUnknown(RuntimeError):
    """A command POST may have reached the control plane without a response."""

    def __init__(self, command_id: UUID) -> None:
        super().__init__("command result is unknown")
        self.command_id = command_id


class ControlPlaneRejected(RuntimeError):
    """A safe, structured error returned by the control plane."""

    def __init__(self, code: str, details: Mapping[str, JsonValue]) -> None:
        if not isinstance(code, str) or _PROTOCOL_CODE_PATTERN.fullmatch(code) is None:
            code = "invalid_control_plane_response"
            details = {}
        super().__init__(code)
        self.code = code
        self.details = _safe_details(details)


def _safe_details(details: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    safe: dict[str, Any] = {}
    for key, value in details.items():
        if not isinstance(key, str) or _SECRET_MARKER_PATTERN.search(key):
            continue
        if key.lower() in {"message", "detail", "raw_body", "response_body"}:
            continue
        clean = _safe_detail_value(value)
        if clean is not _DROP:
            safe[key] = clean
    return safe


def _safe_detail_value(value: Any) -> Any:
    if isinstance(value, str):
        if _SECRET_MARKER_PATTERN.search(value) or "sk-" in value.lower():
            return _DROP
        return value
    if isinstance(value, Mapping):
        return _safe_details(value)
    if isinstance(value, (list, tuple)):
        safe_values = []
        for item in value:
            clean = _safe_detail_value(item)
            if clean is not _DROP:
                safe_values.append(clean)
        return safe_values
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _DROP
