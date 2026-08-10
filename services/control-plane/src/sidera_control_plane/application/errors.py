from typing import Any


class ApplicationError(Exception):
    code = "application_error"

    def __init__(
        self, message: str, *, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.details = details or {}


class NotFound(ApplicationError):
    code = "not_found"


class IdempotencyConflict(ApplicationError):
    code = "idempotency_conflict"


class UnsupportedCommand(ApplicationError):
    code = "unsupported_command"
