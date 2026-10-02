from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .contracts import ContextItem, ProfileRevision, RunStart, RuntimeSnapshot


@dataclass(frozen=True, slots=True)
class LiveRunContext:
    """Non-durable projection required to continue one already-open native session."""

    request: RunStart
    profile: ProfileRevision
    runtime: RuntimeSnapshot

    @classmethod
    def from_start(
        cls, request: RunStart, profile: ProfileRevision, runtime: RuntimeSnapshot
    ) -> LiveRunContext:
        projection = request.model_copy(update={"text": "", "context": ()})
        return cls(request=projection, profile=profile, runtime=runtime)

    @property
    def grant_id(self) -> UUID:
        return self.request.grant_id

    def request_for_input(
        self, operation_id: UUID, text: str, context: tuple[ContextItem, ...]
    ) -> RunStart:
        return self.request.model_copy(
            update={"operation_id": operation_id, "text": text, "context": context}
        )


def supports_session_load(capabilities: tuple[str, ...]) -> bool:
    return "session_load" in capabilities
