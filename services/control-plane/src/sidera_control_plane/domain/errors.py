class DomainError(Exception):
    code = "domain_error"

    def __init__(
        self, message: str, *, details: dict[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.details = details or {}


class ReplayedDomainError(DomainError):
    def __init__(
        self, code: str, message: str, details: dict[str, object]
    ) -> None:
        super().__init__(message, details=details)
        self.code = code


class ValidationError(DomainError):
    code = "validation_error"


class OperatorRequired(ValidationError):
    code = "operator_required"


class InvalidTransition(DomainError):
    code = "invalid_transition"


class VersionConflict(DomainError):
    code = "version_conflict"

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            f"expected version {expected}, actual version {actual}",
            details={"expected": expected, "actual": actual},
        )
        self.expected = expected
        self.actual = actual


class ExecutionContextChanged(DomainError):
    code = "execution_context_changed"

    def __init__(
        self,
        changed_fields: tuple[str, ...],
        *,
        current: dict[str, object] | None = None,
    ) -> None:
        super().__init__(
            "execution context changed; refresh the Session before sending input",
            details={
                "changed_fields": list(changed_fields),
                "current": current or {},
            },
        )
        self.changed_fields = changed_fields


class PlanGenerationConflict(DomainError):
    code = "plan_generation_conflict"

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            f"expected plan generation {expected}, actual {actual}",
            details={"expected": expected, "actual": actual},
        )
        self.expected = expected
        self.actual = actual


class CapacityExceeded(DomainError):
    code = "capacity_exceeded"


class StaleFencingToken(DomainError):
    code = "stale_fencing_token"


class LeaseExpired(DomainError):
    code = "lease_expired"
