from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from sidera_control_plane.application.notification_observations import (
    NotificationAuthoritativeRead,
    NotificationObservationComposer,
    NotificationObservationSource,
    NotificationRouteBackfillCandidate,
    observation_subject_key,
    projection_change_is_observable,
)
from sidera_control_plane.domain.notifications import (
    NotificationKind,
    NotificationTransition,
)
from sidera_control_plane.domain.projections import ProjectionChange
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    NotificationDeliveryIntentModel,
    NotificationFactModel,
    NotificationRouteModel,
    NotificationSourceBindingModel,
    NotificationThreadModel,
    PlanningRequestModel,
    TaskModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
)
from sidera_control_plane.persistence.projection_change_repository import (
    ProjectionChangeRepository,
)


class SqlAlchemyNotificationObservationStore(NotificationObservationSource):
    """Read current state for notification reconciliation in one DB session."""

    def __init__(
        self,
        session: Session,
        *,
        composer: NotificationObservationComposer | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._projection_changes = ProjectionChangeRepository(session)
        self._composer = composer or NotificationObservationComposer()
        self._clock = clock or (lambda: datetime.now(UTC))

    def list_after(
        self, workspace_id: UUID, *, cursor: int, limit: int
    ) -> tuple[ProjectionChange, ...]:
        return self._projection_changes.list_after(
            workspace_id, cursor=cursor, limit=limit
        )

    def observe(self, change: ProjectionChange):
        if not projection_change_is_observable(change):
            return None
        authoritative_read = self._read_current_state(change)
        if authoritative_read is None:
            return None
        return self._composer.compose(change, authoritative_read)

    def list_route_backfill_candidates(
        self,
        workspace_id: UUID,
        *,
        now: datetime,
        window: timedelta,
    ) -> tuple[NotificationRouteBackfillCandidate, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if window <= timedelta(0):
            raise ValueError("window must be positive")
        intent_exists = exists(
            select(NotificationDeliveryIntentModel.id).where(
                NotificationDeliveryIntentModel.workspace_id == workspace_id,
                NotificationDeliveryIntentModel.notification_fact_id
                == NotificationFactModel.id,
                NotificationDeliveryIntentModel.notification_route_id
                == NotificationRouteModel.id,
                NotificationDeliveryIntentModel.delivery_round == 1,
            )
        )
        rows = self._session.execute(
            select(
                NotificationFactModel.id,
                NotificationThreadModel.id,
                NotificationRouteModel.id,
            )
            .join(
                NotificationThreadModel,
                NotificationThreadModel.latest_fact_id == NotificationFactModel.id,
            )
            .join(
                NotificationSourceBindingModel,
                (
                    NotificationSourceBindingModel.resource_type == "root_task"
                )
                & (
                    NotificationSourceBindingModel.resource_id
                    == NotificationThreadModel.root_task_id
                )
                & (
                    NotificationSourceBindingModel.workspace_id
                    == NotificationThreadModel.workspace_id
                )
                & (
                    NotificationSourceBindingModel.binding_kind == "origin"
                ),
            )
            .join(
                NotificationRouteModel,
                (
                    NotificationRouteModel.id
                    == NotificationSourceBindingModel.notification_route_id
                )
                & (
                    NotificationRouteModel.workspace_id
                    == NotificationSourceBindingModel.workspace_id
                ),
            )
            .where(
                NotificationFactModel.workspace_id == workspace_id,
                NotificationThreadModel.workspace_id == workspace_id,
                NotificationThreadModel.business_status == "open",
                NotificationThreadModel.organization_status == "active",
                NotificationFactModel.baseline_import.is_(False),
                NotificationFactModel.created_at >= now - window,
                NotificationRouteModel.status == "active",
                NotificationRouteModel.recipient_type
                == NotificationThreadModel.recipient_type,
                NotificationRouteModel.recipient_id
                == NotificationThreadModel.recipient_id,
                ~intent_exists,
            )
            .order_by(
                NotificationFactModel.created_at.asc(),
                NotificationThreadModel.id.asc(),
                NotificationRouteModel.id.asc(),
            )
        ).all()
        return tuple(
            NotificationRouteBackfillCandidate(
                fact_id=fact_id,
                thread_id=thread_id,
                route_id=route_id,
            )
            for fact_id, thread_id, route_id in rows
        )

    def _read_current_state(
        self, change: ProjectionChange
    ) -> NotificationAuthoritativeRead | None:
        subject = observation_subject_key(change)
        if subject is None or change.resource_id is None:
            return None

        worker_item = None
        execution = None
        planning_request = None
        source_resource_version = change.resource_version or 1
        subtask_id = None
        root_task_id: UUID | None = None

        if change.projection_type in {
            "worker_item_workspace",
            "worker_item_activity",
        }:
            worker_item = self._session.scalar(
                select(WorkerItemModel).where(
                    WorkerItemModel.workspace_id == change.workspace_id,
                    WorkerItemModel.id == change.resource_id,
                )
            )
            if worker_item is None:
                return None
            root_task_id = worker_item.root_task_id
            source_resource_version = worker_item.version
            placement = self._session.scalar(
                select(WorkerItemPlacementModel)
                .where(
                    WorkerItemPlacementModel.workspace_id == change.workspace_id,
                    WorkerItemPlacementModel.worker_item_id == worker_item.id,
                )
                .order_by(WorkerItemPlacementModel.plan_generation.desc())
                .limit(1)
            )
            subtask_id = placement.subtask_task_id if placement else None
        elif change.projection_type == "executions":
            execution = self._session.scalar(
                select(ExecutionModel).where(
                    ExecutionModel.workspace_id == change.workspace_id,
                    ExecutionModel.id == change.resource_id,
                )
            )
            if execution is None:
                return None
            source_resource_version = execution.version
            if execution.worker_item_id is not None:
                worker_item = self._session.scalar(
                    select(WorkerItemModel).where(
                        WorkerItemModel.workspace_id == change.workspace_id,
                        WorkerItemModel.id == execution.worker_item_id,
                    )
                )
                if worker_item is not None:
                    root_task_id = worker_item.root_task_id
                    placement = self._session.scalar(
                        select(WorkerItemPlacementModel)
                        .where(
                            WorkerItemPlacementModel.workspace_id
                            == change.workspace_id,
                            WorkerItemPlacementModel.worker_item_id == worker_item.id,
                        )
                        .order_by(WorkerItemPlacementModel.plan_generation.desc())
                        .limit(1)
                    )
                    subtask_id = placement.subtask_task_id if placement else None
            if root_task_id is None:
                root_task_id = execution.task_id
        elif change.projection_type == "planning_request":
            planning_request = self._session.scalar(
                select(PlanningRequestModel).where(
                    PlanningRequestModel.workspace_id == change.workspace_id,
                    PlanningRequestModel.id == change.resource_id,
                )
            )
            if planning_request is None or planning_request.root_task_id is None:
                return None
            root_task_id = planning_request.root_task_id
            source_resource_version = planning_request.version
        elif change.projection_type == "subtask_workspace":
            placement = self._session.scalar(
                select(WorkerItemPlacementModel)
                .where(
                    WorkerItemPlacementModel.workspace_id == change.workspace_id,
                    WorkerItemPlacementModel.subtask_task_id == change.resource_id,
                )
                .order_by(WorkerItemPlacementModel.plan_generation.desc())
                .limit(1)
            )
            if placement is not None:
                root_task_id = placement.root_task_id
                subtask_id = change.resource_id
            else:
                root_task_id = change.resource_id
        else:
            root_task_id = change.resource_id

        root_task = self._session.scalar(
            select(TaskModel).where(
                TaskModel.workspace_id == change.workspace_id,
                TaskModel.id == root_task_id,
            )
        )
        if root_task is None:
            return None

        subtask_path: tuple[str, ...] = ()
        if subtask_id is not None:
            subtask = self._session.scalar(
                select(TaskModel).where(
                    TaskModel.workspace_id == change.workspace_id,
                    TaskModel.id == subtask_id,
                )
            )
            if subtask is not None:
                subtask_path = (subtask.title,)

        (
            kind,
            topic,
            observed_status,
            transition,
            requires_operator,
            automatic_recovery,
        ) = self._classify(
            change,
            root_task_status=root_task.status,
            planning_request_status=(
                planning_request.status if planning_request is not None else None
            ),
            worker_item_status=(
                worker_item.status if worker_item is not None else None
            ),
            execution_status=execution.status if execution is not None else None,
        )

        source_type, origin_route_active = self._origin_source(
            workspace_id=change.workspace_id,
            root_task_id=root_task.id,
            planning_request_id=(
                planning_request.id if planning_request is not None else None
            ),
        )
        authoritative_fields: dict[str, object] = {
            "root_task_version": root_task.version,
            "source_resource_version": source_resource_version,
            "status": observed_status,
        }
        if execution is not None:
            authoritative_fields["failure_code"] = execution.failure_code
        if worker_item is not None:
            authoritative_fields["worker_item_version"] = worker_item.version
        if planning_request is not None:
            authoritative_fields["planning_request_version"] = planning_request.version

        subject_type = subject.subject_type.value
        subject_id = str(change.resource_id)
        deep_link = f"/workspaces/{change.workspace_id}/tasks/{root_task.id}"
        if worker_item is not None:
            deep_link += f"/worker-items/{worker_item.id}"
        elif planning_request is not None:
            deep_link += f"?planning_request={planning_request.id}"

        return NotificationAuthoritativeRead(
            recipient_type="workspace_operator",
            recipient_id="local",
            subject_type=subject_type,
            subject_id=subject_id,
            topic=topic,
            root_task_id=root_task.id,
            subtask_id=subtask_id,
            worker_item_id=worker_item.id if worker_item is not None else None,
            kind=kind,
            observed_status=observed_status,
            root_task_title=root_task.title,
            subtask_path=subtask_path,
            worker_item_title=(worker_item.title if worker_item is not None else None),
            deep_link=deep_link,
            correlation_id=str(change.correlation_id),
            transition=transition,
            requires_operator=requires_operator,
            automatic_recovery=automatic_recovery,
            source_type=source_type,
            origin_route_active=origin_route_active,
            authoritative_fields=authoritative_fields,
            source_resource_version=source_resource_version,
        )

    def _classify(
        self,
        change: ProjectionChange,
        *,
        root_task_status: str,
        planning_request_status: str | None,
        worker_item_status: str | None,
        execution_status: str | None,
    ) -> tuple[
        str,
        str,
        str,
        NotificationTransition | None,
        bool,
        bool,
    ]:
        reason = change.change_type.lower()
        status = (
            planning_request_status
            or execution_status
            or worker_item_status
            or root_task_status
        )
        if change.projection_type == "planning_request":
            if "clarification" in reason or status == "waiting_for_user":
                return (
                    NotificationKind.PLANNING_CLARIFICATION.value,
                    "planning_clarification",
                    status,
                    None,
                    True,
                    False,
                )
            if "conflicted" in reason or status == "conflicted":
                return (
                    NotificationKind.PLANNING_PROPOSAL_CONFLICTED.value,
                    "planning_proposal",
                    status,
                    None,
                    True,
                    False,
                )
            if "proposal_ready" in reason or status == "proposal_ready":
                return (
                    NotificationKind.PLANNING_PROPOSAL_READY.value,
                    "planning_proposal",
                    status,
                    None,
                    True,
                    False,
                )
            if "materialized" in reason or status == "materialized":
                return (
                    "planning_materialized",
                    "planning_materialized",
                    status,
                    NotificationTransition.COMPLETED,
                    False,
                    False,
                )
            if status in {"failed", "rejected", "cancelled"}:
                return (
                    NotificationKind.PLANNING_PROPOSAL_BLOCKED.value,
                    "planning_proposal",
                    status,
                    None,
                    True,
                    False,
                )
            return "planning_request_updated", "planning_request", status, None, False, False

        if "degraded" in reason:
            return (
                NotificationKind.PLAN_TRANSITION_DEGRADED.value,
                "plan_transition",
                status,
                NotificationTransition.DEGRADED,
                True,
                False,
            )
        if "worker_transition" in reason and "unknown" in reason:
            return (
                NotificationKind.WORKER_TRANSITION_BLOCKED_UNKNOWN.value,
                "worker_transition",
                status,
                None,
                True,
                False,
            )
        if "handoff" in reason:
            if "failed" in reason:
                kind = NotificationKind.HANDOFF_FAILED.value
            elif "unknown" in reason or "stale" in reason:
                kind = NotificationKind.HANDOFF_UNKNOWN.value
            elif "converged" in reason or "completed" in reason:
                return (
                    "handoff_converged",
                    "handoff",
                    status,
                    NotificationTransition.COMPLETED,
                    False,
                    False,
                )
            else:
                kind = NotificationKind.HANDOFF_ATTENTION.value
            return kind, "handoff", status, None, True, False
        if change.projection_type == "collaboration_governance":
            kind = (
                NotificationKind.COLLABORATION_RELEASE_PENDING.value
                if "release" in reason
                else NotificationKind.GOVERNANCE_DECISION_REQUIRED.value
            )
            return kind, "collaboration_governance", status, None, True, False
        if change.projection_type == "collaboration_runtime" and (
            "converged" in reason or "completed" in reason
        ):
            return (
                "plan_generation_converged",
                "plan_generation",
                status,
                NotificationTransition.COMPLETED,
                False,
                False,
            )
        if execution_status is not None:
            if execution_status in {"failed", "lost", "canceled"} or "failed" in reason:
                return (
                    NotificationKind.EXECUTION_FAILURE.value,
                    "execution_failure",
                    execution_status,
                    None,
                    True,
                    False,
                )
            if "attention" in reason:
                return (
                    NotificationKind.EXECUTION_ATTENTION.value,
                    "execution_attention",
                    execution_status,
                    None,
                    True,
                    False,
                )
        if root_task_status == "succeeded" or "task_completed" in reason:
            return (
                NotificationKind.TASK_COMPLETED.value,
                "task_completed",
                root_task_status,
                NotificationTransition.COMPLETED,
                False,
                False,
            )
        if root_task_status in {"blocked", "waiting"} or "blocked" in reason:
            return (
                NotificationKind.TASK_BLOCKED.value,
                "task_blocked",
                root_task_status,
                None,
                True,
                True,
            )
        if worker_item_status == "paused" or "attention" in reason:
            return (
                NotificationKind.EXECUTION_ATTENTION.value,
                "execution_attention",
                status,
                None,
                True,
                False,
            )
        if "completed" in reason or "milestone" in reason:
            return (
                NotificationKind.TASK_MILESTONE_COMPLETED.value,
                "task_milestone",
                status,
                NotificationTransition.COMPLETED,
                False,
                False,
            )
        return "state_changed", "task_state", status, None, False, False

    def _origin_source(
        self,
        *,
        workspace_id: UUID,
        root_task_id: UUID,
        planning_request_id: UUID | None,
    ) -> tuple[str, bool]:
        resource_targets = [("root_task", root_task_id)]
        if planning_request_id is not None:
            resource_targets.insert(0, ("planning_request", planning_request_id))
        for resource_type, resource_id in resource_targets:
            row = self._session.scalar(
                select(NotificationRouteModel.status)
                .join(
                    NotificationSourceBindingModel,
                    (
                        NotificationSourceBindingModel.notification_route_id
                        == NotificationRouteModel.id
                    )
                    & (
                        NotificationSourceBindingModel.workspace_id
                        == NotificationRouteModel.workspace_id
                    ),
                )
                .where(
                    NotificationRouteModel.workspace_id == workspace_id,
                    NotificationSourceBindingModel.resource_type == resource_type,
                    NotificationSourceBindingModel.resource_id == resource_id,
                    NotificationSourceBindingModel.binding_kind == "origin",
                )
                .order_by(NotificationRouteModel.registered_at.asc())
            )
            if row is not None:
                return "akashic_private", row == "active"
        return "sidera_web_chat", False
