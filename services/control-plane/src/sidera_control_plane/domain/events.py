from dataclasses import dataclass
from typing import Any
from uuid import UUID

HANDOFF_PROPOSAL_EVENT_TYPES = frozenset(
    {
        "HandoffProposalPrepared",
        "HandoffProposalStale",
        "HandoffProposalExpired",
        "HandoffProposalCancelled",
    }
)


@dataclass(frozen=True, slots=True)
class EventData:
    id: UUID
    workspace_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    event_type: str
    payload: dict[str, Any]
    actor_type: str
    actor_id: str
    causation_id: UUID
    correlation_id: UUID
