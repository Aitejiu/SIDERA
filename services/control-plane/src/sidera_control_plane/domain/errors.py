class DomainError(Exception):
    code = "domain_error"


class ValidationError(DomainError):
    code = "validation_error"


class InvalidTransition(DomainError):
    code = "invalid_transition"


class VersionConflict(DomainError):
    code = "version_conflict"

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"expected version {expected}, actual version {actual}")
        self.expected = expected
        self.actual = actual


class CapacityExceeded(DomainError):
    code = "capacity_exceeded"


class StaleFencingToken(DomainError):
    code = "stale_fencing_token"


class LeaseExpired(DomainError):
    code = "lease_expired"
