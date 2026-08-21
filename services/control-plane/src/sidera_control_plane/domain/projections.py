from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

SUPPORTED_PROJECTION_SCHEMA_VERSIONS = frozenset({1})


@dataclass(frozen=True, slots=True)
class ProjectionChange:
    workspace_id: UUID
    cursor: int
    schema_version: int
    projection_type: str
    resource_id: UUID | None
    change_type: str
    resource_version: int | None
    occurred_at: datetime
    correlation_id: UUID

    def to_payload(self) -> dict[str, Any]:
        return {
            "workspace_id": str(self.workspace_id),
            "cursor": self.cursor,
            "schema_version": self.schema_version,
            "projection_type": self.projection_type,
            "resource_id": str(self.resource_id) if self.resource_id else None,
            "change_type": self.change_type,
            "resource_version": self.resource_version,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "correlation_id": str(self.correlation_id),
        }
