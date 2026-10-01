from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ..contracts import (
    ConfigIssue,
    ProfileRevision,
    RuntimeCandidate,
    RuntimeDescriptor,
    RuntimeProbe,
    RuntimeSnapshot,
)
from ..errors import AgentError


class TrustedHarness(Protocol):
    harness_id: str
    executable_name: str

    def describe(self) -> RuntimeDescriptor: ...

    def probe(self, candidate: RuntimeCandidate, checked_at) -> RuntimeProbe: ...

    def validate(
        self, profile: ProfileRevision, runtime: RuntimeSnapshot
    ) -> tuple[ConfigIssue, ...]: ...


class HarnessRegistry:
    """Static registry of trusted harness implementations."""

    def __init__(self, harnesses: Iterable[TrustedHarness]) -> None:
        entries: dict[str, TrustedHarness] = {}
        for harness in harnesses:
            if not harness.harness_id or harness.harness_id in entries:
                raise ValueError("Harness IDs must be unique and non-empty.")
            entries[harness.harness_id] = harness
        self._entries = entries

    def get(self, harness_id: str) -> TrustedHarness:
        try:
            return self._entries[harness_id]
        except KeyError:
            raise AgentError(
                "unsupported_harness", "Harness is not supported."
            ) from None

    def all(self) -> tuple[TrustedHarness, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))


def default_harness_registry() -> HarnessRegistry:
    from .opencode_descriptor import OpenCodeDescriptor

    return HarnessRegistry((OpenCodeDescriptor(),))
