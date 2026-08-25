from __future__ import annotations

from typing import Any

from sidera_knowledge.domain.common import DomainValidationError


class CommandFailure(DomainValidationError):
    """A safe domain rejection that can become a stable command error code."""

    def __init__(self, code: str, details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(code)
