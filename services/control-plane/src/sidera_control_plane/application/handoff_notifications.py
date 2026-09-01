from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.domain.events import EventData


class HandoffNotificationKind(StrEnum):
    ATTENTION_REQUIRED = "handoff_attention_required"
    TARGET_TOOK_OVER = "handoff_target_took_over"
    CONVERGED = "handoff_converged"


@dataclass(frozen=True, slots=True)
class HandoffNotificationContext:
    """Human-readable WorkerItem scope resolved from authoritative projections."""

    root_task_id: UUID
    root_task_title: str
    subtask_path: tuple[str, ...]
    worker_item_id: UUID
    worker_item_title: str
    deep_link: str
    correlation_id: UUID
    resource_version: int
    important: bool = False


@dataclass(frozen=True, slots=True)
class HandoffNotification:
    notification_id: UUID
    source_event_id: UUID
    kind: HandoffNotificationKind
    root_task_id: UUID
    root_task_title: str
    subtask_path: tuple[str, ...]
    worker_item_id: UUID
    worker_item_title: str
    summary: str
    action: str
    deep_link: str
    correlation_id: UUID
    resource_version: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "notification_id": str(self.notification_id),
            "source_event_id": str(self.source_event_id),
            "kind": self.kind.value,
            "root_task_id": str(self.root_task_id),
            "root_task_title": self.root_task_title,
            "subtask_path": list(self.subtask_path),
            "worker_item_id": str(self.worker_item_id),
            "worker_item_title": self.worker_item_title,
            "summary": self.summary,
            "action": self.action,
            "deep_link": self.deep_link,
            "correlation_id": str(self.correlation_id),
            "resource_version": self.resource_version,
        }


class HandoffNotificationService:
    """Map handoff milestones to sparse, actionable WorkerItem notices."""

    def from_event(
        self,
        event: EventData,
        context: HandoffNotificationContext,
    ) -> HandoffNotification | None:
        kind = self._kind_for_event(event)
        if kind is None:
            return None
        if kind is HandoffNotificationKind.CONVERGED and not context.important:
            return None
        notification_id = uuid5(
            NAMESPACE_URL,
            "sidera:handoff-notification:"
            f"{self._semantic_identity(event)}:{kind.value}",
        )
        return HandoffNotification(
            notification_id=notification_id,
            source_event_id=event.id,
            kind=kind,
            root_task_id=context.root_task_id,
            root_task_title=context.root_task_title,
            subtask_path=context.subtask_path,
            worker_item_id=context.worker_item_id,
            worker_item_title=context.worker_item_title,
            summary=_SUMMARY[kind],
            action=_ACTION[kind],
            deep_link=context.deep_link,
            correlation_id=context.correlation_id,
            resource_version=context.resource_version,
        )

    @staticmethod
    def _kind_for_event(event: EventData) -> HandoffNotificationKind | None:
        if event.event_type == "HandoffTargetActivated":
            return HandoffNotificationKind.TARGET_TOOK_OVER
        if event.event_type == "HandoffConverged":
            return HandoffNotificationKind.CONVERGED
        if event.event_type == "HandoffBlocked":
            status = event.payload.get("status")
            strategies = event.payload.get("available_recovery_strategies")
            if (
                event.payload.get("attention_required") is True
                or status in {"blocked_unknown", "failed"}
                or status == "blocked"
                and (
                    not strategies
                    or event.payload.get("recovery_required") is True
                    or event.payload.get("requires_recovery_decision") is True
                )
            ):
                return HandoffNotificationKind.ATTENTION_REQUIRED
        return None

    @staticmethod
    def _semantic_identity(event: EventData) -> str:
        if event.aggregate_type == "handoff_transition":
            return f"handoff_transition:{event.aggregate_id}"
        transition_id = event.payload.get("handoff_transition_id")
        if isinstance(transition_id, str) and transition_id:
            return f"handoff_transition:{transition_id}"
        return f"{event.aggregate_type}:{event.aggregate_id}:{event.aggregate_version}"


_SUMMARY = {
    HandoffNotificationKind.ATTENTION_REQUIRED: "WorkerItem 的 agent 切换需要确认",
    HandoffNotificationKind.TARGET_TOOK_OVER: "WorkerItem 已由目标 agent 接管",
    HandoffNotificationKind.CONVERGED: "WorkerItem 已完成 agent 切换",
}

_ACTION = {
    HandoffNotificationKind.ATTENTION_REQUIRED: "inspect_worker_item_handoff",
    HandoffNotificationKind.TARGET_TOOK_OVER: "open_worker_item",
    HandoffNotificationKind.CONVERGED: "review_worker_item",
}
