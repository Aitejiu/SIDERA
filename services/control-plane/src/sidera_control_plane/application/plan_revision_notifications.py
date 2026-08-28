from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.domain.events import EventData


class PlanRevisionNotificationKind(StrEnum):
    PROPOSAL_READY = "proposal_ready"
    PROPOSAL_STALE = "proposal_stale"
    GENERATION_PUBLISHED = "generation_published"
    SUBTASK_CONVERGED = "subtask_converged"
    TRANSITION_DEGRADED = "transition_degraded"
    WORKER_BLOCKED_UNKNOWN = "worker_blocked_unknown"
    ALL_CONVERGED = "all_converged"


@dataclass(frozen=True, slots=True)
class PlanRevisionNotificationContext:
    """Human-readable scope resolved by an authoritative task projection."""

    root_task_id: UUID
    root_task_title: str
    subtask_path: tuple[str, ...]
    deep_link: str
    correlation_id: UUID
    resource_version: int
    important: bool = False


@dataclass(frozen=True, slots=True)
class PlanRevisionNotification:
    notification_id: UUID
    source_event_id: UUID
    kind: PlanRevisionNotificationKind
    root_task_id: UUID
    root_task_title: str
    subtask_path: tuple[str, ...]
    summary: str
    action: str
    deep_link: str
    correlation_id: UUID
    resource_version: int

    def to_payload(self) -> dict[str, Any]:
        """Return the public notification contract without event internals."""

        return {
            "notification_id": str(self.notification_id),
            "source_event_id": str(self.source_event_id),
            "kind": self.kind.value,
            "root_task_id": str(self.root_task_id),
            "root_task_title": self.root_task_title,
            "subtask_path": list(self.subtask_path),
            "summary": self.summary,
            "action": self.action,
            "deep_link": self.deep_link,
            "correlation_id": str(self.correlation_id),
            "resource_version": self.resource_version,
        }


class PlanRevisionNotificationService:
    """Map durable plan events to sparse, actionable notifications.

    The service deliberately does not read or copy event payloads. Callers must
    resolve titles, paths and links from the current authoritative projection.
    """

    def from_event(
        self,
        event: EventData,
        context: PlanRevisionNotificationContext,
    ) -> PlanRevisionNotification | None:
        kind = self._kind_for_event(event)
        if kind is None:
            return None
        if kind is PlanRevisionNotificationKind.SUBTASK_CONVERGED and not context.important:
            return None

        notification_id = uuid5(
            NAMESPACE_URL,
            f"sidera:plan-revision-notification:{self._semantic_identity(event)}:"
            f"{kind.value}",
        )
        return PlanRevisionNotification(
            notification_id=notification_id,
            source_event_id=event.id,
            kind=kind,
            root_task_id=context.root_task_id,
            root_task_title=context.root_task_title,
            subtask_path=context.subtask_path,
            summary=_SUMMARY[kind],
            action=_ACTION[kind],
            deep_link=context.deep_link,
            correlation_id=context.correlation_id,
            resource_version=context.resource_version,
        )

    @staticmethod
    def _kind_for_event(event: EventData) -> PlanRevisionNotificationKind | None:
        event_type = event.event_type
        payload = event.payload

        if event_type == "PlanningProposalReady":
            return PlanRevisionNotificationKind.PROPOSAL_READY
        if event_type == "PlanningProposalConflicted":
            return PlanRevisionNotificationKind.PROPOSAL_STALE
        if event_type == "PlanningMaterialized":
            return PlanRevisionNotificationKind.GENERATION_PUBLISHED
        if event_type == "WorkerTransitionBlockedUnknown":
            return PlanRevisionNotificationKind.WORKER_BLOCKED_UNKNOWN
        if event_type in {"WorkerTransitionConverged", "WorkerTransitionActivated"}:
            return (
                PlanRevisionNotificationKind.SUBTASK_CONVERGED
                if payload.get("status") in {None, "converged"}
                else None
            )
        if event_type == "WorkerTransitionFailed":
            return PlanRevisionNotificationKind.TRANSITION_DEGRADED

        if event_type == "PlanTransitionCreated":
            return PlanRevisionNotificationService._transition_kind(
                payload.get("status"),
                default=PlanRevisionNotificationKind.GENERATION_PUBLISHED,
            )
        if event_type in {
            "PlanTransitionConverged",
            "PlanTransitionDegraded",
            "PlanTransitionReconciled",
            "PlanTransitionUpdated",
        }:
            return PlanRevisionNotificationService._transition_kind(
                payload.get("status")
            )
        return None

    @staticmethod
    def _semantic_identity(event: EventData) -> str:
        if event.aggregate_type == "plan_transition":
            return f"plan_transition:{event.aggregate_id}"
        if event.aggregate_type == "worker_transition":
            return f"worker_transition:{event.aggregate_id}"
        for key in ("plan_transition_id", "worker_transition_id"):
            value = event.payload.get(key)
            if isinstance(value, str) and value:
                return f"{key.removesuffix('_id')}:{value}"
        return f"{event.aggregate_type}:{event.aggregate_id}:{event.aggregate_version}"

    @staticmethod
    def _transition_kind(
        status: object,
        *,
        default: PlanRevisionNotificationKind | None = None,
    ) -> PlanRevisionNotificationKind | None:
        if status == "degraded":
            return PlanRevisionNotificationKind.TRANSITION_DEGRADED
        if status == "converged":
            return PlanRevisionNotificationKind.ALL_CONVERGED
        return default


_SUMMARY = {
    PlanRevisionNotificationKind.PROPOSAL_READY: "新的计划方案已就绪",
    PlanRevisionNotificationKind.PROPOSAL_STALE: "当前计划已变化，需要重新审阅",
    PlanRevisionNotificationKind.GENERATION_PUBLISHED: "新的计划代已发布，正在收敛",
    PlanRevisionNotificationKind.SUBTASK_CONVERGED: "重要子任务已完成计划切换",
    PlanRevisionNotificationKind.TRANSITION_DEGRADED: "计划切换出现降级，需要处理",
    PlanRevisionNotificationKind.WORKER_BLOCKED_UNKNOWN: "子任务切换结果未知，需要确认",
    PlanRevisionNotificationKind.ALL_CONVERGED: "全部子任务已完成计划收敛",
}

_ACTION = {
    PlanRevisionNotificationKind.PROPOSAL_READY: "review_revision",
    PlanRevisionNotificationKind.PROPOSAL_STALE: "refresh_revision",
    PlanRevisionNotificationKind.GENERATION_PUBLISHED: "watch_convergence",
    PlanRevisionNotificationKind.SUBTASK_CONVERGED: "review_subtask",
    PlanRevisionNotificationKind.TRANSITION_DEGRADED: "recover_transition",
    PlanRevisionNotificationKind.WORKER_BLOCKED_UNKNOWN: "inspect_transition",
    PlanRevisionNotificationKind.ALL_CONVERGED: "review_result",
}
