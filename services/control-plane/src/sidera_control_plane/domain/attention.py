from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AttentionSource:
    attention_key: str
    root_task_id: UUID
    resource_id: UUID
    resource_version: int
    correlation_id: UUID


@dataclass(frozen=True, slots=True)
class AttentionAcknowledgement:
    workspace_id: UUID
    actor_type: str
    actor_id: str
    attention_key: str
    root_task_id: UUID
    source_correlation_id: UUID
    source_resource_version: int
    acknowledged_command_id: UUID
    acknowledged_at: datetime

    def applies_to(self, source: AttentionSource) -> bool:
        return (
            self.attention_key == source.attention_key
            and self.root_task_id == source.root_task_id
            and self.source_correlation_id == source.correlation_id
            and self.source_resource_version == source.resource_version
        )
