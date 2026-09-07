from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from sidera_control_plane.application.notification_observations import (
    NotificationAuthoritativeRead,
    NotificationBaselineCandidate,
    NotificationObservationComposer,
    NotificationObservationSource,
    NotificationObservationSubjectType,
    NotificationRouteBackfillCandidate,
    observation_subject_key,
    projection_change_is_observable,
)
from sidera_control_plane.application.result_notifications import (
    result_notification_facts,
)
from sidera_control_plane.domain.notifications import (
    NotificationKind,
    NotificationTransition,
)
from sidera_control_plane.domain.projections import ProjectionChange
from sidera_control_plane.persistence.models import (
    EventModel,
    ExecutionModel,
    ExecutionRequestModel,
    IntegrationCandidateModel,
    NotificationDeliveryIntentModel,
    NotificationFactModel,
    NotificationPolicyStateModel,
    NotificationRouteModel,
    NotificationSourceBindingModel,
    NotificationThreadModel,
    PlanningRequestModel,
    ResultApplyOperationModel,
    ResultReworkRecordModel,
    ResultVerificationResultModel,
    SchedulingBlockModel,
    TaskAcceptanceDecisionModel,
    TaskModel,
    TaskResultHeadModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
)
from sidera_control_plane.persistence.projection_change_repository import (
    ProjectionChangeRepository,
)

_RESULT_INPUT_FAILURE_CODES = frozenset(
    {
        "result_input_unavailable",
        "result_input_integrity_failed",
        "result_input_format_unsupported",
        "result_input_budget_exceeded",
    }
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

    def list_baseline_candidates(
        self,
        workspace_id: UUID,
        *,
        source_cursor: int,
        now: datetime,
    ) -> tuple[NotificationBaselineCandidate, ...]:
        if source_cursor < 0:
            raise ValueError("source_cursor must be nonnegative")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        candidates: dict[
            tuple[str, UUID, str | None], NotificationBaselineCandidate
        ] = {}
        active_tasks = self._session.execute(
            select(TaskModel.id, TaskModel.version)
            .where(
                TaskModel.workspace_id == workspace_id,
                TaskModel.status.not_in(("succeeded", "failed", "canceled")),
            )
            .order_by(TaskModel.id.asc())
        ).all()
        for task_id, version in active_tasks:
            candidate = NotificationBaselineCandidate(
                change=_baseline_change(
                    workspace_id=workspace_id,
                    projection_type="task_overview",
                    resource_id=task_id,
                    resource_version=version,
                    source_cursor=source_cursor,
                    now=now,
                    topic=None,
                )
            )
            candidates[("task_overview", task_id, None)] = candidate

        active_requests = self._session.execute(
            select(PlanningRequestModel.id, PlanningRequestModel.version)
            .where(
                PlanningRequestModel.workspace_id == workspace_id,
                PlanningRequestModel.root_task_id.is_not(None),
                PlanningRequestModel.status.in_(
                    (
                        "submitted",
                        "planning",
                        "waiting_for_user",
                        "proposal_ready",
                        "failed",
                        "conflicted",
                    )
                ),
            )
            .order_by(PlanningRequestModel.id.asc())
        ).all()
        for request_id, version in active_requests:
            candidate = NotificationBaselineCandidate(
                change=_baseline_change(
                    workspace_id=workspace_id,
                    projection_type="planning_request",
                    resource_id=request_id,
                    resource_version=version,
                    source_cursor=source_cursor,
                    now=now,
                    topic=None,
                )
            )
            candidates[("planning_request", request_id, None)] = candidate

        active_result_blocks = self._session.execute(
            select(
                SchedulingBlockModel.execution_id,
                ExecutionModel.version,
                SchedulingBlockModel.reason_code,
            )
            .join(
                ExecutionModel,
                (ExecutionModel.id == SchedulingBlockModel.execution_id)
                & (ExecutionModel.workspace_id == SchedulingBlockModel.workspace_id),
            )
            .where(
                SchedulingBlockModel.workspace_id == workspace_id,
                SchedulingBlockModel.status == "active",
                SchedulingBlockModel.reason_code.in_(_RESULT_INPUT_FAILURE_CODES),
            )
            .order_by(SchedulingBlockModel.execution_id.asc())
        ).all()
        for execution_id, version, reason_code in active_result_blocks:
            topic = f"result_inputs_blocked:{reason_code}"
            candidate = NotificationBaselineCandidate(
                change=_baseline_change(
                    workspace_id=workspace_id,
                    projection_type="executions",
                    resource_id=execution_id,
                    resource_version=version,
                    source_cursor=source_cursor,
                    now=now,
                    topic=topic,
                    change_type="notification_baseline_result_inputs_blocked",
                ),
                topic=topic,
            )
            candidates[("executions", execution_id, topic)] = candidate

        policy_states = self._session.execute(
            select(
                NotificationPolicyStateModel.policy_version,
                NotificationPolicyStateModel.subject_type,
                NotificationPolicyStateModel.subject_id,
                NotificationPolicyStateModel.topic,
                NotificationPolicyStateModel.observed_payload,
            )
            .where(
                NotificationPolicyStateModel.workspace_id == workspace_id,
                NotificationPolicyStateModel.policy_version.in_((1, 2)),
            )
            .order_by(
                NotificationPolicyStateModel.subject_type.asc(),
                NotificationPolicyStateModel.subject_id.asc(),
                NotificationPolicyStateModel.topic.asc(),
            )
        ).all()
        projection_types = {
            "root_task": "task_overview",
            "planning_request": "planning_request",
            "worker_item": "worker_item_workspace",
            "execution": "executions",
            "execution_request": "executions",
        }
        for (
            policy_version,
            subject_type,
            subject_id,
            topic,
            observed_payload,
        ) in policy_states:
            projection_type = projection_types.get(subject_type)
            if projection_type is None:
                continue
            candidate_resource_id = subject_id
            if subject_type == "execution_request" and isinstance(
                observed_payload, Mapping
            ):
                candidate_resource_id = observed_payload.get("source_resource_id")
            try:
                resource_id = UUID(candidate_resource_id)
            except (AttributeError, TypeError, ValueError):
                continue
            is_result_input = (
                policy_version == 2 and subject_type == "execution_request"
            )
            candidate = NotificationBaselineCandidate(
                change=_baseline_change(
                    workspace_id=workspace_id,
                    projection_type=projection_type,
                    resource_id=resource_id,
                    resource_version=self._baseline_resource_version(
                        workspace_id,
                        subject_type,
                        resource_id,
                        observed_payload,
                    ),
                    source_cursor=source_cursor,
                    now=now,
                    topic=topic,
                    change_type=(
                        "notification_baseline_result_inputs_blocked"
                        if is_result_input
                        else "notification_baseline"
                    ),
                ),
                topic=topic,
            )
            candidates[(projection_type, resource_id, topic)] = candidate
        return tuple(
            candidates[key]
            for key in sorted(
                candidates,
                key=lambda item: (item[0], str(item[1]), item[2] or ""),
            )
        )

    def _baseline_resource_version(
        self,
        workspace_id: UUID,
        subject_type: str,
        resource_id: UUID,
        observed_payload: object,
    ) -> int:
        if subject_type == "root_task":
            version = self._session.scalar(
                select(TaskModel.version).where(
                    TaskModel.workspace_id == workspace_id,
                    TaskModel.id == resource_id,
                )
            )
        elif subject_type == "planning_request":
            version = self._session.scalar(
                select(PlanningRequestModel.version).where(
                    PlanningRequestModel.workspace_id == workspace_id,
                    PlanningRequestModel.id == resource_id,
                )
            )
        elif subject_type == "worker_item":
            version = self._session.scalar(
                select(WorkerItemModel.version).where(
                    WorkerItemModel.workspace_id == workspace_id,
                    WorkerItemModel.id == resource_id,
                )
            )
        elif subject_type in {"execution", "execution_request"}:
            version = self._session.scalar(
                select(ExecutionModel.version).where(
                    ExecutionModel.workspace_id == workspace_id,
                    ExecutionModel.id == resource_id,
                )
            )
        else:
            version = None
        if version is not None:
            return int(version)
        if isinstance(observed_payload, Mapping):
            raw_version = observed_payload.get("resource_version")
            if (
                isinstance(raw_version, int)
                and not isinstance(raw_version, bool)
                and raw_version >= 1
            ):
                return raw_version
        return 1

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
        execution_request = None
        scheduling_block = None
        result_input_change = False
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
            change_reason = change.change_type.lower()
            result_input_change = (
                "scheduling" in change_reason or "result_input" in change_reason
            )
            scheduling_block = self._session.scalar(
                select(SchedulingBlockModel)
                .where(
                    SchedulingBlockModel.workspace_id == change.workspace_id,
                    SchedulingBlockModel.execution_id == execution.id,
                )
                .order_by(
                    SchedulingBlockModel.last_seen_at.desc(),
                    SchedulingBlockModel.version.desc(),
                )
                .limit(1)
            )
            if (
                scheduling_block is not None
                and scheduling_block.reason_code in _RESULT_INPUT_FAILURE_CODES
            ):
                result_input_change = True
                execution_request = self._session.scalar(
                    select(ExecutionRequestModel)
                    .where(
                        ExecutionRequestModel.workspace_id == change.workspace_id,
                        ExecutionRequestModel.execution_lineage_id
                        == execution.lineage_id,
                    )
                    .order_by(
                        ExecutionRequestModel.version.desc(),
                        ExecutionRequestModel.id.asc(),
                    )
                    .limit(1)
                )
                if execution_request is None:
                    return None
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
        if change.projection_type == "result_overview":
            source_type, origin_route_active = self._origin_source(
                workspace_id=change.workspace_id,
                root_task_id=root_task.id,
                planning_request_id=None,
            )
            overview = self._result_notification_overview(
                change,
                root_task=root_task,
                source_type=source_type,
                origin_route_active=origin_route_active,
            )
            observations = result_notification_facts(
                overview["event"], overview["view"]
            )
            if not observations:
                return None
            return replace(
                observations[0],
                source_projection_cursor=change.cursor,
                source_resource_version=(change.resource_version or 1),
                correlation_id=str(change.correlation_id),
                source_type=source_type,
                origin_route_active=origin_route_active,
            )
        if subject.subject_type.value == "root_task":
            source_resource_version = root_task.version

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
            scheduling_block=scheduling_block,
            result_input_change=result_input_change,
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
        if (
            scheduling_block is not None
            and scheduling_block.reason_code in _RESULT_INPUT_FAILURE_CODES
        ):
            authoritative_fields.update(
                {
                    "result_input_reason": scheduling_block.reason_code,
                    "result_input_block_id": str(scheduling_block.id),
                    "result_input_block_status": scheduling_block.status,
                    "result_input_block_version": scheduling_block.version,
                    "result_input_fingerprint": scheduling_block.fingerprint,
                    "execution_id": str(execution.id) if execution is not None else None,
                    "execution_request_id": (
                        str(execution_request.id)
                        if execution_request is not None
                        else None
                    ),
                }
            )
        if worker_item is not None:
            authoritative_fields["worker_item_version"] = worker_item.version
        if planning_request is not None:
            authoritative_fields["planning_request_version"] = planning_request.version

        subject_type = subject.subject_type.value
        subject_id = str(change.resource_id)
        if (
            result_input_change
            and scheduling_block is not None
            and scheduling_block.reason_code in _RESULT_INPUT_FAILURE_CODES
            and execution_request is not None
            and kind == NotificationKind.RESULT_INPUTS_BLOCKED.value
        ):
            subject_type = NotificationObservationSubjectType.EXECUTION_REQUEST.value
            subject_id = str(execution_request.id)
        deep_link = f"/workspaces/{change.workspace_id}/tasks/{root_task.id}"
        if worker_item is not None:
            deep_link = (
                f"/workspaces/{change.workspace_id}/worker-items/{worker_item.id}"
            )
        elif planning_request is not None:
            deep_link += f"?planning_request={planning_request.id}"
        if (
            subject_type == NotificationObservationSubjectType.EXECUTION_REQUEST.value
            and execution_request is not None
        ):
            separator = "&" if "?" in deep_link else "?"
            deep_link += f"{separator}execution_request={execution_request.id}"

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

    def _result_notification_overview(
        self,
        change: ProjectionChange,
        *,
        root_task: TaskModel,
        source_type: str,
        origin_route_active: bool,
    ) -> dict[str, object]:
        """Build the bounded result state consumed by result notifications."""

        head = self._session.scalar(
            select(TaskResultHeadModel).where(
                TaskResultHeadModel.workspace_id == change.workspace_id,
                TaskResultHeadModel.root_task_id == root_task.id,
            )
        )
        candidate = None
        acceptance = None
        active_operation = None
        verification_status: str | None = None
        rework_status: str | None = None
        if head is not None and head.current_candidate_id is not None:
            candidate = self._session.scalar(
                select(IntegrationCandidateModel).where(
                    IntegrationCandidateModel.workspace_id == change.workspace_id,
                    IntegrationCandidateModel.id == head.current_candidate_id,
                )
            )
        if head is not None and head.current_acceptance_id is not None:
            acceptance = self._session.scalar(
                select(TaskAcceptanceDecisionModel).where(
                    TaskAcceptanceDecisionModel.workspace_id == change.workspace_id,
                    TaskAcceptanceDecisionModel.id == head.current_acceptance_id,
                )
            )
        if head is not None and head.active_operation_id is not None:
            active_operation = self._session.scalar(
                select(ResultApplyOperationModel).where(
                    ResultApplyOperationModel.workspace_id == change.workspace_id,
                    ResultApplyOperationModel.id == head.active_operation_id,
                )
            )
        if candidate is not None:
            verification = self._session.scalars(
                select(ResultVerificationResultModel)
                .where(
                    ResultVerificationResultModel.workspace_id == change.workspace_id,
                    ResultVerificationResultModel.candidate_id == candidate.id,
                )
                .order_by(
                    ResultVerificationResultModel.created_at.desc(),
                    ResultVerificationResultModel.id.desc(),
                )
                .limit(1)
            ).first()
            verification_status = None if verification is None else verification.status
            rework = self._session.scalars(
                select(ResultReworkRecordModel)
                .where(
                    ResultReworkRecordModel.workspace_id == change.workspace_id,
                    ResultReworkRecordModel.candidate_id == candidate.id,
                )
                .order_by(
                    ResultReworkRecordModel.created_at.desc(),
                    ResultReworkRecordModel.id.desc(),
                )
                .limit(1)
            ).first()
            rework_status = None if rework is None else rework.status

        event = self._session.scalars(
            select(EventModel)
            .where(
                EventModel.workspace_id == change.workspace_id,
                EventModel.correlation_id == change.correlation_id,
            )
            .order_by(EventModel.created_at.desc(), EventModel.id.desc())
            .limit(1)
        ).first()
        event_type = (
            event.event_type
            if event is not None
            else change.change_type
        )
        stage = "legacy"
        blocking: list[str] = []
        if head is not None:
            stage = "waiting_for_result"
            if active_operation is not None:
                if active_operation.status in {"unknown", "blocked", "failed"}:
                    stage = "attention"
                    blocking.append(
                        f"apply_{active_operation.status}"
                    )
                else:
                    stage = "applying"
            elif verification_status in {
                "unknown",
                "interrupted",
                "blocked",
                "failed",
            }:
                stage = "attention"
                blocking.append(f"verification_{verification_status}")
            elif rework_status in {"blocked", "failed"}:
                stage = "attention"
                blocking.append(f"rework_{rework_status}")
            elif acceptance is not None:
                stage = "finalizing"
            elif candidate is not None:
                stage = "awaiting_acceptance"

        attention_items: list[dict[str, object]] = []
        payload = event.payload if event is not None else {}
        worker_item_value = (
            payload.get("worker_item_id") if isinstance(payload, dict) else None
        )
        try:
            worker_item_id = (
                worker_item_value
                if isinstance(worker_item_value, UUID)
                else UUID(str(worker_item_value))
            )
        except (TypeError, ValueError):
            worker_item_id = None
        if stage == "attention" and worker_item_id is not None:
            worker_item = self._session.scalar(
                select(WorkerItemModel).where(
                    WorkerItemModel.workspace_id == change.workspace_id,
                    WorkerItemModel.id == worker_item_id,
                )
            )
            if worker_item is not None:
                placement = self._session.scalar(
                    select(WorkerItemPlacementModel)
                    .where(
                        WorkerItemPlacementModel.workspace_id == change.workspace_id,
                        WorkerItemPlacementModel.worker_item_id == worker_item.id,
                    )
                    .order_by(WorkerItemPlacementModel.plan_generation.desc())
                    .limit(1)
                )
                subtask = (
                    self._session.scalar(
                        select(TaskModel).where(
                            TaskModel.workspace_id == change.workspace_id,
                            TaskModel.id == placement.subtask_task_id,
                        )
                    )
                    if placement is not None and placement.subtask_task_id is not None
                    else None
                )
                attention_items.append(
                    {
                        "cause_key": f"result:{candidate.id if candidate else root_task.id}:{blocking[0] if blocking else 'attention'}",
                        "subtask_id": None if subtask is None else str(subtask.id),
                        "subtask_title": None if subtask is None else subtask.title,
                        "worker_item_id": str(worker_item.id),
                        "worker_item_title": worker_item.title,
                        "resource_version": worker_item.version,
                    }
                )
        return {
            "event": {
                "event_type": event_type,
                "workspace_id": str(change.workspace_id),
                "root_task_id": str(root_task.id),
                "cursor": change.cursor,
                "resource_version": change.resource_version,
                "correlation_id": str(change.correlation_id),
            },
            "view": {
                "workspace_id": str(change.workspace_id),
                "root_task_id": str(root_task.id),
                "task_title": root_task.title,
                "task_status": root_task.status,
                "result_version": None if head is None else head.result_version,
                "result_stage": stage,
                "current_candidate": (
                    None
                    if candidate is None
                    else {
                        "candidate_id": str(candidate.id),
                        "candidate_hash": candidate.content_hash,
                    }
                ),
                "blocking_reasons": blocking,
                "attention_items": attention_items,
                "source_type": source_type,
                "origin_route_active": origin_route_active,
            },
        }

    def _classify(
        self,
        change: ProjectionChange,
        *,
        root_task_status: str,
        planning_request_status: str | None,
        worker_item_status: str | None,
        execution_status: str | None,
        scheduling_block: SchedulingBlockModel | None,
        result_input_change: bool,
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
        if (
            result_input_change
            and scheduling_block is not None
            and scheduling_block.reason_code in _RESULT_INPUT_FAILURE_CODES
        ):
            recovered = scheduling_block.status == "resolved"
            return (
                NotificationKind.RESULT_INPUTS_BLOCKED.value,
                f"result_inputs_blocked:{scheduling_block.reason_code}",
                "recovered" if recovered else "blocked",
                NotificationTransition.RECOVERED if recovered else None,
                scheduling_block.requires_operator,
                False,
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


def _baseline_change(
    *,
    workspace_id: UUID,
    projection_type: str,
    resource_id: UUID,
    resource_version: int,
    source_cursor: int,
    now: datetime,
    topic: str | None,
    change_type: str = "notification_baseline",
) -> ProjectionChange:
    correlation_id = uuid5(
        NAMESPACE_URL,
        f"sidera:notification-baseline:{workspace_id}:{projection_type}:"
        f"{resource_id}:{topic or '-'}:{source_cursor}",
    )
    return ProjectionChange(
        workspace_id=workspace_id,
        cursor=source_cursor,
        schema_version=1,
        projection_type=projection_type,
        resource_id=resource_id,
        change_type=change_type,
        resource_version=resource_version,
        occurred_at=now,
        correlation_id=correlation_id,
    )
