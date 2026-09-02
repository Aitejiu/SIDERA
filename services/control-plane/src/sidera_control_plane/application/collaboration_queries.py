from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sidera_control_plane.application.errors import InvalidCursor, NotFound
from sidera_control_plane.domain.collaboration_projections import (
    CollaborationDecisionProjection,
    CollaborationDetailProjection,
    CollaborationProjectionFacts,
    CollaborationProjectionService,
)


@dataclass(frozen=True, slots=True)
class CollaborationDecisionPageView:
    items: tuple[CollaborationDecisionProjection, ...]
    next_cursor: str | None


class CollaborationQueryStore(Protocol):
    def load(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> CollaborationProjectionFacts | None:
        raise NotImplementedError


class CollaborationQueryService:
    def __init__(self, store: CollaborationQueryStore) -> None:
        self._store = store
        self._projection = CollaborationProjectionService()

    def get_detail(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> CollaborationDetailProjection:
        facts = self._require_facts(workspace_id, root_task_id)
        return self._projection.detail(facts)

    def list_decisions(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> CollaborationDecisionPageView:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        facts = self._require_facts(workspace_id, root_task_id)
        page = self._projection.decision_history_page(
            facts,
            after=decode_decision_cursor(cursor) if cursor else None,
            limit=limit,
        )
        return CollaborationDecisionPageView(
            items=page.items,
            next_cursor=(
                encode_decision_cursor(*page.next_after)
                if page.next_after is not None
                else None
            ),
        )

    def _require_facts(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> CollaborationProjectionFacts:
        facts = self._store.load(workspace_id, root_task_id)
        if (
            facts is None
            or facts.plan.workspace_id != workspace_id
            or facts.plan.root_task_id != root_task_id
            or facts.run.workspace_id != workspace_id
            or facts.run.root_task_id != root_task_id
            or facts.run.plan_snapshot_id != facts.plan.snapshot_id
            or facts.run.plan_generation != facts.plan.generation
        ):
            raise NotFound("collaboration view not found")
        return facts


def encode_decision_cursor(created_at: datetime, decision_id: UUID) -> str:
    value = json.dumps(
        [created_at.astimezone(UTC).isoformat(), str(decision_id)],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_decision_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("cursor payload shape")
        created_at = datetime.fromisoformat(value[0])
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        decision_id = UUID(value[1])
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise InvalidCursor("invalid collaboration decision cursor") from exc
    return created_at.astimezone(UTC), decision_id
