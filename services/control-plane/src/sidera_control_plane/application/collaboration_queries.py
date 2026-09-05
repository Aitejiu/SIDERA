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
    WorkerItemCollaborationSummaryProjection,
)


@dataclass(frozen=True, slots=True)
class CollaborationDecisionPageView:
    items: tuple[CollaborationDecisionProjection, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class CollaborationGovernanceWorkerItemView:
    workspace_id: UUID
    root_task_id: UUID
    subtask_id: UUID | None
    subtask_key: str
    subtask_title: str
    worker_item_id: UUID
    worker_item_key: str
    worker_item_title: str
    worker_item_role: str
    worker_item_responsibility: str
    governance: WorkerItemCollaborationSummaryProjection


@dataclass(frozen=True, slots=True)
class CollaborationGovernanceProposalView:
    workspace_id: UUID
    root_task_id: UUID
    subtask_id: UUID | None
    subtask_key: str
    subtask_title: str
    worker_item_id: UUID
    worker_item_key: str
    worker_item_title: str
    proposal_id: UUID
    status: str
    resource_version: int
    target_request_version: int
    reason_code: str
    summary: str
    created_at: datetime
    resolved_at: datetime | None
    hold_id: UUID
    hold_resource_version: int
    governance_mode: str
    available_actions: tuple[str, ...]
    deep_link: str
    input_selection_id: UUID | None = None
    input_selection_hash: str | None = None


@dataclass(frozen=True, slots=True)
class _WorkerPath:
    subtask_id: UUID | None
    subtask_key: str
    subtask_title: str
    worker_item_key: str
    worker_item_title: str
    worker_item_role: str
    worker_item_responsibility: str


class CollaborationQueryStore(Protocol):
    def load(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> CollaborationProjectionFacts | None:
        raise NotImplementedError

    def load_for_worker_item(
        self, workspace_id: UUID, worker_item_id: UUID
    ) -> CollaborationProjectionFacts | None:
        raise NotImplementedError

    def load_for_proposal(
        self, workspace_id: UUID, proposal_id: UUID
    ) -> CollaborationProjectionFacts | None:
        raise NotImplementedError


class CollaborationQueryService:
    def __init__(
        self, store: CollaborationQueryStore, *, governance_enabled: bool = True
    ) -> None:
        self._store = store
        self._projection = CollaborationProjectionService(
            governance_enabled=governance_enabled
        )

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

    def get_worker_item_summary(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        worker_item_id: UUID,
    ) -> WorkerItemCollaborationSummaryProjection:
        facts = self._require_facts(workspace_id, root_task_id)
        summary = self._projection.worker_summary(facts, worker_item_id=worker_item_id)
        if summary is None:
            raise NotFound("collaboration worker item not found")
        return summary

    def get_governance_worker_item(
        self, workspace_id: UUID, worker_item_id: UUID
    ) -> CollaborationGovernanceWorkerItemView:
        facts = self._store.load_for_worker_item(workspace_id, worker_item_id)
        facts = self._require_facts_for_lookup(facts)
        path = self._worker_path(facts, worker_item_id)
        summary = self._projection.worker_summary(facts, worker_item_id=worker_item_id)
        if summary is None:
            raise NotFound("collaboration worker item not found")
        return CollaborationGovernanceWorkerItemView(
            workspace_id=workspace_id,
            root_task_id=facts.plan.root_task_id,
            subtask_id=path.subtask_id,
            subtask_key=path.subtask_key,
            subtask_title=path.subtask_title,
            worker_item_id=worker_item_id,
            worker_item_key=path.worker_item_key,
            worker_item_title=path.worker_item_title,
            worker_item_role=path.worker_item_role,
            worker_item_responsibility=path.worker_item_responsibility,
            governance=summary,
        )

    def get_governance_proposal(
        self, workspace_id: UUID, proposal_id: UUID
    ) -> CollaborationGovernanceProposalView:
        facts = self._store.load_for_proposal(workspace_id, proposal_id)
        facts = self._require_facts_for_lookup(facts)
        proposal = next(
            (item for item in facts.proposals if item.id == proposal_id), None
        )
        if proposal is None:
            raise NotFound("collaboration release proposal not found")
        path = self._worker_path(facts, proposal.worker_item_id)
        hold = next((item for item in facts.holds if item.id == proposal.hold_id), None)
        summary = self._projection.worker_summary(
            facts, worker_item_id=proposal.worker_item_id
        )
        if hold is None or summary is None:
            raise NotFound("collaboration release proposal not found")
        return CollaborationGovernanceProposalView(
            workspace_id=workspace_id,
            root_task_id=facts.plan.root_task_id,
            subtask_id=path.subtask_id,
            subtask_key=path.subtask_key,
            subtask_title=path.subtask_title,
            worker_item_id=proposal.worker_item_id,
            worker_item_key=path.worker_item_key,
            worker_item_title=path.worker_item_title,
            proposal_id=proposal.id,
            status=proposal.status,
            resource_version=proposal.resource_version,
            target_request_version=proposal.target_request_version,
            reason_code=proposal.reason_code,
            summary=proposal.summary,
            created_at=proposal.created_at,
            resolved_at=proposal.resolved_at,
            hold_id=hold.id,
            hold_resource_version=hold.resource_version,
            governance_mode=summary.governance_mode,
            available_actions=(
                summary.available_actions if proposal.status == "pending" else ()
            ),
            deep_link=f"/workspaces/{workspace_id}/worker-items/{proposal.worker_item_id}",
            input_selection_id=proposal.input_selection_id,
            input_selection_hash=proposal.input_selection_hash,
        )

    @staticmethod
    def _require_facts_for_lookup(
        facts: CollaborationProjectionFacts | None,
    ) -> CollaborationProjectionFacts:
        if facts is None:
            raise NotFound("collaboration view not found")
        return facts

    @staticmethod
    def _worker_path(
        facts: CollaborationProjectionFacts, worker_item_id: UUID
    ) -> _WorkerPath:
        request = next(
            (item for item in facts.requests if item.worker_item_id == worker_item_id),
            None,
        )
        if request is None:
            raise NotFound("collaboration worker item not found")
        worker_plan = next(
            (
                item
                for item in facts.plan.worker_items
                if item.worker_item_id == worker_item_id
            ),
            None,
        )
        subtask = next(
            (item for item in facts.plan.subtasks if item.key == request.subtask_key),
            None,
        )
        return _WorkerPath(
            subtask_id=subtask.task_id if subtask is not None else None,
            subtask_key=request.subtask_key,
            subtask_title=subtask.title if subtask is not None else request.subtask_key,
            worker_item_key=(
                worker_plan.key if worker_plan is not None else request.worker_item_key
            ),
            worker_item_title=(
                worker_plan.title
                if worker_plan is not None
                else request.worker_item_key
            ),
            worker_item_role=worker_plan.role if worker_plan is not None else "",
            worker_item_responsibility=(
                worker_plan.responsibility
                if worker_plan is not None
                else request.worker_item_key
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
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        UnicodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise InvalidCursor("invalid collaboration decision cursor") from exc
    return created_at.astimezone(UTC), decision_id
